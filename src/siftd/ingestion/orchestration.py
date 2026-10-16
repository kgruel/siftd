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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from siftd.adapters.records import CONTENT_REVISION_ATTRIBUTE, CONTENT_SOURCE_ATTRIBUTE
from siftd.adapters.sdk import AdapterParseError
from siftd.dateparse import to_utc
from siftd.domain import Conversation, Source
from siftd.storage.attributes import get_attribute
from siftd.storage.replacement import (
    ConversationCarryover,
    release_carryover,
    restore_and_report,
    snapshot_conversation,
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
    delete_conversation,
    ensure_tool_aliases,
    find_conversation_by_external_id,
    get_ingested_file_info,
    get_or_create_harness,
    harness_id_for_conversation,
    link_ingested_file,
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    record_session_file,
    store_conversation,
    update_file_stat,
)

from .discovery import discover_all

logger = logging.getLogger(__name__)

# Returned by _ingest_file and _reingest_file when the file settled as a second
# copy of a legacy row that another path claims. Nothing was stored; the caller
# counts the file as a skipped duplicate.
_SETTLED_DUPLICATE = object()

if TYPE_CHECKING:
    from siftd.ingestion import AdapterModule


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
    by_harness: dict = field(default_factory=dict)


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


def _session_should_replace(
    new_ended: str | None,
    existing_ended: str | None,
    *,
    revision_moved: bool = False,
) -> bool:
    """Whether a re-parsed session should replace the copy already stored.

    `ended_at` is this branch's per-conversation change detector: a container
    source re-parses every session it holds, and only the ones that actually
    moved are worth the delete-then-insert (which costs new ULIDs, a tag
    snapshot/restore, and the conversation's ownership rows). An end time cannot
    show every change, so a session whose content revision moved within its own
    source (see :func:`_revision_moved`) replaces whatever its end time says.

    A conversation reporting no `ended_at` is not "unchanged" — it is a
    session with no detector at all, and `newer than` is false forever, which
    is what froze every aider session at its first ingest (#36). We only reach
    here because the file's content hash changed, so the honest reading of
    "no detector" is that the re-parsed file wins.
    """
    if revision_moved or new_ended is None:
        return True
    return _compare_timestamps(new_ended, existing_ended)


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


def _stored_text(raw: str | None) -> str | None:
    """The text of a stored first content block (``json.dumps`` of its dict), or None."""
    if not raw:
        return None
    try:
        text = json.loads(raw).get("text")
    except (ValueError, AttributeError):
        return None
    return text if isinstance(text, str) and text else None


def _parsed_text(content) -> str | None:
    """The text of a parsed message's first content block, or None."""
    if not content or not isinstance(content[0].content, dict):
        return None
    text = content[0].content.get("text")
    return text if isinstance(text, str) and text else None


def _stored_message_keys(
    conn, conversation_id: str, kinds: tuple[str, ...] = ("prompt", "response")
) -> set[tuple]:
    """Message keys of a stored conversation's prompts and/or responses."""
    placeholders = ",".join("?" * len(kinds))
    return _message_keys(
        (row["kind"], row["external_id"], row["timestamp"], _stored_text(row["content"]))
        for row in conn.execute(
            "SELECT e.kind, e.external_id, e.timestamp, ec.content FROM events e "
            "LEFT JOIN event_content ec ON ec.event_id = e.id AND ec.block_index = 0 "
            f"WHERE e.conversation_id = ? AND e.kind IN ({placeholders})",
            (conversation_id, *kinds),
        )
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
    """Whether a stored row holds every prompt ``conversation`` has: its transcript is in the row.

    The converse of _holds_transcript_of, which asks that the row has nothing this
    parse lacks. Together they say the row stores this transcript, rather than a
    prefix of it (a parent's history under a forked child). Prompts decide it for
    the reason given there; a parse with no prompt falls back to its message keys.
    """
    parsed = _parsed_prompt_keys(conversation)
    if parsed:
        return parsed <= _stored_message_keys(conn, conversation_id, ("prompt",))
    return _parsed_message_keys(conversation) <= _stored_message_keys(conn, conversation_id)


def _superseded_conversation(conn, harness_id, conversation) -> tuple[dict, str] | None:
    """The stored row this conversation replaces, and the superseded key it was found under.

    Only that key's queued tags belong to the replacement: a candidate that was
    refused names a different file, so its queue stays where it is.

    An adapter that re-keys its conversations names the old keys on the
    conversation, and a key names a file only as well as its spelling does (a
    sessionId shared by several chat files, a forked child and its parent under
    the parent's legacy id, a relative path), so a key alone cannot adopt a row.
    The row must also hold this conversation's transcript (see
    _holds_transcript_of). Start instant is not the discriminator: a row a
    previous release stored for a file without startTime carries a parse-time
    stamp that no later parse reproduces, and sessions opened in the same second
    share it.
    """
    for key in conversation.superseded_external_ids or ():
        found = find_conversation_by_external_id(conn, harness_id, key)
        if found and _holds_transcript_of(conn, found["id"], conversation):
            return found, key
    return None


def _recorded_locations(conn, harness_name: str) -> list[str]:
    """The paths earlier ingests recorded for this harness, as each was spelled.

    A session-strategy source is keyed by how its path was spelled when it was
    scanned, and a relative spelling names a file only relative to the directory
    it was scanned from. Nothing records that directory, but the bookkeeping does
    record the spelling: every marker is written under the path as scanned. The
    adapter reads these as candidates for its legacy ids (see
    ``Source.metadata["recorded_locations"]``), and ingest verifies any row they
    name against the file's own prompts before adopting it.
    """
    return [
        row["path"]
        for row in conn.execute(
            "SELECT f.path FROM ingested_files f JOIN harnesses h ON h.id = f.harness_id WHERE h.name = ?",
            (harness_name,),
        )
    ]


def _claimant_conversations(
    conn, adapter, conversation_id: str, file_path: str
) -> Iterator[Conversation | None]:
    """What each other path claiming a row parses to; None for one that cannot be read.

    Both questions a refused claim raises (is this file a copy of the claimant, or
    does the claimant also hold the row's transcript) are answered from the
    claimant's own parse, so the claimants are read in one place.
    """
    paths = [
        row["path"]
        for row in conn.execute(
            "SELECT path FROM ingested_files WHERE conversation_id = ? AND path != ?",
            (conversation_id, file_path),
        )
    ]
    for path in paths:
        try:
            yield _parse_source_conversation(Source(kind="file", location=Path(path)), adapter, path)
        except Exception:
            yield None


def _split_carryover(
    conn, adapter, conversation_id: str, file_path: str, conversation
) -> ConversationCarryover | None:
    """The assignments a row another path claims gives up to ``conversation``, when they are its transcript's.

    A refused replacement leaves the row where it is, for its claimant, so its tags
    stay on the row unless they move. They move only when the row's transcript is
    this conversation's and no claimant's: a claimant that also holds the row's
    transcript makes the row ambiguous (a parent and its child share a prefix), and
    an ambiguous row keeps its assignments. Ownership is decided by content, never
    by who holds the claim. Returns None when nothing moves.
    """
    if not _holds_transcript_of(conn, conversation_id, conversation):
        return None
    for other in _claimant_conversations(conn, adapter, conversation_id, file_path):
        # An unreadable claimant may hold the row too: keep the assignments.
        if other is None or _holds_transcript_of(conn, conversation_id, other):
            return None
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
        list(adapter.parse(source)), source_path, adapter.NAME
    )


def _parse_source_conversations(
    source: Source,
    adapter: AdapterModule,
    source_path: str,
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
    return list(adapter.parse(source))


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
        "exchange_count": len(conversation.prompts),
        "model": model,
    }


def ingest_all(
    conn: sqlite3.Connection,
    adapters: list[AdapterModule],
    *,
    on_file: Callable[[Source, str], None] | None = None,
    on_event: Callable[[IngestEvent], None] | None = None,
    filter_binary: bool | None = None,
) -> IngestStats:
    """Discover and ingest all new files from all adapters.

    Handles two dedup strategies:
    - "file": one conversation per file, skip if file already ingested
    - "session": a container of conversations, each replaced when it moves
      (see `_session_should_replace`)

    Args:
        conn: Database connection
        adapters: List of adapter modules
        on_file: Optional callback for progress reporting
        on_event: Optional callback for structured progress events
        filter_binary: If True, filter binary content from tool results.
            If None (default), reads from config (ingestion.filter_binary).

    Returns:
        IngestStats with counts
    """
    # Read filter_binary from config if not explicitly set
    if filter_binary is None:
        from siftd.config import get_ingestion_filter_binary

        filter_binary = get_ingestion_filter_binary()

    stats = IngestStats()

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

    # Cache workspace identity lookups to avoid repeated git subprocess calls
    _workspace_cache: dict[str, str] = {}

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
            }

        try:
            # Strategy: file-based dedup
            if dedup_strategy == "file":
                # Check if already ingested
                existing_info = get_ingested_file_info(conn, file_path)
                if existing_info:
                    location = source.as_path
                    st = location.stat()

                    # Fast path: stat-only skip (no file I/O for hashing)
                    if _stat_unchanged(existing_info, st):
                        _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                        continue

                    # Slow path: mtime or size changed (or no stored stat), hash the file
                    current_hash = compute_file_hash(location)

                    if _hash_unchanged(existing_info, current_hash):
                        # Content unchanged, mtime drifted — update stored stat
                        update_file_stat(conn, file_path, st.st_mtime, st.st_size)
                        conn.commit()
                        _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                        continue

                    # Hash changed - re-ingest
                    # Delete old conversation/record
                    carryover: ConversationCarryover | None = None
                    if existing_info["conversation_id"]:
                        carryover = _take_conversation_for_replacement(
                            conn, existing_info["conversation_id"], file_path
                        )
                        parsed = None
                        if carryover is None:
                            parsed = _parse_source_conversation(source, adapter, file_path)
                        if carryover is None and (
                            parsed is None
                            or _refused_claim_is_a_copy(
                                conn, adapter, existing_info["conversation_id"], file_path, parsed
                            )
                        ):
                            # Two paths carry one session and this one changed.
                            # Settle exactly as the collision repair settles
                            # its loser: keep the link, stamp this file's hash
                            # so the copies do not churn (and so an errored row
                            # stops being re-examined every run), and name it —
                            # a frozen copy is only diagnosable if it is named.
                            # The freeze is mutual and deliberate: neither copy
                            # may replace the conversation, so its content is
                            # whatever the path that won the slot last stored.
                            _settle_duplicate_path(
                                conn, file_path, existing_info["conversation_id"],
                                current_hash, st,
                            )
                            _count_file_skipped(
                                source, "skipped (duplicate)", stats, on_file, emit_event
                            )
                            continue
                        if carryover is None:
                            # The link is a legacy collision: this file's
                            # conversation is a different one, filed under
                            # another's id. Release the link; the re-ingest
                            # below stores the file under its own key, and
                            # takes the assignments its transcript owns.
                            carryover = _split_carryover(
                                conn, adapter, existing_info["conversation_id"], file_path, parsed
                            )
                            clear_ingested_file_error(conn, file_path)
                    else:
                        # No conversation recorded (empty file, errored file, or
                        # a row whose pointer was discarded by an older siftd).
                        # Remove the stale record; _reingest_file resolves the
                        # real state from the parsed conversation rather than
                        # trusting this NULL. See its orphan handling.
                        clear_ingested_file_error(conn, file_path)

                    # Re-ingest and update the record
                    conv = _reingest_file(
                        conn, source, adapter, file_path, current_hash, st, stats, filter_binary,
                        _workspace_cache=_workspace_cache,
                        carryover=carryover,
                        resolve_orphan=not existing_info["conversation_id"],
                    )
                    if conv is _SETTLED_DUPLICATE:
                        _count_file_skipped(
                            source, "skipped (duplicate)", stats, on_file, emit_event
                        )
                        continue
                    if on_file:
                        on_file(source, "updated")
                    emit_event("updated", conversation=conv)
                    continue

                # New file - ingest normally
                conv = _ingest_file(conn, source, adapter, file_path, stats, filter_binary, _workspace_cache=_workspace_cache)
                if conv is _SETTLED_DUPLICATE:
                    _count_file_skipped(
                        source, "skipped (duplicate)", stats, on_file, emit_event
                    )
                    continue
                if on_file:
                    on_file(source, "ingested")
                emit_event("ingested", conversation=conv)

            # Strategy: session-based dedup (latest wins)
            elif dedup_strategy == "session":
                # Fast path: stat-only skip for unchanged session files
                existing_file_info = get_ingested_file_info(conn, file_path)
                change_files = _change_files(adapter, source)
                if existing_file_info:
                    st = _change_stat(change_files)

                    if _stat_unchanged(existing_file_info, st):
                        _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                        continue

                    current_hash = _change_hash(change_files)
                    if _hash_unchanged(existing_file_info, current_hash):
                        update_file_stat(conn, file_path, st.st_mtime, st.st_size)
                        conn.commit()
                        _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                        continue

                # A session source may yield MANY conversations (one per
                # session in a DB, or per header in an aider history). Store
                # each independently, deduped by external_id; replace only the
                # ones that moved, per _session_should_replace. The
                # ingested_files row is a single per-file hash/mtime marker with
                # conversation_id=NULL, so replacing any one session does not
                # cascade-delete the marker (see record_session_file).
                # The marker is measured before the parse, so it describes what
                # was read. Measured after, a commit that lands during the parse
                # would be stamped as ingested without having been read, and the
                # next run would skip it.
                if st is None:
                    st = _change_stat(change_files)
                file_hash = current_hash or _change_hash(change_files)
                recorded = replace(
                    source,
                    metadata={**source.metadata, "recorded_locations": _recorded_locations(conn, harness_name)},
                )
                conversations = [
                    _with_content_source(c, file_path)
                    for c in _parse_source_conversations(recorded, adapter, file_path)
                ]
                if not conversations:
                    _count_file_skipped(source, "skipped (empty)", stats, on_file, emit_event)
                    continue

                file_harness_id = None
                file_ingested = False
                file_replaced = False
                last_conversation = None

                for conversation in conversations:
                    harness_id = harness_id_for_conversation(conn, conversation)
                    file_harness_id = harness_id

                    existing = find_conversation_by_external_id(
                        conn, harness_id, conversation.external_id
                    )
                    # A row stored under an id the adapter has since re-keyed is
                    # this session. Adopt it, as the file branch does, so its
                    # tags carry over instead of a second copy being stored.
                    superseded = None if existing else _superseded_conversation(conn, harness_id, conversation)
                    if superseded:
                        legacy_row, legacy_key = superseded
                        carryover = _take_conversation_for_replacement(
                            conn, legacy_row["id"], file_path
                        )
                        if carryover is None:
                            logger.warning(
                                f"{file_path}: another path's bookkeeping row points at "
                                f"{conversation.external_id}; leaving it as ingested"
                            )
                            continue
                        rekey_pending_tags(conn, (legacy_key,), conversation.external_id)
                        conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
                        _restore_carryover_after_replacement(
                            conn, conv_id, carryover, conversation.external_id
                        )
                        _apply_pending_tags(conn, adapter, conversation, conv_id)
                        _update_stats_for_conversation(stats, harness_name, conversation)
                        stats.by_harness[harness_name]["replaced"] += 1
                        file_replaced = True
                        last_conversation = conversation
                    elif existing:
                        # Only a moved session triggers replacement; an
                        # equal/older one is left untouched (preserving its
                        # tags, ownership, and manual state).
                        if _session_should_replace(
                            conversation.ended_at,
                            existing["ended_at"],
                            revision_moved=_revision_moved(conn, conversation, existing["id"]),
                        ):
                            # Same delete-then-insert shape as the file branch,
                            # so it loses tags the same way and takes the same
                            # claim. A session marker is conversation_id=NULL by
                            # design, so a refusal is unreachable here today —
                            # the guard rides along structurally rather than as
                            # a comment asserting it can't happen.
                            carryover = _take_conversation_for_replacement(
                                conn, existing["id"], file_path
                            )
                            if carryover is None:
                                logger.warning(
                                    f"{file_path}: another path's bookkeeping row points at "
                                    f"{conversation.external_id}; leaving it as ingested"
                                )
                                continue
                            conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
                            _restore_carryover_after_replacement(
                                conn, conv_id, carryover, conversation.external_id
                            )
                            _apply_pending_tags(conn, adapter, conversation, conv_id)
                            _update_stats_for_conversation(stats, harness_name, conversation)
                            stats.by_harness[harness_name]["replaced"] += 1
                            file_replaced = True
                            last_conversation = conversation
                    else:
                        conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
                        _apply_pending_tags(conn, adapter, conversation, conv_id)
                        _update_stats_for_conversation(stats, harness_name, conversation)
                        file_ingested = True
                        last_conversation = conversation

                # One marker for the whole file (NULL conversation_id), upserted.
                # conversations is non-empty here, so the loop set the harness.
                assert file_harness_id is not None
                record_session_file(
                    conn, file_path, file_hash, file_harness_id,
                    file_mtime=st.st_mtime, file_size=st.st_size,
                )
                conn.commit()

                # File-level outcome: ingested if any new session arrived, else
                # replaced if any session was updated, else nothing changed.
                if file_ingested:
                    stats.files_ingested += 1
                    file_status = "ingested"
                elif file_replaced:
                    stats.files_replaced += 1
                    file_status = "replaced"
                else:
                    _count_file_skipped(
                        source, "skipped (older)", stats, on_file, emit_event
                    )
                    continue
                if on_file:
                    on_file(source, file_status)
                emit_event(file_status, conversation=last_conversation)

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
                # We lost a race (or are recovering from a poisoned row): the
                # conversation this file describes already exists. That is not a
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
                        if _conversation_claimed_elsewhere(conn, existing["id"], file_path):
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
            if keep is not None and not _conversation_exists(conn, keep):
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


def _conversation_exists(conn: sqlite3.Connection, conversation_id: str) -> bool:
    """Whether a conversation row is still present."""
    return conn.execute(
        "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone() is not None


def _conversation_claimed_elsewhere(
    conn: sqlite3.Connection,
    conversation_id: str,
    file_path: str,
) -> bool:
    """Whether some OTHER path's bookkeeping row points at this conversation.

    ``ingested_files.conversation_id`` has no uniqueness constraint, so two
    paths carrying one session (a restored backup, an overlapping ``--path``)
    can both reference one conversation — a state the duplicate-collision
    repair itself creates. It is also ``ON DELETE CASCADE``, so replacing that
    conversation takes the other path's row with it. Anywhere this code is
    about to delete a conversation it did not itself store, or to declare one
    unowned, it has to ask first.
    """
    return conn.execute(
        "SELECT 1 FROM ingested_files WHERE conversation_id = ? AND path != ? LIMIT 1",
        (conversation_id, file_path),
    ).fetchone() is not None


def _refused_claim_is_a_copy(
    conn: sqlite3.Connection,
    adapter: AdapterModule,
    conversation_id: str,
    file_path: str,
    conversation,
) -> bool:
    """Whether a refused claim on ``conversation_id`` is a second copy of ``conversation``.

    A refusal means another path's bookkeeping row names the row. That path is a
    second copy of this file only if it holds this conversation's identity, and
    the row actually stores its transcript (see _stores_all_of). A shared identity
    alone shows two files are copies of each other, not that either was stored: a
    backup of a forked child can claim the row holding only the parent's history,
    and settling this file against it would leave the child with no conversation
    at all. That claim is not storage, so the file is stored under its own key,
    once, and its copies then resolve against it. A claimant whose copy the row
    does hold leaves this file's own change unstored, as a duplicate's is.

    A claimant that parses to a different identity owns a different conversation:
    a forked child keeps its parent's history, so a legacy collision filed the
    child under the parent's id, and a sibling chat that shared a session id was
    filed the same way. For those, storing this file under its own key is the
    lossless answer, because nothing is a copy of it. A claimant that cannot be
    parsed is not shown to differ, so it keeps the claim as a copy.
    """
    for other in _claimant_conversations(conn, adapter, conversation_id, file_path):
        if other is None:  # an unreadable claimant is not shown to be a different conversation
            return True
        if other.external_id == conversation.external_id and _stores_all_of(conn, conversation_id, other):
            return True
    return False


def _settle_duplicate_path(
    conn: sqlite3.Connection,
    file_path: str,
    conversation_id: str,
    file_hash: str,
    file_stat: os.stat_result,
    *,
    external_id: str | None = None,
) -> None:
    """Leave a second copy of a tracked transcript linked, named, and settled.

    Only one path can hold a session's ``(harness_id, external_id)`` slot and
    neither copy is more authoritative, so the loser records what it actually
    has — a link to the conversation that exists — rather than replacing it.
    Stamping this file's own hash is what stops the copies taking turns: it is
    the one case where the row may assert content it did not ingest, because
    there is no convergence to wait for (contrast the lost-race branch of the
    duplicate-collision handler, where re-hashing next run is the whole point).

    ``external_id`` names the shared session in the warning; pass it when the
    caller has already parsed it, and it is read back from the conversation
    otherwise.
    """
    if external_id is None:
        row = conn.execute(
            "SELECT external_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        external_id = row["external_id"] if row else conversation_id
    logger.warning(
        f"{file_path} duplicates a transcript already tracked under another path "
        f"(external_id {external_id}); its change was not ingested — the "
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


def _take_conversation_for_replacement(
    conn: sqlite3.Connection,
    conversation_id: str,
    file_path: str,
) -> ConversationCarryover | None:
    """Claim a conversation for delete-then-insert, or refuse.

    Returns a snapshot of the assignments the delete would otherwise destroy,
    with the conversation already gone — or ``None`` when another path's
    bookkeeping row points at it, in which case nothing was touched. ``None``
    is unambiguous: a snapshot is always an object, empty at worst.

    Ordering is forced, which is why this is one operation rather than three
    calls a caller could get out of order: ``UNIQUE(harness_id, external_id)``
    means the old row must go before the new one lands, and an AFTER DELETE
    trigger means a post-delete re-point would match zero rows. So snapshot,
    then delete, then let the caller store and re-point — all inside the
    caller's transaction.

    The refusal is the reason this is a function at all. ``conversation_id``
    has no uniqueness constraint and is ``ON DELETE CASCADE``, so a
    conversation two paths carry (a restored backup, an overlapping
    ``--path``, or the collision repair's own re-link) would take the other
    path's row and events with it. Routing every delete site through here
    makes "ask before replacing" structural rather than a rule each site has
    to remember; the sites differ only in what they do with a refusal.
    """
    if _conversation_claimed_elsewhere(conn, conversation_id, file_path):
        return None
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
    file_path: str,
    stats: IngestStats,
    filter_binary: bool,
    *,
    _workspace_cache: dict | None = None,
) -> object | None:
    """Ingest a single file (file-based dedup strategy)."""
    harness_name = adapter.NAME
    location = source.as_path
    st = location.stat()
    file_hash = compute_file_hash(location)

    conversation = _parse_source_conversation(source, adapter, file_path)

    if conversation is None:
        # Empty file - record with NULL conversation_id
        harness_kwargs = {}
        if hasattr(adapter, "HARNESS_SOURCE"):
            harness_kwargs["source"] = adapter.HARNESS_SOURCE
        harness_id = get_or_create_harness(conn, harness_name, **harness_kwargs)
        record_empty_file(conn, file_path, file_hash, harness_id, file_mtime=st.st_mtime, file_size=st.st_size)
        conn.commit()
        stats.files_ingested += 1
        return None

    # A row stored under a superseded external_id is this file's conversation
    # (a previous release keyed it differently). Take it for replacement so its
    # tags carry over and no second copy is stored beside it. If another path
    # already claims that row, it is settled as a copy only when that path holds
    # this conversation's identity (see _refused_claim_is_a_copy).
    harness_id = harness_id_for_conversation(conn, conversation)
    carryover: ConversationCarryover | None = None
    superseded = _superseded_conversation(conn, harness_id, conversation)
    if superseded:
        legacy_row, legacy_key = superseded
        carryover = _take_conversation_for_replacement(conn, legacy_row["id"], file_path)
        if carryover is None:
            if _refused_claim_is_a_copy(conn, adapter, legacy_row["id"], file_path, conversation):
                _settle_duplicate_path(
                    conn, file_path, legacy_row["id"], file_hash, st,
                    external_id=conversation.external_id,
                )
                return _SETTLED_DUPLICATE
            # The legacy row belongs to another conversation: store this one under its own key,
            # and take the assignments its transcript owns. The key's queued tags stay queued:
            # a refused row names a different file, so its queue is not this conversation's.
            carryover = _split_carryover(conn, adapter, legacy_row["id"], file_path, conversation)
        else:
            rekey_pending_tags(conn, (legacy_key,), conversation.external_id)

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
    _restore_carryover_after_replacement(conn, conv_id, carryover, conversation.external_id)
    _update_stats_for_conversation(stats, harness_name, conversation)
    record_ingested_file(conn, file_path, file_hash, conv_id, file_mtime=st.st_mtime, file_size=st.st_size)

    # Apply pending tags from live session
    _apply_pending_tags(conn, adapter, conversation, conv_id)

    conn.commit()
    stats.files_ingested += 1
    return conversation


def _reingest_file(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    file_path: str,
    file_hash: str,
    file_stat: os.stat_result,
    stats: IngestStats,
    filter_binary: bool,
    *,
    _workspace_cache: dict | None = None,
    carryover: ConversationCarryover | None = None,
    resolve_orphan: bool = False,
) -> object | None:
    """Re-ingest a file that has changed (file-based dedup strategy).

    Unlike _ingest_file, the old conversation has already been deleted
    and the file hash is already computed.

    ``resolve_orphan`` says the caller found no conversation pointer on the
    bookkeeping row, so it deleted nothing. A NULL pointer is not proof that no
    conversation exists — older releases discarded the pointer whenever a
    concurrent ingest lost the UNIQUE(harness_id, external_id) race — so this
    path re-derives the truth from the parsed conversation instead of trusting
    the row, which makes it idempotent whatever state the bookkeeping is in.

    Note: delete_conversation also deletes the ingested_files record,
    so we create a new record rather than updating.

    carryover carries the deleted conversation's assignments so they can be
    re-pointed at the replacement rows before the commit — the caller cannot
    do it afterwards, because that would either split the transaction or
    (post-delete) match zero rows. See
    :class:`~siftd.storage.replacement.ConversationCarryover` for what is carried
    and what is not.
    """
    harness_name = adapter.NAME

    conversation = _parse_source_conversation(source, adapter, file_path)

    if conversation is None:
        # The transcript parsed to nothing — there is no replacement row to
        # carry the snapshotted tags to, so they go with the conversation they
        # described. Usually the file really was emptied, but a transcript
        # rewritten in place can transiently parse to zero too, so say what was
        # lost rather than dropping it silently.
        if carryover:
            logger.warning(
                f"{carryover.describe()} were dropped: "
                f"{file_path} no longer parses to a conversation"
            )
        # Record with NULL conversation_id
        harness_kwargs = {}
        if hasattr(adapter, "HARNESS_SOURCE"):
            harness_kwargs["source"] = adapter.HARNESS_SOURCE
        harness_id = get_or_create_harness(conn, harness_name, **harness_kwargs)
        record_empty_file(conn, file_path, file_hash, harness_id, file_mtime=file_stat.st_mtime, file_size=file_stat.st_size)
        conn.commit()
        stats.files_replaced += 1
        stats.by_harness[harness_name]["replaced"] += 1
        return None

    if resolve_orphan:
        # The bookkeeping claimed no conversation. If one exists under this
        # (harness, external_id) it is an orphan the bookkeeping lost track of,
        # and storing over it would raise UNIQUE — the state that froze these
        # files permanently (kgruel/siftd#29). Replace it exactly like the
        # normal path does, snapshot first: the orphan is where the tags live,
        # so deleting it unsnapshotted would destroy every affected
        # conversation's tags on the first ingest after upgrade.
        harness_id = harness_id_for_conversation(conn, conversation)
        orphan = find_conversation_by_external_id(conn, harness_id, conversation.external_id)
        if orphan:
            # A refusal means it is not an orphan at all: another path's
            # bookkeeping row points at it, so this file is a second copy of a
            # tracked transcript. Leaving it lets store_conversation collide,
            # and the duplicate-conversation handler in ingest_all links this
            # path at the existing conversation, which is lossless.
            carryover = _take_conversation_for_replacement(
                conn, orphan["id"], file_path
            )
        else:
            # A legacy row under a superseded key is this file's conversation. A
            # refusal cannot be left to a collision: the new key does not collide
            # with the legacy one, so the store would write a second copy. So a
            # refused claim settles as a copy when its holder is this conversation
            # (see _refused_claim_is_a_copy), and otherwise the file is stored.
            superseded = _superseded_conversation(conn, harness_id, conversation)
            if superseded:
                legacy_row, legacy_key = superseded
                carryover = _take_conversation_for_replacement(
                    conn, legacy_row["id"], file_path
                )
                if carryover is None:
                    if _refused_claim_is_a_copy(conn, adapter, legacy_row["id"], file_path, conversation):
                        _settle_duplicate_path(
                            conn, file_path, legacy_row["id"], file_hash, file_stat,
                            external_id=conversation.external_id,
                        )
                        return _SETTLED_DUPLICATE
                    # The legacy row belongs to another conversation: store this one under its own key,
                    # and take the assignments its transcript owns.
                    carryover = _split_carryover(conn, adapter, legacy_row["id"], file_path, conversation)
                else:
                    rekey_pending_tags(conn, (legacy_key,), conversation.external_id)

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)

    # Re-point the pre-delete tag assignments at the replacement rows,
    # preserving applied_at.
    _restore_carryover_after_replacement(conn, conv_id, carryover, conversation.external_id)

    _update_stats_for_conversation(stats, harness_name, conversation)
    record_ingested_file(conn, file_path, file_hash, conv_id, file_mtime=file_stat.st_mtime, file_size=file_stat.st_size)

    # Apply pending tags from live session
    _apply_pending_tags(conn, adapter, conversation, conv_id)

    conn.commit()
    stats.files_replaced += 1
    stats.by_harness[harness_name]["replaced"] += 1
    return conversation


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
