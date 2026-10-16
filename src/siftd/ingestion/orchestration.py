"""Orchestration: coordinate ingestion pipeline.

Design constraint: cardinality is a property of the adapter's DEDUP_STRATEGY.
----------------------------------------------------------------------------
The `ingested_files.path` column is UNIQUE, so a row can name at most one
conversation. Which of the two branches below a source takes decides what that
row means:

- `"file"`: one conversation per source (JSONL session logs, single-transcript
  exports, a Gemini CLI chat JSON). The row points at it, and a content change
  replaces it outright.
  A parse yielding more than one conversation is an adapter bug and fails the
  source.
- `"session"`: conversations are deduped by external_id rather than by the
  file that produced them. That covers a source holding many at once (an
  OpenCode SQLite DB, an aider history file accumulating every session for a
  project). The row is a hash/mtime marker with `conversation_id = NULL`, so
  replacing any one conversation does not cascade the marker away.

An adapter whose source grows new conversations over its life belongs on
`"session"`, whatever its storage format. `aider` did not, and every history
file with a second session failed permanently (#36).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from siftd.adapters.assembly import Ledger
from siftd.adapters.contract import adapt, decodes
from siftd.adapters.records import CONTENT_REVISION_ATTRIBUTE, CONTENT_SOURCE_ATTRIBUTE, Disposition
from siftd.adapters.sdk import AdapterParseError, same_file, spells
from siftd.dateparse import to_utc
from siftd.domain import Conversation, Source
from siftd.storage.attributes import get_attribute
from siftd.storage.replacement import (
    ConversationCarryover,
    merged_external_ids,
    release_carryover,
    restore_and_report,
    snapshot_conversation,
    unrestorable_user_tags,
)
from siftd.storage.sessions import (
    drain_pending_tags,
    rekey_pending_tags,
    resolve_session_conversation,
    unregister_session,
)
from siftd.storage.sqlite import (
    clear_ingested_file_error,
    clear_vocabulary_caches,
    compute_file_hash,
    conversation_exists,
    delete_conversation,
    ensure_tool_aliases,
    find_conversation_by_external_id,
    find_conversation_by_id,
    get_ingested_file_info,
    get_or_create_harness,
    harness_id_for_conversation,
    ingested_paths_for_conversation,
    ingested_paths_for_harness,
    link_ingested_file,
    message_rows,
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    record_session_file,
    release_conversation_pointers,
    store_conversation,
    update_file_stat,
)

from .discovery import discover_all

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from siftd.ingestion import AdapterModule


@dataclass(frozen=True)
class TagSkip:
    """A conversation a reparse declined to replace because it would lose user tags."""
    path: str
    conversation_id: str
    lost: int


@dataclass(frozen=True)
class RefusedAdoption:
    """A stored row a conversation was not allowed to adopt: it may be another conversation's.

    The row and its assignments were left as they were, and the conversation was
    stored under its own key beside it.
    """
    adapter: str
    path: str
    conversation_id: str
    external_id: str


@dataclass
class IngestStats:
    """Statistics from an ingestion run."""
    files_found: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    files_replaced: int = 0
    files_errored: int = 0
    conversations: int = 0
    prompts: int = 0
    responses: int = 0
    tool_calls: int = 0
    # Per adapter name: conversations, prompts, responses, tool_calls, replaced,
    # errors, plus the ledger counts (LEDGER_COUNTS) of the sources parsed this run.
    by_harness: dict = field(default_factory=dict)
    # --reparse only: sources re-parsed although their stat or hash was unchanged.
    reparsed: int = 0
    # Conversations deleted and stored again in their place (any run). Counted
    # per conversation, because a container can replace one session and ingest
    # another, which no per-file outcome counter can express.
    conversations_replaced: int = 0
    # --reparse only: conversations left as they were because replacing them
    # would lose user-applied tags (named by id; --force replaces them anyway).
    skipped_for_tags: list[TagSkip] = field(default_factory=list)
    # Rows found under a superseded key whose evidence did not identify them as
    # the conversation's own (see _adoption_is_ambiguous).
    refused_adoptions: list[RefusedAdoption] = field(default_factory=list)


@dataclass
class IngestEvent:
    """Per-file ingestion event for progress reporting."""
    adapter: str
    status: str
    reason: str | None
    path: str
    index: int | None
    total: int | None
    workspace_path: str | None = None
    summary: str | None = None
    exchange_count: int | None = None
    model: str | None = None
    error: str | None = None
    # Records of this source the adapter did not recognize / could not place /
    # deliberately skipped. Zero for a source that was not parsed or whose adapter
    # does not decode (interface version 1), which has no ledger.
    unknown: int = 0
    orphan: int = 0
    ignored: int = 0


#: The ledger dispositions surfaced per harness: what the decoder did not
#: recognize, what could not be placed, and what it deliberately skipped.
LEDGER_COUNTS = {
    "unknown": Disposition.UNKNOWN,
    "orphan": Disposition.ORPHAN,
    "ignored": Disposition.IGNORED,
}


def _count_ledger(stats: IngestStats, harness_name: str, ledger: Ledger) -> None:
    """Add one parsed source's ledger to its harness's counts."""
    counts = stats.by_harness[harness_name]
    for key, disposition in LEDGER_COUNTS.items():
        counts[key] += ledger.count(disposition)


def _parse_source(adapter: AdapterModule, source: Source, stats: IngestStats | None) -> list:
    """The conversations of a source; for an adapter that decodes, its ledger lands in ``stats``.

    Only a parse whose result is stored passes ``stats``: the parses that only
    check identity read the same source again and must not count it twice.
    """
    if not decodes(adapter):
        return list(adapter.parse(source))
    assembly = adapter.assemble(source)
    if stats is not None:
        _count_ledger(stats, adapter.NAME, assembly.ledger)
    return list(assembly.conversations)


def _compare_timestamps(new_ts: str | None, existing_ts: str | None) -> bool:
    """Return True if new_ts is newer than existing_ts.

    None is treated as oldest (so any timestamp beats None).

    Resolves both to instants rather than comparing the strings, because the
    two sides can be spelled differently — one row written by an adapter that
    passes its log's timestamp through, the other by one that renders its own.
    """
    if new_ts is None:
        return False
    if existing_ts is None:
        return True
    return to_utc(new_ts) > to_utc(existing_ts)


def _stat_unchanged(existing_info, st: _StatLike) -> bool:
    """Whether the cheap skip applies: same mtime and size, nothing unresolved.

    Both dedup branches open with this and then with :func:`_hash_unchanged`,
    so the rule they share lives here rather than in two copies that drift.
    The shared half is the one that keeps costing us: **a row carrying an
    error is re-examined every run, whatever its stat says.** The error write
    stamps the *failing* file's own hash/mtime/size, so a source that stops
    changing after a failure matches both skips forever — which is how rows
    poisoned by the #29 race survived every later ingest, and how an aider
    file poisoned by #36 would have survived the very upgrade that fixes it.
    Re-examining is what makes self-healing reachable without the file having
    to grow again.
    """
    if existing_info["error"] is not None:
        return False
    stored_mtime = existing_info["file_mtime"]
    return (
        stored_mtime is not None
        and st.st_mtime == stored_mtime
        and st.st_size == existing_info["file_size"]
    )


def _hash_unchanged(existing_info, current_hash: str) -> bool:
    """Whether the content is byte-identical to what was ingested.

    Reached when the stat moved but the bytes may not have. Carries the same
    errored-row rule as :func:`_stat_unchanged` for the same reason.
    """
    return (
        existing_info["error"] is None and current_hash == existing_info["file_hash"]
    )


class _StatLike(Protocol):
    """The two stat fields the skip checks and the file markers read.

    ``os.stat_result`` satisfies it, and so does a session source's combined
    change signal (:class:`_ChangeStat`).
    """

    st_mtime: float
    st_size: int


class _ChangeStat(NamedTuple):
    """The stat of a session source's change files: newest mtime, total size."""

    st_mtime: float
    st_size: int


def _change_files(adapter: AdapterModule, source: Source) -> list[Path]:
    """The files whose bytes decide whether a session source changed.

    An adapter declares its change files with ``change_paths(source)`` when its
    store has companions (SQLite's write-ahead log). Without one the source's own
    file is the whole signal, exactly as before. The source's file is always
    first, so a missing source still fails at the stat, as it always did.
    """
    declared = getattr(adapter, "change_paths", None)
    if declared is None:
        return [source.as_path]
    paths = [source.as_path]
    paths += [p for p in declared(source) if p != source.as_path and p.exists()]
    return paths


def _change_stat(paths: list[Path]) -> _ChangeStat:
    stats = [p.stat() for p in paths]
    return _ChangeStat(max(s.st_mtime for s in stats), sum(s.st_size for s in stats))


def _change_hash(paths: list[Path]) -> str:
    """One digest for the change files: the file's own digest, or a digest of the digests."""
    digests = [compute_file_hash(p) for p in paths]
    if len(digests) == 1:
        return digests[0]
    return hashlib.sha256("".join(digests).encode()).hexdigest()


def _with_content_source(conversation, file_path: str):
    """The conversation with the source it was read from named, when it states a revision.

    A revision is meaningful only within the source it was read from, and the
    source is ingest's to name: ``ingested_files`` keys a source by its path. The
    adapter states the revision; ingest states where it came from. The adapter's
    output therefore carries no machine-specific path.
    """
    if CONTENT_REVISION_ATTRIBUTE not in conversation.attributes:
        return conversation
    return replace(
        conversation,
        attributes={**conversation.attributes, CONTENT_SOURCE_ATTRIBUTE: file_path},
    )


def _revision_moved(conn, conversation, stored_id: str) -> bool:
    """Whether the stored copy is an older read of this same source's content.

    A revision is a digest: it says the content changed, not which way. Two
    databases can hold copies of one session (a backup beside the live file), and
    a digest cannot tell the newer copy from the older one. So a revision is
    trusted only when both copies came from the same source; a copy from another
    source competes on ``ended_at`` alone, which keeps latest-wins across sources.

    A conversation that declares no revision has no detector here. A stored copy
    with no revision (stored before the adapter declared one) has no source to
    match, so it too competes on ``ended_at``: an older copy cannot overwrite it.
    """
    revision = conversation.attributes.get(CONTENT_REVISION_ATTRIBUTE)
    if revision is None:
        return False
    stored_revision = get_attribute(conn, "conversation", stored_id, CONTENT_REVISION_ATTRIBUTE)
    if stored_revision is None or stored_revision == revision:
        return False
    source = conversation.attributes.get(CONTENT_SOURCE_ATTRIBUTE)
    stored_source = get_attribute(conn, "conversation", stored_id, CONTENT_SOURCE_ATTRIBUTE)
    return source is not None and source == stored_source


def _from_another_source(conn, conversation, stored_id: str) -> bool:
    """Whether both copies name the source they were read from, and the sources differ.

    Only a conversation that states a revision is stamped with its source
    (:func:`_with_content_source`), and it is exactly such a source whose content
    can change while its end time does not. Elsewhere there is no source to
    compare, and False keeps the existing rule.
    """
    source = conversation.attributes.get(CONTENT_SOURCE_ATTRIBUTE)
    if source is None:
        return False
    stored_source = get_attribute(conn, "conversation", stored_id, CONTENT_SOURCE_ATTRIBUTE)
    return stored_source is not None and stored_source != source


def _session_should_replace(conn, conversation, stored_id: str, stored_ended: str | None, *, reparse: bool) -> bool:
    """Whether a re-parsed session replaces the copy stored under its key.

    ``ended_at`` is the session branch's per-conversation change detector: a
    container re-parses every session it holds, and only the ones that moved are
    worth a delete-then-insert. It replaces when any of these holds:

    - its content revision moved within the same source path
      (:func:`_revision_moved`), whatever its end time says;
    - either ``ended_at`` is missing, so there is no order to apply. A session
      with none has no detector at all, and "newer than" would be false forever,
      which froze every aider session at its first ingest (#36);
    - the incoming ``ended_at`` is newer;
    - under ``--reparse``, the two are equal and the copy was not read from
      another source (:func:`_from_another_source`): an unchanged source is
      re-parsed so the current parser reaches it. A copy from another source (a
      backup beside the live database) competes on newer-only, because a source
      with revisions can change content without moving its end time, so an equal
      end time says nothing about which copy is current.

    Each replacement compares against what is stored at that moment, so an older
    copy never displaces a newer one, whatever order the sources arrive in.
    """
    new_ended = conversation.ended_at
    if _revision_moved(conn, conversation, stored_id) or new_ended is None or stored_ended is None:
        return True
    if reparse and not _from_another_source(conn, conversation, stored_id):
        return not _compare_timestamps(stored_ended, new_ended)
    return _compare_timestamps(new_ended, stored_ended)


def _instant(timestamp: str | None) -> object | None:
    """The instant a timestamp names, so two spellings of one time are one key.

    A timestamp that does not parse is kept as the text it was written as.
    """
    if not timestamp:
        return None
    try:
        return to_utc(timestamp)
    except ValueError:
        return timestamp


def _message_key(kind: str, external_id: str | None, timestamp: str | None, text: str | None = None) -> tuple | None:
    """Identity of one prompt or response for adoption, or None when it has none.

    The adapter's message id is the identity when there is one. A message without
    an id is identified by its text and its time together. The text says what the
    message is; the time says which message it is. Text alone is not enough: one
    prompt ("continue") is typed in many sessions, and a session opened later must
    not claim the row an earlier one stored. Some sources stamp every message of a
    session with the session's start (aider), so there the time only separates
    sessions by when they opened, and the text does the rest. A message with no
    id and no text is identified by its time alone.
    """
    if external_id:
        return ("id", external_id)
    if text:
        return ("text", kind, text, _instant(timestamp))
    if timestamp:
        return ("at", kind, _instant(timestamp))
    return None


def _message_keys(messages: Iterable[tuple[str, str | None, str | None, str | None]]) -> set[tuple]:
    """Keys of ``(kind, external_id, timestamp, text)`` messages, skipping those with none."""
    keys = (_message_key(kind, external_id, timestamp, text) for kind, external_id, timestamp, text in messages)
    return {key for key in keys if key is not None}


def _block_text(fields: Any) -> str | None:
    """The text a content block's fields hold, indexed (``text``) or not (``meta_text``).

    Whether text is indexed is a display choice that changes between releases
    (codex_cli moved injected context onto ``meta_text``), not part of which
    message it is, so a stored row and a new parse of one message agree.
    """
    if not isinstance(fields, dict):
        return None
    text = fields.get("text") or fields.get("meta_text")
    return text if isinstance(text, str) and text else None


def _stored_text(raw: str | None) -> str | None:
    """The text of a stored first content block (``json.dumps`` of its dict), or None."""
    if not raw:
        return None
    try:
        return _block_text(json.loads(raw))
    except ValueError:
        return None


def _parsed_text(content) -> str | None:
    """The text of a parsed message's first content block, or None."""
    return _block_text(content[0].content) if content else None


def _stored_message_keys(
    conn, conversation_id: str, kinds: tuple[str, ...] = ("prompt", "response")
) -> set[tuple]:
    """Message keys of a stored conversation's prompts and/or responses."""
    return _message_keys(
        (row["kind"], row["external_id"], row["timestamp"], _stored_text(row["content"]))
        for row in message_rows(conn, conversation_id, kinds)
    )


def _parsed_prompt_keys(conversation) -> set[tuple]:
    """Message keys of a parsed conversation's prompts."""
    return _message_keys(
        ("prompt", p.external_id, p.timestamp, _parsed_text(p.content)) for p in conversation.prompts
    )


def _parsed_message_keys(conversation) -> set[tuple]:
    """Message keys of a parsed conversation's prompts and responses."""
    responses = [*conversation.preamble, *(r for p in conversation.prompts for r in p.responses)]
    return _parsed_prompt_keys(conversation) | _message_keys(
        ("response", r.external_id, r.timestamp, _parsed_text(r.content)) for r in responses
    )


def _holds_transcript_of(conn, conversation_id: str, conversation) -> bool:
    """Whether a stored row holds ``conversation``'s transcript rather than another's.

    Every prompt the row stores must be one of this parse's prompts, matched by
    message id, by text, or (for a prompt with neither) by kind and timestamp (see
    _message_key). The file is append-only, so a row a previous release stored
    from it holds a subset of its prompts, and a row from a different file holds
    at least one prompt this parse does not have. A row with no prompt shares none
    to test, so it is not shown to hold anything.

    Prompts are the evidence because they are what the user typed. Responses are
    not: a session's replies are often generic text that another session shares
    too, so a shared reply says nothing about which file a row came from.
    """
    stored = _stored_message_keys(conn, conversation_id, ("prompt",))
    return bool(stored) and stored <= _parsed_prompt_keys(conversation)


def _stores_all_of(conn, conversation_id: str, conversation) -> bool:
    """Whether a stored row holds every prompt ``conversation`` has (every message, for a parse with no prompt).

    The converse of _holds_transcript_of. Prompts decide it for the reason given
    there; equal prompts show two transcripts agree, not which file the row was
    read from.
    """
    parsed = _parsed_prompt_keys(conversation)
    if parsed:
        return parsed <= _stored_message_keys(conn, conversation_id, ("prompt",))
    return _parsed_message_keys(conversation) <= _stored_message_keys(conn, conversation_id)


def _superseded_conversation(conn, harness_id, conversation) -> tuple[dict, str] | None:
    """A stored row this conversation may adopt, and the superseded or inherited key it was found under.

    Only that key's queued tags belong to the adoption: a candidate that was
    refused names a different file, so its queue stays where it is.

    A key names a file only as well as its spelling does (a sessionId shared by
    several chat files, a relative path), so a key alone cannot adopt a row: the
    row must also hold this conversation's transcript (see _holds_transcript_of).
    That is candidate evidence, not identity; :func:`_adoption_is_ambiguous`
    decides whether it is enough.
    """
    for key in (*conversation.superseded_external_ids, *conversation.inherited_external_ids):
        found = find_conversation_by_external_id(conn, harness_id, key)
        if found and _holds_transcript_of(conn, found["id"], conversation):
            return found, key
    return None


def _adoption_is_ambiguous(target: _Target, conversation) -> bool:
    """Whether a candidate row may be another live conversation's, so adopting it would move that one's tags.

    A superseded key (aider's path spelling, gemini_cli's shared sessionId) is
    retired: no current parse produces it, so a row holding this transcript is
    an earlier read of this file. An inherited key (codex_cli's restated parent
    id) is the parent's own key too, and a fork holds its parent's prompts, so
    containment, even of equal prompt sets, cannot tell the parent's row from
    one a previous release stored for the fork. The row is then this
    conversation's only if it is a read of this file: codex bounds are the
    file's own line times, and a fork's file begins at the fork, after its
    parent began. Start instant does not decide a superseded key: a legacy
    gemini row carries a parse-time stamp no later parse reproduces.
    """
    if target.superseded_key not in conversation.inherited_external_ids:
        return False
    return _instant(target.started_at) != _instant(conversation.started_at)


def _claimant_conversations(adapter, paths: list[str]) -> list[Conversation | None]:
    """What the other paths claiming a row parse to (every session, for a session source); None for one that cannot be read.

    Both questions a claimed row raises (is this file a copy of a claimant, does
    a claimant also hold the row's transcript) are answered from these parses.
    """
    session = getattr(adapter, "DEDUP_STRATEGY", "file") == "session"
    parsed: list[Conversation | None] = []
    for path in paths:
        source = Source(kind="file", location=Path(path))
        try:
            if session:
                parsed.extend(_parse_source_conversations(source, adapter, path))
            else:
                parsed.append(_parse_source_conversation(source, adapter, path))
        except Exception:
            parsed.append(None)
    return parsed


def _is_a_copy(conn, row_id: str, conversation, claimants: list[Conversation | None]) -> bool:
    """Whether this file is a second copy of what a claimant holds in ``row_id``.

    A claimant is a copy of this file only if it holds this conversation's
    identity and the row actually stores its transcript (see _stores_all_of). A
    shared identity alone shows two files are copies of each other, not that
    either was stored: a backup of a forked child can claim the row holding only
    the parent's history, and settling against it would leave the child with no
    conversation at all. A claimant with a different identity owns a different
    conversation (a forked child filed under its parent's id by a legacy
    collision), so storing this file under its own key is lossless. A claimant
    that cannot be parsed is not shown to differ, so it keeps the claim as a copy.
    """
    return any(
        other is None
        or (other.external_id == conversation.external_id and _stores_all_of(conn, row_id, other))
        for other in claimants
    )


def _split_moves_assignments(conn, row_id: str, conversation, claimants: list[Conversation | None]) -> bool:
    """Whether a row other paths claim gives its assignments up to ``conversation``.

    They move only when the row's transcript is this conversation's and no
    claimant's: a claimant that also holds it (a parent and its child share a
    prefix), or one that cannot be read, makes the row ambiguous, and an
    ambiguous row keeps its assignments.
    """
    if not _holds_transcript_of(conn, row_id, conversation):
        return False
    return all(other is not None and not _holds_transcript_of(conn, row_id, other) for other in claimants)


def _split_carryover(conn, conversation_id: str) -> ConversationCarryover:
    """Release the carried assignments from a row that stays, to re-attach them beside it."""
    return release_carryover(conn, conversation_id, snapshot_conversation(conn, conversation_id))


def _get_single_conversation(
    conversations: list, source_path: str, adapter_name: str = "this adapter"
):
    """Enforce 0/1 conversation per source, for a file-strategy adapter.

    Returns None if the list is empty.
    Raises AdapterParseError when a source yields multiple conversations.

    The message names the strategy, because the constraint belongs to it and
    not to ingest. It used to read "ingest currently requires exactly one
    conversation per source", which is a statement about a limitation rather
    than about a choice the adapter made — and it sent diagnosis toward a
    schema change for the whole life of the one built-in that tripped it. The
    remedy is one attribute away and worth saying out loud (#36).
    """
    if not conversations:
        return None
    if len(conversations) > 1:
        raise AdapterParseError(
            f"Source {source_path} yielded {len(conversations)} conversations, but "
            f"{adapter_name} declares DEDUP_STRATEGY = 'file'. A source that holds "
            "or grows several conversations belongs on DEDUP_STRATEGY = 'session'."
        )
    return conversations[0]


def _parse_source_conversation(
    source: Source,
    adapter: AdapterModule,
    source_path: str,
    stats: IngestStats | None = None,
):
    """Parse a source and enforce the file-strategy contract (0/1 conversation).

    For file-strategy adapters one file maps to exactly one conversation, so a
    multi-conversation parse is an adapter bug worth surfacing.
    """
    if not adapter.can_handle(source):
        raise AdapterParseError(
            f"Adapter {adapter.NAME} cannot handle source {source_path}"
        )
    return _get_single_conversation(
        _parse_source(adapter, source, stats), source_path, adapter.NAME
    )


def _parse_source_conversations(
    source: Source,
    adapter: AdapterModule,
    source_path: str,
    stats: IngestStats | None = None,
) -> list:
    """Parse a session-strategy source into all of its conversations.

    Unlike :func:`_parse_source_conversation` (file strategy, 0/1), a session
    source may legitimately yield many conversations — e.g. an OpenCode
    SQLite DB or an aider history file, each holding one per session. Each is
    stored and deduped independently by external_id.
    """
    if not adapter.can_handle(source):
        raise AdapterParseError(
            f"Adapter {adapter.NAME} cannot handle source {source_path}"
        )
    return _parse_source(adapter, source, stats)


def _normalize_status(status: str) -> tuple[str, str | None]:
    """Normalize status string into (kind, reason)."""
    if status.startswith("error:"):
        reason = status.split(":", 1)[1].strip()
        return "error", reason or None
    if status.startswith("skipped"):
        if status == "skipped":
            return "skipped", "unchanged"
        if status.startswith("skipped (") and status.endswith(")"):
            return "skipped", status[9:-1]
        reason = status[len("skipped"):].strip()
        if reason.startswith("(") and reason.endswith(")"):
            reason = reason[1:-1]
        return "skipped", reason or None
    return status, None


def _extract_first_text(blocks: list) -> str | None:
    """Return the first non-empty text block content."""
    for block in blocks:
        block_type = getattr(block, "block_type", None)
        if block_type != "text":
            continue
        content = getattr(block, "content", None)
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _truncate_summary(text: str, limit: int = 80) -> str:
    """Truncate summary text to a fixed length."""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _summarize_conversation(conversation) -> dict:
    """Extract summary metadata from a conversation."""
    summary = None
    for prompt in conversation.prompts:
        summary = _extract_first_text(prompt.content)
        if summary:
            break
    if not summary:
        for prompt in conversation.prompts:
            for response in prompt.responses:
                summary = _extract_first_text(response.content)
                if summary:
                    break
            if summary:
                break
    if summary:
        summary = " ".join(summary.split())
        summary = _truncate_summary(summary)

    models = [
        response.model
        for prompt in conversation.prompts
        for response in prompt.responses
        if response.model
    ]
    model = Counter(models).most_common(1)[0][0] if models else None

    return {
        "workspace_path": conversation.workspace_path,
        "summary": summary,
        "exchange_count": conversation.turn_count,
        "model": model,
    }


def ingest_all(
    conn: sqlite3.Connection,
    adapters: list[AdapterModule],
    *,
    on_file: Callable[[Source, str], None] | None = None,
    on_event: Callable[[IngestEvent], None] | None = None,
    filter_binary: bool | None = None,
    reparse: bool = False,
    force: bool = False,
) -> IngestStats:
    """Discover and ingest all new files from all adapters.

    Handles two dedup strategies:
    - "file": one conversation per file, skip if file already ingested
    - "session": a container of conversations, each replaced when it moves
      (see `_session_should_replace`)

    Either way, each conversation is stored through one plan and one executor
    (:func:`_plan`, :func:`_execute`); the branches differ only in when a source
    is eligible and what bookkeeping row it writes.

    Args:
        conn: Database connection
        adapters: List of adapter modules
        on_file: Optional callback for progress reporting
        on_event: Optional callback for structured progress events
        filter_binary: If True, filter binary content from tool results.
            If None (default), reads from config (ingestion.filter_binary).
        reparse: Re-parse every source already ingested, even when its stat and
            hash are unchanged, so a parser fix reaches stored rows. Each
            conversation goes through the ordinary replacement path, which
            carries its tags and ownership (see ``storage/replacement.py``).
        force: With ``reparse``, replace a conversation even when replacing it
            would lose user-applied tags. Without it those are skipped and
            reported in ``stats.skipped_for_tags``.

    Returns:
        IngestStats with counts
    """
    # Read filter_binary from config if not explicitly set
    if filter_binary is None:
        from siftd.config import get_ingestion_filter_binary

        filter_binary = get_ingestion_filter_binary()

    stats = IngestStats()
    # A module that decodes is given the interface derived for it (a no-op for
    # one that already has it, and for version 1).
    adapters = [adapt(a) for a in adapters]

    # Performance pragmas for bulk ingest:
    # - synchronous=OFF: skip fsync on commits (safe with WAL against process crashes)
    # - defer_foreign_keys: skip FK checks until commit (faster inserts)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA defer_foreign_keys = ON")

    sources = list(discover_all(adapters))
    stats.files_found = len(sources)

    totals: dict[str, int] = {}
    for source, adapter in sources:
        totals[adapter.NAME] = totals.get(adapter.NAME, 0) + 1

    seen: dict[str, int] = {}

    # Register tool aliases for each adapter (once per harness)
    registered_harnesses: set[str] = set()
    for adapter in adapters:
        harness_name = adapter.NAME
        if harness_name in registered_harnesses:
            continue
        aliases = getattr(adapter, "TOOL_ALIASES", None)
        if aliases:
            harness_kwargs = {}
            if hasattr(adapter, "HARNESS_SOURCE"):
                harness_kwargs["source"] = adapter.HARNESS_SOURCE
            if hasattr(adapter, "HARNESS_LOG_FORMAT"):
                harness_kwargs["log_format"] = adapter.HARNESS_LOG_FORMAT
            if hasattr(adapter, "HARNESS_DISPLAY_NAME"):
                harness_kwargs["display_name"] = adapter.HARNESS_DISPLAY_NAME
            harness_id = get_or_create_harness(conn, harness_name, **harness_kwargs)
            ensure_tool_aliases(conn, harness_id, aliases)
            conn.commit()
        registered_harnesses.add(harness_name)

    # Workspace identity lookups are cached across the run to avoid repeated git subprocess calls.
    run = _Run(stats=stats, filter_binary=filter_binary, reparse=reparse, force=force)

    for source, adapter in sources:
        file_path = str(source.location)
        harness_name = adapter.NAME
        dedup_strategy = getattr(adapter, "DEDUP_STRATEGY", "file")
        seen[harness_name] = seen.get(harness_name, 0) + 1
        index = seen[harness_name]
        total = totals.get(harness_name)

        def emit_event(status_raw: str, conversation=None) -> None:
            if not on_event:
                return
            status, reason = _normalize_status(status_raw)
            meta = (
                _summarize_conversation(conversation)
                if conversation is not None and status != "skipped"
                else {}
            )
            event = IngestEvent(
                adapter=harness_name,
                status=status,
                reason=reason,
                path=file_path,
                index=index,
                total=total,
                workspace_path=meta.get("workspace_path"),
                summary=meta.get("summary"),
                exchange_count=meta.get("exchange_count"),
                model=meta.get("model"),
                error=reason if status == "error" else None,
                **{
                    key: stats.by_harness[harness_name][key] - ledger_before[key]
                    for key in LEDGER_COUNTS
                },
            )
            on_event(event)

        # What this file has already been measured as, for whoever needs it
        # below — the error handlers reuse it rather than re-stat and re-hash.
        # Reset per source: these are loop-scoped names in Python, so a value
        # left by the previous file would otherwise be stamped onto this one.
        st: _StatLike | None = None
        current_hash: str | None = None

        # Initialize per-harness stats
        if harness_name not in stats.by_harness:
            stats.by_harness[harness_name] = {
                "conversations": 0,
                "prompts": 0, "responses": 0, "tool_calls": 0,
                "replaced": 0, "errors": 0,
                **dict.fromkeys(LEDGER_COUNTS, 0),
            }
        ledger_before = {key: stats.by_harness[harness_name][key] for key in LEDGER_COUNTS}
        try:
            if dedup_strategy == "file":
                existing_info = get_ingested_file_info(conn, file_path)
                location = source.as_path
                st = location.stat()
                # Fast path: stat-only skip (no file I/O for hashing). --reparse
                # skips neither check: the point is to re-parse a source whose
                # bytes did not move.
                if existing_info and not reparse and _stat_unchanged(existing_info, st):
                    _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                    continue
                current_hash = compute_file_hash(location)
                if existing_info and not reparse and _hash_unchanged(existing_info, current_hash):
                    # Content unchanged, mtime drifted — update stored stat
                    update_file_stat(conn, file_path, st.st_mtime, st.st_size)
                    conn.commit()
                    _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                    continue
                if existing_info and reparse:
                    stats.reparsed += 1
                outcome, conversation = _ingest_file(conn, source, adapter, run, existing_info, current_hash, st)

            elif dedup_strategy == "session":
                existing_file_info = get_ingested_file_info(conn, file_path)
                change_files = _change_files(adapter, source)
                if existing_file_info:
                    st = _change_stat(change_files)
                    # --reparse skips the stat and hash fast paths, as in the
                    # file branch: every session the source holds is re-parsed.
                    if reparse:
                        stats.reparsed += 1
                    else:
                        if _stat_unchanged(existing_file_info, st):
                            _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                            continue
                        current_hash = _change_hash(change_files)
                        if _hash_unchanged(existing_file_info, current_hash):
                            update_file_stat(conn, file_path, st.st_mtime, st.st_size)
                            conn.commit()
                            _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                            continue
                # The marker is measured before the parse, so it describes what
                # was read. Measured after, a commit that lands during the parse
                # would be stamped as ingested without having been read, and the
                # next run would skip it.
                if st is None:
                    st = _change_stat(change_files)
                current_hash = current_hash or _change_hash(change_files)
                outcome, conversation = _ingest_session_source(conn, source, adapter, run, current_hash, st)
            else:
                continue

            if outcome.startswith("skipped"):
                _count_file_skipped(source, outcome, stats, on_file, emit_event)
            else:
                if on_file:
                    on_file(source, outcome)
                emit_event(outcome, conversation=conversation)
        except sqlite3.IntegrityError as e:
            conn.rollback()
            # The rollback erases any uncommitted vocab rows (models/providers
            # created lazily this file) from the DB but NOT from the module
            # caches; a later file would then reuse a now-dangling ULID as an FK
            # and fail spuriously. Clear the caches so they re-resolve from disk.
            clear_vocabulary_caches()
            error_msg = str(e)
            # Check specifically for duplicate conversation (harness_id, external_id)
            # SQLite format: "UNIQUE constraint failed: conversations.harness_id, conversations.external_id"
            is_duplicate_conversation = (
                "UNIQUE constraint failed: conversations.harness_id" in error_msg
                or "conversations.external_id" in error_msg
            )
            # The repair only makes sense for file-strategy sources: a
            # session-strategy marker is conversation_id=NULL by design (one
            # file, many sessions), so pointing it at a single conversation
            # would corrupt it. Those fall through to the error path unchanged.
            if is_duplicate_conversation and dedup_strategy == "file":
                # The plan resolves the file's own key under the write lock, so
                # this is reached only by a fault: the conversation this file
                # describes already exists. That is not a
                # failure — it is a no-op plus a bookkeeping repair. Whatever
                # happens below, this path must never reach _record_file_error,
                # which NULLs the conversation pointer: discarding the only link
                # to the winner's conversation is exactly the corruption that
                # made this file un-ingestable forever (kgruel/siftd#29).
                try:
                    conv = _parse_source_conversation(source, adapter, file_path)
                    existing = None
                    if conv is not None:
                        h_id = harness_id_for_conversation(conn, conv)
                        existing = find_conversation_by_external_id(conn, h_id, conv.external_id)
                    if existing:
                        location = source.as_path
                        # Upsert, not insert: the conn.rollback() above has
                        # resurrected any ingested_files row this file's
                        # transaction had deleted (the re-ingest branch DELETEs
                        # the old row before re-storing), so a row is normally
                        # present here — carrying a stale hash and possibly a
                        # stale error. Re-point it either way.
                        prior = get_ingested_file_info(conn, file_path)
                        if ingested_paths_for_conversation(conn, existing["id"], excluding=file_path):
                            # A second copy of a tracked transcript — the same
                            # situation the replace path settles, reached from
                            # the other direction, so it settles the same way.
                            _settle_duplicate_path(
                                conn, file_path, existing["id"],
                                compute_file_hash(location), location.stat(),
                                external_id=conv.external_id,
                            )
                        else:
                            # A lost race, or a row poisoned by an older siftd:
                            # the conversation holds some OTHER read of this
                            # path. Point the row at it — never NULL — but do
                            # not claim the bytes just hashed were ingested,
                            # because they were not. Keep whatever hash/stat the
                            # row already asserted (a never-matching sentinel
                            # when there is no row) so the next run re-hashes,
                            # takes the re-ingest branch with a non-NULL pointer,
                            # and converges on the current content instead of
                            # freezing silently on the winner's.
                            link_ingested_file(
                                conn, file_path,
                                prior["file_hash"] if prior else "",
                                existing["id"],
                                file_mtime=prior["file_mtime"] if prior else None,
                                file_size=prior["file_size"] if prior else None,
                            )
                            conn.commit()
                        _count_file_skipped(
                            source, "skipped (duplicate)", stats, on_file, emit_event
                        )
                        continue
                    logger.warning(
                        f"Duplicate-conversation collision on {file_path} but the "
                        "conversation could not be resolved; leaving bookkeeping untouched"
                    )
                except Exception as repair_error:  # noqa: BLE001 — reported, never re-raised
                    conn.rollback()
                    logger.warning(
                        f"Could not repair duplicate-conversation collision on "
                        f"{file_path}: {repair_error}"
                    )
                # Count the run as errored without touching the row: the
                # pointer we could not confirm is worth more than the marker.
                _count_file_error(
                    source, adapter, error_msg, stats, on_file, emit_event
                )
                continue
            # Other IntegrityError (not duplicate conversation) — record as error
            _record_file_error(
                conn, source, adapter, file_path, error_msg, stats, on_file, emit_event,
                known_hash=current_hash, known_stat=st,
            )

        except Exception as e:
            conn.rollback()
            # See the IntegrityError handler above: clear vocab caches so a
            # rolled-back model/provider ULID is not reused as a dangling FK.
            clear_vocabulary_caches()
            _record_file_error(
                conn, source, adapter, file_path, str(e), stats, on_file, emit_event,
                known_hash=current_hash, known_stat=st,
            )

    # Rebuild the derived tier: usage_by_conv_model rollup, then conversation_stats
    # (its model/provider-dropped cache) re-derived from it.
    from siftd.storage.usage_rollup import rebuild_rollups

    rebuild_rollups(conn, commit=True)

    # Restore normal settings after bulk ingest
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA defer_foreign_keys = OFF")

    return stats


def _record_file_error(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    file_path: str,
    error: str,
    stats: IngestStats,
    on_file: Callable[[Source, str], None] | None,
    emit_event: Callable[[str, object | None], None] | None,
    *,
    known_hash: str | None = None,
    known_stat: _StatLike | None = None,
) -> None:
    """Record a file that failed ingestion, without discarding a live pointer.

    The failure is written to the row so it is queryable (``get_ingest_errors``,
    ``siftd doctor``) and so the next ingest re-examines the file whatever its
    stat says.

    ``known_hash`` / ``known_stat`` are what the caller already measured this
    run; they save a re-stat and a re-hash of a file the loop just read. What
    gets stamped is then the run's own reading rather than a fresher one — which
    is fine precisely because an errored row is re-examined regardless of its
    stat, so nothing here gates a later skip.

    The conversation link is *kept* whenever the row has one and that
    conversation still exists. It survives here because every caller rolled the
    transaction back first, which resurrects the conversation the re-ingest
    branch had already deleted — so "the parse failed" is not evidence that
    nothing belongs to this path, and NULLing the pointer would strand a live
    conversation exactly the way kgruel/siftd#29 describes. A path that never
    produced a conversation has nothing to keep and records NULL as before. The
    duplicate-conversation branch in :func:`ingest_all` does not route here at
    all; it repairs the pointer and reports through :func:`_count_file_error`.
    """
    try:
        existing = get_ingested_file_info(conn, file_path)
        location = source.as_path
        st = known_stat if known_stat is not None else location.stat()
        file_hash = known_hash if known_hash is not None else compute_file_hash(location)
        if existing:
            # Update existing row: set error, keep a pointer that still resolves
            keep = existing["conversation_id"]
            if keep is not None and not conversation_exists(conn, keep):
                keep = None
            conn.execute(
                """UPDATE ingested_files
                   SET file_hash = ?, conversation_id = ?, error = ?,
                       file_mtime = ?, file_size = ?
                   WHERE path = ?""",
                (file_hash, keep, error, st.st_mtime, st.st_size, file_path),
            )
            conn.commit()
        else:
            harness_kwargs = {}
            if hasattr(adapter, "HARNESS_SOURCE"):
                harness_kwargs["source"] = adapter.HARNESS_SOURCE
            harness_id = get_or_create_harness(conn, adapter.NAME, **harness_kwargs)
            record_failed_file(conn, file_path, file_hash, harness_id, error, file_mtime=st.st_mtime, file_size=st.st_size)
            conn.commit()
    except Exception:
        pass  # Don't fail the whole ingest because we couldn't record the error
    _count_file_error(source, adapter, error, stats, on_file, emit_event)


def _settle_duplicate_path(
    conn: sqlite3.Connection,
    file_path: str,
    conversation_id: str,
    file_hash: str,
    file_stat: os.stat_result,
    *,
    external_id: str | None,
) -> None:
    """Leave a second copy of a tracked transcript linked, named, and settled.

    Only one path can hold a session's ``(harness_id, external_id)`` slot and
    neither copy is more authoritative, so the loser records what it actually
    has — a link to the conversation that exists — rather than replacing it.
    Stamping this file's own hash is what stops the copies taking turns: it is
    the one case where the row may assert content it did not ingest, because
    there is no convergence to wait for (contrast the lost-race branch of the
    duplicate-collision handler, where re-hashing next run is the whole point).

    ``external_id`` names the shared session in the warning; with none (this
    file parsed to nothing) the conversation's id does.
    """
    named = f"external_id {external_id}" if external_id else f"conversation {conversation_id}"
    logger.warning(
        f"{file_path} duplicates a transcript already tracked under another path "
        f"({named}); its change was not ingested — the "
        "conversation stays as the path holding the slot left it"
    )
    link_ingested_file(
        conn, file_path, file_hash, conversation_id,
        file_mtime=file_stat.st_mtime, file_size=file_stat.st_size,
    )
    conn.commit()


def _count_file_skipped(
    source: Source,
    label: str,
    stats: IngestStats,
    on_file: Callable[[Source, str], None] | None,
    emit_event: Callable[[str, object | None], None] | None,
) -> None:
    """Report a file this run did not ingest. Sibling of :func:`_count_file_error`.

    The label is given once, so the counter, the per-file callback and the
    event stream cannot end up describing the skip differently — which is the
    only way the seven call sites ever diverged.
    """
    stats.files_skipped += 1
    if on_file:
        on_file(source, label)
    if emit_event:
        emit_event(label, None)


def _replacement_event_keys(conversation) -> frozenset[tuple[str, str]]:
    """``(event kind, external_id)`` of every event a parse will store.

    The same events :func:`store_conversation` writes, keyed the way
    :func:`~siftd.storage.replacement.unrestorable_user_tags` matches them. An
    event with no external_id is not a key: nothing can carry onto it. A
    response is also keyed by the ids of the chunks merged into it.
    """
    if conversation is None:
        return frozenset()
    responses = list(conversation.preamble) + [
        response for prompt in conversation.prompts for response in prompt.responses
    ]
    pairs = [("prompt", prompt.external_id) for prompt in conversation.prompts]
    pairs += [("response", response.external_id) for response in responses]
    pairs += [("response", merged) for response in responses for merged in merged_external_ids(response)]
    pairs += [
        ("tool_call", tool_call.external_id)
        for response in responses
        for tool_call in response.tool_calls
    ]
    return frozenset((kind, ext) for kind, ext in pairs if ext is not None)


@dataclass
class _Run:
    """What every source of one ingest run shares."""

    stats: IngestStats
    filter_binary: bool
    reparse: bool
    force: bool
    workspace_cache: dict[str, str] = field(default_factory=dict)


class _Target(NamedTuple):
    """The stored conversation a source's conversation is about to act on, and how it was found."""

    row: str
    own_key: bool = False
    """Stored under the conversation's own external_id, so nothing can be stored beside it."""
    pointed: bool = False
    """This source's bookkeeping names it, or names nothing (a pointer an older release discarded, kgruel/siftd#29)."""
    started_at: str | None = None
    ended_at: str | None = None
    superseded_key: str | None = None
    """The superseded or inherited key the row is stored under; its queued tags move with an adoption."""


def _find_target(conn, conversation, *, pointer: str | None, recorded: bool) -> _Target | None:
    """The stored conversation at stake for this parse, or None, read under the write lock.

    The row under the parse's own key comes first, since the conversation can
    only be stored in its place. Otherwise the bookkeeping pointer names it, and
    otherwise a row stored under a key the parse supersedes or inherits
    (:func:`_superseded_conversation`). A pointer names a row, not whose
    conversation it holds (a legacy collision pointed a fork at its parent's
    row), so a pointed row under such a key carries that key and its start too,
    and :func:`_adoption_is_ambiguous` judges it as it would a candidate.
    ``recorded`` says the source has a bookkeeping row at all.

    The ingest lock excludes other ingests, not ``siftd tag``, so the write lock
    is taken before anything is read: the assignments the tag gate counts and the
    snapshot carries are then read in the transaction that deletes them, and no
    row can appear between this lookup and the store.
    """
    _hold_write_lock(conn)
    if conversation is None:
        return _Target(pointer, pointed=True) if pointer else None
    harness_id = harness_id_for_conversation(conn, conversation)
    own = find_conversation_by_external_id(conn, harness_id, conversation.external_id)
    if own:
        pointed = own["id"] == pointer or (recorded and pointer is None)
        return _Target(own["id"], own_key=True, pointed=pointed, ended_at=own["ended_at"])
    if pointer:
        row = find_conversation_by_id(conn, pointer)
        key = row["external_id"] if row else None
        prior = (*conversation.superseded_external_ids, *conversation.inherited_external_ids)
        return _Target(
            pointer, pointed=True, started_at=row["started_at"] if row else None,
            superseded_key=key if key in prior else None,
        )
    superseded = _superseded_conversation(conn, harness_id, conversation)
    if superseded:
        row, key = superseded
        return _Target(row["id"], started_at=row["started_at"], superseded_key=key)
    return None


class _Action(Enum):
    STORE = "store"  # nothing is stored for this conversation yet
    REPLACE = "replace"  # the target is this source's alone: snapshot, delete, store, restore
    SPLIT = "split"  # another path claims the target; store beside it, taking what its transcript owns
    SETTLE = "settle"  # another path claims the target and this source is a copy of it: store nothing
    LINK = "link"  # a file's own key holds a row its bookkeeping does not name: point it there, store nothing
    REFUSE = "refuse"  # the target may be another conversation's: store beside it, move nothing
    DECLINE = "decline"  # --reparse: acting would lose user-applied tags; touch nothing


@dataclass(frozen=True)
class _Plan:
    """What storing one conversation does to the database, decided before anything is written."""

    action: _Action
    target: str | None = None
    superseded_key: str | None = None
    """REPLACE of a row found under a superseded key: that key's queued tags move to the conversation."""
    released: tuple[str, ...] = ()
    """REPLACE: other paths to this source (for a session source, other spellings) whose pointer is cleared first."""
    moves: bool = False
    """SPLIT: the target's transcript is this conversation's alone, so its assignments move."""


def _plan(conn, adapter, conversation, target: _Target | None, *, file_path: str, session: bool, run: _Run) -> _Plan:
    """Decide what storing ``conversation`` does to ``target``: the one place a plan is made.

    A row other paths point at cannot be deleted (``conversation_id`` is ``ON
    DELETE CASCADE``, so their bookkeeping would go with it). This source then
    settles as a copy of it, or stores beside it (taking the assignments its
    transcript owns); a row under the conversation's own key is always settled
    against, since storing beside it would collide. The exception is another
    path to this same file (a symlinked log directory), which is this source,
    not another: its pointer is released rather than honoured, or the row could
    never be replaced. A session source also releases its other spellings
    (:func:`~siftd.adapters.sdk.spells`): its bookkeeping never points at a
    conversation, so such a pointer is a file-strategy leftover (pre-#36 aider),
    and honouring it would freeze the session under a key no parse produces again.

    A file's own key can hold a row its bookkeeping does not name (a new file
    after the bookkeeping was cleared, a legacy collision). That row is linked,
    not replaced, keeping the hash the bookkeeping had, so the next run finds the
    pointer and replaces it; meanwhile another file can still claim the row's
    transcript from it.

    Every plan passes the ``--reparse`` tag gate here (:func:`_declined_for_tags`).
    """
    if target is None:
        plan = _Plan(_Action.STORE)
    elif target.superseded_key and _adoption_is_ambiguous(target, conversation):
        plan = _Plan(_Action.REFUSE, target.row)
    else:
        claimants = ingested_paths_for_conversation(conn, target.row, excluding=file_path)
        released = tuple(
            path for path in claimants if (spells(path, file_path) if session else same_file(path, file_path))
        )
        others = [path for path in claimants if path not in released]
        if others:
            parsed = _claimant_conversations(adapter, others)
            if conversation is None or target.own_key or _is_a_copy(conn, target.row, conversation, parsed):
                plan = _Plan(_Action.SETTLE, target.row)
            else:
                plan = _Plan(
                    _Action.SPLIT, target.row,
                    moves=_split_moves_assignments(conn, target.row, conversation, parsed),
                )
        elif target.own_key and not target.pointed and not session:
            plan = _Plan(_Action.LINK, target.row)
        else:
            plan = _Plan(_Action.REPLACE, target.row, target.superseded_key, released=released)
    if _declined_for_tags(conn, plan, conversation, file_path=file_path, run=run):
        plan = _Plan(_Action.DECLINE, plan.target)
    return plan


def _declined_for_tags(conn, plan: _Plan, conversation, *, file_path: str, run: _Run) -> bool:
    """The one tag-safety decision for ``--reparse``: True when the plan would lose user-applied tags.

    It counts what the plan removes and the new parse cannot re-attach, not what
    the row merely holds: a replacement removes every assignment on the row (all
    of them, conversation tags included, when the parse is empty), a split only
    the event assignments it releases, and the other actions remove nothing.
    ``tests/architecture/test_replacement_carry.py`` holds every door to a plan.
    """
    if plan.target is None or not run.reparse or run.force:
        return False
    keys = _replacement_event_keys(conversation)
    if plan.action is _Action.REPLACE:
        lost = unrestorable_user_tags(conn, plan.target, keys, replacement_exists=conversation is not None)
    elif plan.action is _Action.SPLIT and plan.moves:
        lost = unrestorable_user_tags(conn, plan.target, keys, row_is_kept=True)
    else:
        return False
    if not lost:
        return False
    run.stats.skipped_for_tags.append(TagSkip(path=file_path, conversation_id=plan.target, lost=lost))
    return True


def _execute(conn, adapter, conversation, plan: _Plan, *, file_path: str, run: _Run) -> str | None:
    """Apply a plan: the one door that removes a conversation or its assignments.

    Returns the stored conversation's id, or None when nothing was stored (a
    settled copy, a link, or a parse that yielded nothing). The caller writes the
    bookkeeping row and commits.
    """
    if plan.action in (_Action.SETTLE, _Action.LINK, _Action.DECLINE):
        return None
    carryover: ConversationCarryover | None = None
    if plan.action is _Action.REPLACE:
        assert plan.target is not None
        release_conversation_pointers(conn, plan.target, plan.released)
        carryover = _take_conversation_for_replacement(conn, plan.target, file_path)
        if plan.superseded_key and conversation is not None:
            rekey_pending_tags(conn, (plan.superseded_key,), conversation.external_id)
    elif plan.action is _Action.SPLIT and plan.moves:
        assert plan.target is not None
        carryover = _split_carryover(conn, plan.target)
    if conversation is None:
        # No replacement row to carry the snapshot to. Usually the file was
        # emptied, but a transcript rewritten in place can transiently parse to
        # nothing too, so say what was lost rather than dropping it silently.
        if carryover:
            logger.warning(f"{carryover.describe()} were dropped: {file_path} no longer parses to a conversation")
        return None
    conv_id = store_conversation(
        conn, conversation, filter_binary=run.filter_binary, _workspace_cache=run.workspace_cache
    )
    _restore_carryover_after_replacement(conn, conv_id, carryover, conversation.external_id)
    _apply_pending_tags(conn, adapter, conversation, conv_id)
    return conv_id


def _hold_write_lock(conn: sqlite3.Connection) -> None:
    """Take the database write lock now, if this connection does not hold it yet.

    The sqlite3 module opens its transaction only at the first write, so reads
    before it see no lock. ``in_transaction`` is True only once this connection
    has written (nothing in ingest issues a deferred BEGIN), and a write already
    holds the lock. It is held until the source's commit or rollback.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _take_conversation_for_replacement(
    conn: sqlite3.Connection,
    conversation_id: str,
    file_path: str,
) -> ConversationCarryover:
    """Snapshot the assignments a delete would destroy, then delete the conversation.

    One operation because the order is forced: ``UNIQUE(harness_id,
    external_id)`` means the old row must go before the new one lands, and an
    AFTER DELETE trigger means a post-delete re-point would match zero rows. The
    caller stores and restores in the same transaction.

    Only a REPLACE plan reaches here, and a plan replaces only a row no other
    path points at; the check is repeated because ``conversation_id`` is ``ON
    DELETE CASCADE``, so deleting a claimed row would take the other path's
    bookkeeping with it.
    """
    if ingested_paths_for_conversation(conn, conversation_id, excluding=file_path):
        raise RuntimeError(f"conversation {conversation_id} is claimed by another path; its plan is stale")
    snapshot = snapshot_conversation(conn, conversation_id)
    delete_conversation(conn, conversation_id)
    return snapshot


def _count_file_error(
    source: Source,
    adapter: AdapterModule,
    error: str,
    stats: IngestStats,
    on_file: Callable[[Source, str], None] | None,
    emit_event: Callable[[str, object | None], None] | None,
) -> None:
    """Report a failed file (stats + callbacks) without writing bookkeeping.

    The reporting half of :func:`_record_file_error`, split out for the caller
    that must surface a failure but must NOT touch the ingested_files row.
    """
    stats.files_errored += 1
    if adapter.NAME in stats.by_harness:
        harness_counts = stats.by_harness[adapter.NAME]
        harness_counts["errors"] = harness_counts.get("errors", 0) + 1
    if on_file:
        on_file(source, f"error: {error}")
    if emit_event:
        emit_event(f"error: {error}", None)


def _ingest_file(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    run: _Run,
    existing: dict | None,
    file_hash: str,
    file_stat: os.stat_result,
) -> tuple[str, Conversation | None]:
    """Store a file-strategy source that is new, changed, or reparsed; returns its outcome label."""
    file_path = str(source.location)
    harness_name = adapter.NAME
    conversation = _parse_source_conversation(source, adapter, file_path, run.stats)
    pointer = existing["conversation_id"] if existing else None
    target = _find_target(conn, conversation, pointer=pointer, recorded=existing is not None)
    plan = _plan(conn, adapter, conversation, target, file_path=file_path, session=False, run=run)
    if plan.action is _Action.DECLINE:
        # Nothing was written: release the write lock the lookup took, so a tag on
        # another connection is not held out until the next source commits.
        conn.rollback()
        clear_vocabulary_caches()  # a harness row created by the lookup was rolled back
        return "skipped (tags)", None
    if plan.action is _Action.SETTLE:
        assert plan.target is not None
        _settle_duplicate_path(
            conn, file_path, plan.target, file_hash, file_stat,
            external_id=conversation.external_id if conversation is not None else None,
        )
        return "skipped (duplicate)", None
    if plan.action is _Action.LINK:
        # Not claiming the bytes just hashed were ingested: the row keeps the hash
        # it had (a never-matching sentinel for a new file), so the next run looks again.
        assert plan.target is not None
        link_ingested_file(
            conn, file_path, existing["file_hash"] if existing else "", plan.target,
            file_mtime=existing["file_mtime"] if existing else None,
            file_size=existing["file_size"] if existing else None,
        )
        conn.commit()
        return "skipped (duplicate)", None
    if existing:
        clear_ingested_file_error(conn, file_path)  # the row is written again below
    conv_id = _execute(conn, adapter, conversation, plan, file_path=file_path, run=run)
    if conv_id is None:
        harness_kwargs = {"source": adapter.HARNESS_SOURCE} if hasattr(adapter, "HARNESS_SOURCE") else {}
        harness_id = get_or_create_harness(conn, harness_name, **harness_kwargs)
        record_empty_file(conn, file_path, file_hash, harness_id, file_mtime=file_stat.st_mtime, file_size=file_stat.st_size)
    else:
        record_ingested_file(conn, file_path, file_hash, conv_id, file_mtime=file_stat.st_mtime, file_size=file_stat.st_size)
    conn.commit()
    # Counted after the commit, so a rolled-back replacement is not reported.
    if conversation is not None:
        _count_stored(run.stats, harness_name, file_path, conversation, plan)
    if existing:
        run.stats.files_replaced += 1
        run.stats.by_harness[harness_name]["replaced"] += 1
        return "updated", conversation
    run.stats.files_ingested += 1
    return "ingested", conversation


def _ingest_session_source(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    run: _Run,
    file_hash: str,
    file_stat: _StatLike,
) -> tuple[str, Conversation | None]:
    """Store each conversation a session source holds, replacing the ones that moved; returns its outcome label.

    Each is deduped by external_id; only a moved one is replaced
    (:func:`_session_should_replace`). The ``ingested_files`` row is one hash/mtime marker with
    ``conversation_id = NULL``, so replacing a session does not cascade it away.
    """
    file_path = str(source.location)
    harness_name = adapter.NAME
    recorded = replace(
        source,
        metadata={**source.metadata, "recorded_locations": ingested_paths_for_harness(conn, harness_name)},
    )
    conversations = [
        _with_content_source(c, file_path)
        for c in _parse_source_conversations(recorded, adapter, file_path, run.stats)
    ]
    if not conversations:
        return "skipped (empty)", None

    harness_id = None
    declined = False
    # Counted once the container commits: it is one transaction, so a failure on
    # a later session rolls back every earlier one and their counts go with it.
    stored: list[tuple[Conversation, _Plan]] = []
    for conversation in conversations:
        harness_id = harness_id_for_conversation(conn, conversation)
        target = _find_target(conn, conversation, pointer=None, recorded=False)
        if target is not None and target.own_key and not _session_should_replace(
            conn, conversation, target.row, target.ended_at, reparse=run.reparse
        ):
            continue
        plan = _plan(conn, adapter, conversation, target, file_path=file_path, session=True, run=run)
        if plan.action is _Action.DECLINE:
            declined = True
            continue
        if plan.action is _Action.SETTLE:
            logger.warning(
                f"{conversation.external_id} in {file_path} is held by a row another path claims; "
                "its change was not ingested"
            )
            continue
        if _execute(conn, adapter, conversation, plan, file_path=file_path, run=run) is not None:
            stored.append((conversation, plan))

    assert harness_id is not None  # conversations is non-empty
    record_session_file(
        conn, file_path, file_hash, harness_id, file_mtime=file_stat.st_mtime, file_size=file_stat.st_size,
    )
    conn.commit()
    for conversation, plan in stored:
        _count_stored(run.stats, harness_name, file_path, conversation, plan)
        if plan.action is _Action.REPLACE:
            run.stats.by_harness[harness_name]["replaced"] += 1

    # Ingested if any new session arrived, else replaced if any moved.
    last = stored[-1][0] if stored else None
    if any(plan.action is not _Action.REPLACE for _, plan in stored):
        run.stats.files_ingested += 1
        return "ingested", last
    if stored:
        run.stats.files_replaced += 1
        return "replaced", last
    return ("skipped (tags)" if declined else "skipped (older)"), None


def _count_stored(stats: IngestStats, harness_name: str, file_path: str, conversation, plan: _Plan) -> None:
    """Count a conversation a committed plan stored."""
    _update_stats_for_conversation(stats, harness_name, conversation)
    if plan.action is _Action.REPLACE:
        stats.conversations_replaced += 1
    elif plan.action is _Action.REFUSE:
        assert plan.target is not None
        stats.refused_adoptions.append(
            RefusedAdoption(harness_name, file_path, plan.target, conversation.external_id)
        )


def _update_stats_for_conversation(
    stats: IngestStats,
    harness_name: str,
    conversation,
) -> None:
    """Update stats counters for a conversation."""
    stats.conversations += 1
    stats.by_harness[harness_name]["conversations"] += 1

    def count_response(response) -> None:
        stats.responses += 1
        stats.by_harness[harness_name]["responses"] += 1
        stats.tool_calls += len(response.tool_calls)
        stats.by_harness[harness_name]["tool_calls"] += len(response.tool_calls)

    # A preamble's responses came before any prompt. They are stored, so they are counted.
    for response in conversation.preamble:
        count_response(response)
    for prompt in conversation.prompts:
        stats.prompts += 1
        stats.by_harness[harness_name]["prompts"] += 1
        for response in prompt.responses:
            count_response(response)


def _restore_carryover_after_replacement(
    conn: sqlite3.Connection,
    conversation_id: str,
    snapshot: ConversationCarryover | None,
    external_id: str,
) -> None:
    """Re-point a snapshot at the replacement rows, reporting what was lost."""
    restore_and_report(
        conn, conversation_id, snapshot, subject=external_id, operation="re-ingest"
    )


def _session_key_candidates(adapter: AdapterModule, external_id: str) -> list[str]:
    """Return the session keys a conversation may have been queued/registered under.

    `siftd tag --session <id>` and `siftd register` both key off the bare
    harness session id the tool reports (a uuid, for claude_code). Adapters,
    however, are free to namespace their `external_id`; claude_code builds
    `claude_code::<uuid>` (and `claude_code::<uuid>::agent::<agent-id>` for
    subagents), so the conversation's external_id never equals the queue key.

    Ordered, deduped candidates:

    1. `external_id` itself — adapters whose external_id *is* the bare session
       id (the pre-existing behavior).
    2. The parent form for subagents (`...::agent::<id>` stripped) — also
       pre-existing, and correct for a bare-external_id adapter.
    3. The bare form: the adapter-name prefix stripped off the parent form.
       For claude_code that is `<uuid>` for both plain and subagent
       conversations — never `<uuid>::agent::<id>`, which is not a key any
       write path produces. The prefix is matched against `adapter.NAME`
       rather than split on the first `::` so an adapter that uses `::` for
       something else is left alone.
    """
    parent = external_id.split("::agent::")[0]
    prefix = f"{getattr(adapter, 'NAME', '')}::"
    candidates = [external_id, parent]
    if prefix != "::" and parent.startswith(prefix):
        candidates.append(parent[len(prefix):])
    # dict.fromkeys preserves order while dropping the duplicate that a
    # non-subagent external_id (external_id == parent) produces.
    return list(dict.fromkeys(candidates))


def _parent_conversation_exists(
    conn: sqlite3.Connection,
    conversation,
    candidates: list[str],
) -> bool:
    """Has the parent transcript of this subagent conversation been ingested?

    ``candidates[1]`` is the parent's own ``external_id``, so the ordinary
    answer is one indexed lookup on ``UNIQUE(harness_id, external_id)``.
    :func:`resolve_session_conversation` answers the same question in general —
    for an adapter whose parent key is not its external_id — but its suffix
    match is unindexable, so it full-scans ``conversations``. Asking it first
    cost that scan for every subagent conversation at every call site, which on
    a corpus with thousands of subagent transcripts is most of a rebuild's
    ingest time. Exact probe first, general form only on a miss: same answer,
    one index seek in the steady state.
    """
    harness_id = harness_id_for_conversation(conn, conversation)
    if find_conversation_by_external_id(conn, harness_id, candidates[1]):
        return True
    return any(resolve_session_conversation(conn, key) for key in candidates[1:])


def _apply_pending_tags(
    conn: sqlite3.Connection,
    adapter: AdapterModule,
    conversation,
    conversation_id: str,
) -> int:
    """Apply pending tags for a session and unregister it.

    Only applies to adapters with SUPPORTS_LIVE_REGISTRATION = True.
    Returns the number of tags applied.
    """
    if not getattr(adapter, "SUPPORTS_LIVE_REGISTRATION", False):
        return 0

    session_id = conversation.external_id
    candidates = _session_key_candidates(adapter, session_id)

    # A subagent transcript shares its session's key forms with the parent
    # transcript, so whichever file ingest reaches first would take the
    # session's queued tags. resolve_session_conversation (the recovery path)
    # deliberately excludes subagent rows so a session-level tag lands on the
    # parent; the drain agrees with it here, or one queued row resolves to two
    # different conversations depending on ingest order. Rows are left queued
    # when the parent exists — its own ingest, or `siftd doctor fix
    # --pending-tags`, lands them on the parent.
    drain_keys = candidates
    if "::agent::" in session_id and _parent_conversation_exists(
        conn, conversation, candidates
    ):
        drain_keys = candidates[:1]

    # Drain every key form, not just the first with rows: an agent tagging via
    # `--current` queues under the adapter-prefixed id the session-start hook
    # registered, while `siftd tag --session <uuid>` queues under the bare one.
    # Both name this session, so both belong to this drain.
    applied, unresolved = drain_pending_tags(conn, drain_keys, conversation_id)

    for a in applied:
        logger.debug(
            f"Applied tag '{a.tag_name}' to {a.target_kind} {a.target_id[:12]} "
            f"(session {a.harness_session_id})"
        )
    for u in unresolved:
        # Left queued, not dropped — the target may exist by the next ingest.
        logger.warning(
            f"Tag '{u.tag_name}' for session {u.harness_session_id} still queued: "
            f"{u.reason}"
        )

    # Unregister exactly what was drained. Sessions are registered under
    # whichever key form the harness reported — the bare one from `siftd
    # register`, the adapter-prefixed one from the shipped session-start hook —
    # so every drained key has to be cleared or the row outlives its ingest.
    # But a key deliberately *excluded* from the drain keeps its registration:
    # the subagent narrowing above leaves the parent's rows queued for the
    # parent's own ingest, and unregistering the parent would tell the recovery
    # path those rows are orphaned. It would then apply them against whatever
    # the parent transcript held at that moment — the very "resolve a
    # `--last-*` marker against a mid-flight transcript" the registration
    # exists to prevent.
    for key in drain_keys:
        unregister_session(conn, key)
    return len(applied)
