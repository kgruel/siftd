"""Orchestration: coordinate ingestion pipeline.

Design constraint: One source file → at most one conversation.
----------------------------------------------------------
The `ingested_files.path` column is UNIQUE, meaning each file can map to exactly
one conversation. This is intentional: most adapters produce one conversation per
file (JSONL session logs, markdown exports, etc.).

If an adapter's parse() yields multiple conversations from a single source, we
fail that source explicitly. Supporting multi-conversation sources (e.g., SQLite
DBs containing many sessions) would require schema changes to ingested_files —
revisit if a real use case emerges.
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
from siftd.storage.events import get_last_event_id, get_prompt_by_index
from siftd.storage.sessions import (
    LAST_MARKER_DISPATCH,
    consume_pending_tags,
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
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    record_session_file,
    store_conversation,
    update_file_stat,
)
from siftd.storage.tags import apply_tag, get_or_create_tag, get_tag_assignments

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


def _get_single_conversation(conversations: list, source_path: str):
    """Enforce 0/1 conversation per source file.

    Returns None if the list is empty.
    Raises AdapterParseError when a source yields multiple conversations.
    """
    if not conversations:
        return None
    if len(conversations) > 1:
        raise AdapterParseError(
            f"Source {source_path} yielded {len(conversations)} conversations; "
            "ingest currently requires exactly one conversation per source"
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
    return _get_single_conversation(list(adapter.parse(source)), source_path)


def _parse_source_conversations(
    source: Source,
    adapter: AdapterModule,
    source_path: str,
) -> list:
    """Parse a session-strategy source into all of its conversations.

    Unlike :func:`_parse_source_conversation` (file strategy, 0/1), a session
    source may legitimately yield many conversations — e.g. an OpenCode or
    Gemini SQLite DB with one conversation per session. Each is stored and
    deduped independently by external_id.
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
    - "session": one conversation per session, replace if newer

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

                    # Fast path: stat-only skip (no file I/O for hashing)
                    st = location.stat()
                    stored_mtime = existing_info["file_mtime"]
                    stored_size = existing_info["file_size"]
                    if (
                        stored_mtime is not None
                        and st.st_mtime == stored_mtime
                        and st.st_size == stored_size
                    ):
                        stats.files_skipped += 1
                        if on_file:
                            on_file(source, "skipped")
                        emit_event("skipped")
                        continue

                    # Slow path: mtime or size changed (or no stored stat), hash the file
                    current_hash = compute_file_hash(location)

                    if current_hash == existing_info["file_hash"]:
                        # Content unchanged, mtime drifted — update stored stat
                        update_file_stat(conn, file_path, st.st_mtime, st.st_size)
                        conn.commit()
                        stats.files_skipped += 1
                        if on_file:
                            on_file(source, "skipped")
                        emit_event("skipped")
                        continue

                    # Hash changed - re-ingest
                    # Delete old conversation/record
                    tag_snapshot: list[tuple[str, str]] = []
                    if existing_info["conversation_id"]:
                        # Conversation-level tags are keyed by the conversation
                        # ULID, and delete_conversation's AFTER DELETE trigger
                        # (tr_polymorphic_conversations_cleanup) removes them.
                        # The replacement row gets a fresh ULID, so without this
                        # snapshot every re-ingest of a live transcript silently
                        # drops its tags. Ordering is forced: UNIQUE(harness_id,
                        # external_id) means the old row must go before the new
                        # one lands, and an AFTER DELETE trigger means a
                        # post-delete UPDATE would match zero rows. So: snapshot
                        # → delete → re-ingest → re-point, all in one
                        # transaction (_reingest_file owns the commit).
                        tag_snapshot = get_tag_assignments(
                            conn, "conversation", existing_info["conversation_id"]
                        )
                        delete_conversation(conn, existing_info["conversation_id"])
                    else:
                        # No conversation (empty or errored file) — remove old record
                        clear_ingested_file_error(conn, file_path)

                    # Re-ingest and update the record
                    conv = _reingest_file(
                        conn, source, adapter, file_path, current_hash, st, stats, filter_binary,
                        _workspace_cache=_workspace_cache,
                        tag_snapshot=tag_snapshot,
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
                st = None
                current_hash = None
                if existing_file_info:
                    st = location.stat()
                    stored_mtime = existing_file_info["file_mtime"]
                    stored_size = existing_file_info["file_size"]
                    if (
                        stored_mtime is not None
                        and st.st_mtime == stored_mtime
                        and st.st_size == stored_size
                    ):
                        stats.files_skipped += 1
                        if on_file:
                            on_file(source, "skipped")
                        emit_event("skipped")
                        continue

                    current_hash = compute_file_hash(location)
                    if current_hash == existing_file_info["file_hash"]:
                        update_file_stat(conn, file_path, st.st_mtime, st.st_size)
                        conn.commit()
                        stats.files_skipped += 1
                        if on_file:
                            on_file(source, "skipped")
                        emit_event("skipped")
                        continue

                # A session source may yield MANY conversations (one per
                # session in a DB). Store each independently, deduped by
                # external_id; replace only when the parsed copy is newer. The
                # ingested_files row is a single per-file hash/mtime marker with
                # conversation_id=NULL, so replacing any one session does not
                # cascade-delete the marker (see record_session_file).
                conversations = _parse_source_conversations(source, adapter, file_path)
                if not conversations:
                    stats.files_skipped += 1
                    if on_file:
                        on_file(source, "skipped (empty)")
                    emit_event("skipped (empty)")
                    continue

                if st is None:
                    st = location.stat()
                file_hash = current_hash or compute_file_hash(location)

                file_harness_id = None
                file_ingested = False
                file_replaced = False
                last_conversation = None

                for conversation in conversations:
                    harness_kwargs = {}
                    if conversation.harness.source:
                        harness_kwargs["source"] = conversation.harness.source
                    if conversation.harness.log_format:
                        harness_kwargs["log_format"] = conversation.harness.log_format
                    if conversation.harness.display_name:
                        harness_kwargs["display_name"] = conversation.harness.display_name
                    harness_id = get_or_create_harness(conn, conversation.harness.name, **harness_kwargs)
                    file_harness_id = harness_id

                    existing = find_conversation_by_external_id(
                        conn, harness_id, conversation.external_id
                    )
                    if existing:
                        # Only a newer parsed copy triggers replacement; an
                        # equal/older session is left untouched (preserving its
                        # tags, ownership, and manual state).
                        if _compare_timestamps(conversation.ended_at, existing["ended_at"]):
                            delete_conversation(conn, existing["id"])
                            conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)
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
                    stats.files_skipped += 1
                    file_status = "skipped (older)"
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
            if is_duplicate_conversation:
                # Race condition: conversation was inserted between our check and store
                try:
                    conv = _parse_source_conversation(source, adapter, file_path)
                    if conv is not None:
                        harness_kwargs = {}
                        if conv.harness.source:
                            harness_kwargs["source"] = conv.harness.source
                        if conv.harness.log_format:
                            harness_kwargs["log_format"] = conv.harness.log_format
                        if conv.harness.display_name:
                            harness_kwargs["display_name"] = conv.harness.display_name
                        h_id = get_or_create_harness(conn, conv.harness.name, **harness_kwargs)
                        existing = find_conversation_by_external_id(conn, h_id, conv.external_id)
                        if existing and not get_ingested_file_info(conn, file_path):
                            location = source.as_path
                            st = location.stat()
                            fh = compute_file_hash(location)
                            record_ingested_file(conn, file_path, fh, existing["id"], file_mtime=st.st_mtime, file_size=st.st_size)
                            conn.commit()
                            stats.files_skipped += 1
                            if on_file:
                                on_file(source, "skipped (duplicate)")
                            emit_event("skipped (duplicate)")
                            continue
                except Exception:
                    pass
            # Other IntegrityError (not duplicate conversation) — record as error
            _record_file_error(
                conn, source, adapter, file_path, error_msg, stats, on_file, emit_event
            )

        except Exception as e:
            conn.rollback()
            # See the IntegrityError handler above: clear vocab caches so a
            # rolled-back model/provider ULID is not reused as a dangling FK.
            clear_vocabulary_caches()
            _record_file_error(
                conn, source, adapter, file_path, str(e), stats, on_file, emit_event
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
) -> None:
    """Record a file that failed ingestion so it won't retry.

    If the file was previously recorded as a success, update the row to
    reflect the new error state so the failure is queryable and the stale
    conversation link is cleared.
    """
    try:
        existing = get_ingested_file_info(conn, file_path)
        location = source.as_path
        st = location.stat()
        file_hash = compute_file_hash(location)
        if existing:
            # Update existing row: clear conversation link, set error
            conn.execute(
                """UPDATE ingested_files
                   SET file_hash = ?, conversation_id = NULL, error = ?,
                       file_mtime = ?, file_size = ?
                   WHERE path = ?""",
                (file_hash, error, st.st_mtime, st.st_size, file_path),
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
    tag_snapshot: list[tuple[str, str]] | None = None,
) -> object | None:
    """Re-ingest a file that has changed (file-based dedup strategy).

    Unlike _ingest_file, the old conversation has already been deleted
    and the file hash is already computed.

    Note: delete_conversation also deletes the ingested_files record,
    so we create a new record rather than updating.

    tag_snapshot carries the deleted conversation's (tag_id, applied_at)
    assignments so they can be re-pointed at the replacement row before the
    commit — the caller cannot do it afterwards, because that would either
    split the transaction or (post-delete) match zero rows. Only
    conversation-level assignments are carried: events also get new ULIDs on
    re-ingest, and matching them to their predecessors is deferred (0.13.0).
    """
    harness_name = adapter.NAME

    conversation = _parse_source_conversation(source, adapter, file_path)

    if conversation is None:
        # File became empty — there is no replacement row to carry the
        # snapshotted tags to, so they are deliberately dropped along with the
        # conversation they described.
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

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)

    # Re-point the pre-delete tag assignments at the replacement row,
    # preserving applied_at (explicit re-point, same style as
    # storage/migrate_workspaces.py — there is no FK to cascade from).
    for tag_id, applied_at in tag_snapshot or ():
        apply_tag(conn, "conversation", conv_id, tag_id, applied_at=applied_at)

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

    # Consume from the first key that has anything queued.
    #
    # Ingest-order note (subagents): if the parent conversation is ingested
    # first in the same run, it will consume the tags itself (correct — both
    # belong to the same session). The subagent fallback only fires when the
    # subagent is ingested before the parent, or when the parent file was
    # skipped (unchanged since last ingest). Either way, the tag lands on
    # exactly one conversation in the session, which is the intended
    # "tag this session" semantic.
    pending: list = []
    for key in candidates:
        pending = consume_pending_tags(conn, key)
        if pending:
            break

    def _unregister_all() -> None:
        # Sessions are registered under whichever key the harness reported —
        # the bare one, in practice. Unregister every candidate so ingest
        # actually clears the active_sessions row it just ingested.
        for key in candidates:
            unregister_session(conn, key)

    if not pending:
        # No pending tags, but still unregister the session
        _unregister_all()
        return 0

    applied = 0
    for pt in pending:
        tag_id = get_or_create_tag(conn, pt.tag_name)

        if pt.last_marker:
            dispatch = LAST_MARKER_DISPATCH.get(pt.last_marker)
            if dispatch is None:
                logger.warning(
                    f"Unknown last_marker {pt.last_marker!r} for tag '{pt.tag_name}' "
                    f"in session {session_id[:12]}; skipping"
                )
                continue
            target_kind, fetch_kind = dispatch
            event_id = get_last_event_id(conn, conversation_id, fetch_kind)
            if event_id is None:
                logger.warning(
                    f"No {fetch_kind} found for session {session_id[:12]}; "
                    f"tag '{pt.tag_name}' ({pt.last_marker}) not applied"
                )
                continue
            result = apply_tag(conn, target_kind, event_id, tag_id)
            if result:
                applied += 1
                logger.debug(
                    f"Applied tag '{pt.tag_name}' to {target_kind} {event_id[:12]} "
                    f"({pt.last_marker})"
                )

        elif pt.entity_type == "conversation":
            result = apply_tag(conn, "conversation", conversation_id, tag_id)
            if result:
                applied += 1
                logger.debug(f"Applied tag '{pt.tag_name}' to conversation {conversation_id[:12]}")

        elif pt.entity_type == "exchange":
            # Look up the prompt at exchange_index
            try:
                prompt_id = get_prompt_by_index(conn, conversation_id, pt.exchange_index)
            except ValueError as e:
                logger.warning(
                    f"Invalid exchange_index for tag '{pt.tag_name}' in session {session_id[:12]}: {e}"
                )
                continue
            if prompt_id:
                result = apply_tag(conn, "exchange", prompt_id, tag_id)
                if result:
                    applied += 1
                    logger.debug(
                        f"Applied tag '{pt.tag_name}' to exchange {prompt_id[:12]} "
                        f"(exchange {pt.exchange_index})"
                    )
            else:
                logger.warning(
                    f"Exchange index {pt.exchange_index} not found for session {session_id[:12]}; "
                    f"tag '{pt.tag_name}' not applied"
                )

    # Unregister the session (every key form it may be registered under)
    _unregister_all()
    return applied
