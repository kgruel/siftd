"""Orchestration: coordinate ingestion pipeline.

Design constraint: cardinality is a property of the adapter's DEDUP_STRATEGY.
----------------------------------------------------------------------------
The `ingested_files.path` column is UNIQUE, so a row can name at most one
conversation. Which of the two branches below a source takes decides what that
row means:

- `"file"`: one conversation per source (JSONL session logs, single-transcript
  exports). The row points at it, and a content change replaces it outright.
  A parse yielding more than one conversation is an adapter bug and fails the
  source.
- `"session"`: conversations are deduped by external_id rather than by the
  file that produced them. That covers a source holding many at once (an
  OpenCode SQLite DB, an aider history file accumulating every session for a
  project) and a source holding one that is re-exported as it changes (a
  Gemini CLI chat JSON). The row is a hash/mtime marker with
  `conversation_id = NULL`, so replacing any one conversation does not
  cascade the marker away.

An adapter whose source grows new conversations over its life belongs on
`"session"`, whatever its storage format. `aider` did not, and every history
file with a second session failed permanently (#36).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from siftd.adapters.sdk import AdapterParseError
from siftd.domain import Source
from siftd.storage.sessions import (
    drain_pending_tags,
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
from siftd.storage.tags import (
    ConversationTagSnapshot,
    restore_conversation_tags,
    snapshot_conversation_tags,
)

from .discovery import discover_all

logger = logging.getLogger(__name__)

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


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string to datetime.

    Handles various formats:
    - 2024-01-15T10:30:00Z (Zulu)
    - 2024-01-15T10:30:00+00:00 (explicit offset)
    - 2024-01-15T10:30:00 (naive, assumed UTC)
    """
    # Normalize 'Z' suffix to '+00:00' for fromisoformat
    normalized = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # Fallback: try without timezone, assume UTC
        dt = datetime.fromisoformat(ts.rstrip("Z"))
        dt = dt.replace(tzinfo=UTC)

    # If naive (no tzinfo), assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _compare_timestamps(new_ts: str | None, existing_ts: str | None) -> bool:
    """Return True if new_ts is newer than existing_ts.

    None is treated as oldest (so any timestamp beats None).
    Parses timestamps to datetime for safe comparison across formats.
    """
    if new_ts is None:
        return False
    if existing_ts is None:
        return True
    return _parse_timestamp(new_ts) > _parse_timestamp(existing_ts)


def _stat_unchanged(existing_info, st: os.stat_result) -> bool:
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


def _session_should_replace(new_ended: str | None, existing_ended: str | None) -> bool:
    """Whether a re-parsed session should replace the copy already stored.

    `ended_at` is this branch's per-conversation change detector: a container
    source re-parses every session it holds, and only the ones that actually
    moved are worth the delete-then-insert (which costs new ULIDs, a tag
    snapshot/restore, and the conversation's ownership rows).

    A conversation reporting no `ended_at` is not "unchanged" — it is a
    session with no detector at all, and `newer than` is false forever, which
    is what froze every aider session at its first ingest (#36). We only reach
    here because the file's content hash changed, so the honest reading of
    "no detector" is that the re-parsed file wins.
    """
    if new_ended is None:
        return True
    return _compare_timestamps(new_ended, existing_ended)


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
        st: os.stat_result | None = None
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
                    tag_snapshot: ConversationTagSnapshot | None = None
                    if existing_info["conversation_id"]:
                        tag_snapshot = _take_conversation_for_replacement(
                            conn, existing_info["conversation_id"], file_path
                        )
                        if tag_snapshot is None:
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
                        tag_snapshot=tag_snapshot,
                        resolve_orphan=not existing_info["conversation_id"],
                    )
                    if on_file:
                        on_file(source, "updated")
                    emit_event("updated", conversation=conv)
                    continue

                # New file - ingest normally
                conv = _ingest_file(conn, source, adapter, file_path, stats, filter_binary, _workspace_cache=_workspace_cache)
                if on_file:
                    on_file(source, "ingested")
                emit_event("ingested", conversation=conv)

            # Strategy: session-based dedup (latest wins)
            elif dedup_strategy == "session":
                # Fast path: stat-only skip for unchanged session files
                existing_file_info = get_ingested_file_info(conn, file_path)
                location = source.as_path
                if existing_file_info:
                    st = location.stat()

                    if _stat_unchanged(existing_file_info, st):
                        _count_file_skipped(source, "skipped", stats, on_file, emit_event)
                        continue

                    current_hash = compute_file_hash(location)
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
                conversations = _parse_source_conversations(source, adapter, file_path)
                if not conversations:
                    _count_file_skipped(source, "skipped (empty)", stats, on_file, emit_event)
                    continue

                if st is None:
                    st = location.stat()
                file_hash = current_hash or compute_file_hash(location)

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
                    if existing:
                        # Only a moved session triggers replacement; an
                        # equal/older one is left untouched (preserving its
                        # tags, ownership, and manual state).
                        if _session_should_replace(
                            conversation.ended_at, existing["ended_at"]
                        ):
                            # Same delete-then-insert shape as the file branch,
                            # so it loses tags the same way and takes the same
                            # claim. A session marker is conversation_id=NULL by
                            # design, so a refusal is unreachable here today —
                            # the guard rides along structurally rather than as
                            # a comment asserting it can't happen.
                            tag_snapshot = _take_conversation_for_replacement(
                                conn, existing["id"], file_path
                            )
                            if tag_snapshot is None:
                                logger.warning(
                                    f"{file_path}: another path's bookkeeping row points at "
                                    f"{conversation.external_id}; leaving it as ingested"
                                )
                                continue
                            conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
                            _restore_tags_after_replacement(
                                conn, conv_id, tag_snapshot, conversation.external_id
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
    known_stat: os.stat_result | None = None,
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
) -> ConversationTagSnapshot | None:
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
    snapshot = snapshot_conversation_tags(conn, conversation_id)
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

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
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
    tag_snapshot: ConversationTagSnapshot | None = None,
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

    tag_snapshot carries the deleted conversation's assignments so they can be
    re-pointed at the replacement rows before the commit — the caller cannot
    do it afterwards, because that would either split the transaction or
    (post-delete) match zero rows. See
    :class:`~siftd.storage.tags.ConversationTagSnapshot` for what is carried
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
        if tag_snapshot:
            logger.warning(
                f"{_describe_snapshot(tag_snapshot)} were dropped: "
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
            tag_snapshot = _take_conversation_for_replacement(
                conn, orphan["id"], file_path
            )

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)

    # Re-point the pre-delete tag assignments at the replacement rows,
    # preserving applied_at.
    _restore_tags_after_replacement(conn, conv_id, tag_snapshot, conversation.external_id)

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

    for prompt in conversation.prompts:
        stats.prompts += 1
        stats.by_harness[harness_name]["prompts"] += 1
        for response in prompt.responses:
            stats.responses += 1
            stats.by_harness[harness_name]["responses"] += 1
            stats.tool_calls += len(response.tool_calls)
            stats.by_harness[harness_name]["tool_calls"] += len(response.tool_calls)


def _describe_snapshot(snapshot: ConversationTagSnapshot) -> str:
    """Name the nonzero parts of a snapshot, for a loss warning.

    Enumerated rather than templated, so a snapshot carrying only assignments
    that could never be re-pointed doesn't report "0 conversation tag(s) and 0
    element tag(s)" — a warning that names everything except the loss it fired
    for. Never empty at the call site: a snapshot is falsy exactly when every
    part is zero.
    """
    parts = []
    if snapshot.conversation:
        parts.append(f"{len(snapshot.conversation)} conversation tag(s)")
    if snapshot.events:
        parts.append(f"{len(snapshot.events)} element tag(s)")
    if snapshot.dropped_events:
        parts.append(f"{snapshot.dropped_events} synthetic-event tag(s)")
    if snapshot.dropped_blocks:
        parts.append(f"{snapshot.dropped_blocks} block tag(s)")
    return ", ".join(parts)


def _restore_tags_after_replacement(
    conn: sqlite3.Connection,
    conversation_id: str,
    snapshot: ConversationTagSnapshot | None,
    external_id: str,
) -> None:
    """Re-point a snapshot at the replacement rows, reporting what was lost."""
    unmatched = restore_conversation_tags(conn, conversation_id, snapshot)
    if snapshot is None:
        return
    lost = unmatched + snapshot.dropped_events
    if lost:
        logger.warning(
            f"{lost} element tag(s) on {external_id} could not be carried across "
            "re-ingest (the event is no longer in the transcript, or is synthetic)"
        )
    if snapshot.dropped_blocks:
        logger.warning(
            f"{snapshot.dropped_blocks} block tag(s) on {external_id} were dropped "
            "by re-ingest — block-level re-pointing is not implemented yet"
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
