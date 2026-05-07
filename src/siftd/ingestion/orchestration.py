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
from siftd.storage.sessions import consume_pending_tags, unregister_session
from siftd.storage.sqlite import (
    clear_ingested_file_error,
    compute_file_hash,
    delete_conversation,
    ensure_tool_aliases,
    find_conversation_by_external_id,
    get_ingested_file_info,
    get_or_create_harness,
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    store_conversation,
    update_file_stat,
)
from siftd.storage.tags import apply_tag, get_or_create_tag

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
    """Parse a source and enforce the current ingest contract."""
    if not adapter.can_handle(source):
        raise AdapterParseError(
            f"Adapter {adapter.NAME} cannot handle source {source_path}"
        )
    return _get_single_conversation(list(adapter.parse(source)), source_path)


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
                "replaced": 0,
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
                    if existing_info["conversation_id"]:
                        delete_conversation(conn, existing_info["conversation_id"])
                    else:
                        # No conversation (empty or errored file) — remove old record
                        clear_ingested_file_error(conn, file_path)

                    # Re-ingest and update the record
                    conv = _reingest_file(
                        conn, source, adapter, file_path, current_hash, st, stats, filter_binary,
                        _workspace_cache=_workspace_cache,
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

                # We need to parse to get the conversation and check timestamps
                conversation = _parse_source_conversation(source, adapter, file_path)
                if conversation is None:
                    stats.files_skipped += 1
                    if on_file:
                        on_file(source, "skipped (empty)")
                    emit_event("skipped (empty)")
                    continue

                # Get or create harness to look up existing
                harness_kwargs = {}
                if conversation.harness.source:
                    harness_kwargs["source"] = conversation.harness.source
                if conversation.harness.log_format:
                    harness_kwargs["log_format"] = conversation.harness.log_format
                if conversation.harness.display_name:
                    harness_kwargs["display_name"] = conversation.harness.display_name
                harness_id = get_or_create_harness(conn, conversation.harness.name, **harness_kwargs)

                # Check if conversation already exists
                existing = find_conversation_by_external_id(
                    conn, harness_id, conversation.external_id
                )

                if existing:
                    # Compare timestamps — only semantic changes trigger replacement.
                    # Hash-only drift (formatting, adapter-ignored fields) updates the
                    # file record but preserves the conversation and its manual state
                    # (tags, ownership).
                    should_replace = _compare_timestamps(
                        conversation.ended_at, existing["ended_at"]
                    )
                    hash_drifted = (
                        existing_file_info is not None
                        and current_hash is not None
                        and current_hash != existing_file_info["file_hash"]
                    )
                    if should_replace:
                        # New is newer, replace
                        delete_conversation(conn, existing["id"])
                        conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)

                        # Record file ingestion
                        if st is None:
                            st = location.stat()
                        file_hash = current_hash or compute_file_hash(location)
                        record_ingested_file(conn, file_path, file_hash, conv_id, file_mtime=st.st_mtime, file_size=st.st_size)

                        # Apply pending tags from live session
                        _apply_pending_tags(conn, adapter, conversation, conv_id)

                        conn.commit()

                        # Update stats
                        _update_stats_for_conversation(stats, harness_name, conversation)
                        stats.files_replaced += 1
                        stats.by_harness[harness_name]["replaced"] += 1

                        if on_file:
                            on_file(source, "replaced")
                        emit_event("replaced", conversation=conversation)
                    else:
                        # Existing is newer or same, skip
                        # Record file so it's tracked (not shown as pending)
                        if not get_ingested_file_info(conn, file_path):
                            if st is None:
                                st = location.stat()
                            file_hash = current_hash or compute_file_hash(location)
                            record_ingested_file(conn, file_path, file_hash, existing["id"], file_mtime=st.st_mtime, file_size=st.st_size)
                            conn.commit()
                        elif hash_drifted:
                            # File bytes changed but conversation is semantically
                            # the same (no timestamp advance). Update the stored
                            # hash so the fast path works next time, but preserve
                            # the conversation and its manual state.
                            if st is None:
                                st = location.stat()
                            file_hash = current_hash or compute_file_hash(location)
                            conn.execute(
                                """UPDATE ingested_files
                                   SET file_hash = ?, file_mtime = ?, file_size = ?
                                   WHERE path = ?""",
                                (file_hash, st.st_mtime, st.st_size, file_path),
                            )
                            conn.commit()
                        stats.files_skipped += 1
                        if on_file:
                            on_file(source, "skipped (older)")
                        emit_event("skipped (older)")
                else:
                    # New conversation
                    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary, _workspace_cache=_workspace_cache)

                    if st is None:
                        st = location.stat()
                    file_hash = current_hash or compute_file_hash(location)
                    record_ingested_file(conn, file_path, file_hash, conv_id, file_mtime=st.st_mtime, file_size=st.st_size)

                    # Apply pending tags from live session
                    _apply_pending_tags(conn, adapter, conversation, conv_id)

                    conn.commit()

                    _update_stats_for_conversation(stats, harness_name, conversation)
                    stats.files_ingested += 1

                    if on_file:
                        on_file(source, "ingested")
                    emit_event("ingested", conversation=conversation)

        except sqlite3.IntegrityError as e:
            conn.rollback()
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
            _record_file_error(
                conn, source, adapter, file_path, str(e), stats, on_file, emit_event
            )

    # Rebuild materialized stats table for fast list_conversations queries.
    from siftd.storage.conversation_stats import rebuild_conversation_stats

    rebuild_conversation_stats(conn, commit=True)

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
) -> object | None:
    """Re-ingest a file that has changed (file-based dedup strategy).

    Unlike _ingest_file, the old conversation has already been deleted
    and the file hash is already computed.

    Note: delete_conversation also deletes the ingested_files record,
    so we create a new record rather than updating.
    """
    harness_name = adapter.NAME

    conversation = _parse_source_conversation(source, adapter, file_path)

    if conversation is None:
        # File became empty - record with NULL conversation_id
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
    pending = consume_pending_tags(conn, session_id)

    # For subagent conversations (external_id contains ::agent::),
    # also check for tags queued against the parent session.
    # This handles the case where a user tags during a subagent session —
    # the tag targets the parent session ID, but the subagent conversation
    # has a different external_id.
    #
    # Ingest-order note: if the parent conversation is ingested first in the
    # same run, it will consume the tags itself (correct — both belong to the
    # same session). The subagent fallback only fires when the subagent is
    # ingested before the parent, or when the parent file was skipped
    # (unchanged since last ingest). Either way, the tag lands on exactly one
    # conversation in the session, which is the intended "tag this session"
    # semantic.
    parent_id = None
    if not pending and "::agent::" in session_id:
        parent_id = session_id.split("::agent::")[0]
        pending = consume_pending_tags(conn, parent_id)

    if not pending:
        # No pending tags, but still unregister the session
        unregister_session(conn, session_id)
        if parent_id:
            unregister_session(conn, parent_id)
        return 0

    # Phase 3: late-bound markers resolve to the most recent event of the kind
    # at ingest time. The marker → (target_kind, kind-to-fetch) mapping.
    _LAST_MARKER_DISPATCH = {
        "last_prompt": ("prompt", "prompt"),
        "last_response": ("response", "response"),
        # last_exchange is anchored on the prompt event (target_kind='exchange').
        "last_exchange": ("exchange", "prompt"),
        "last_tool_call": ("tool_call", "tool_call"),
    }

    applied = 0
    for pt in pending:
        tag_id = get_or_create_tag(conn, pt.tag_name)

        if pt.last_marker:
            dispatch = _LAST_MARKER_DISPATCH.get(pt.last_marker)
            if dispatch is None:
                logger.warning(
                    f"Unknown last_marker {pt.last_marker!r} for tag '{pt.tag_name}' "
                    f"in session {session_id[:8]}; skipping"
                )
                continue
            target_kind, fetch_kind = dispatch
            event_id = _get_last_event_id(conn, conversation_id, fetch_kind)
            if event_id is None:
                logger.warning(
                    f"No {fetch_kind} found for session {session_id[:8]}; "
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
                prompt_id = _get_prompt_by_index(conn, conversation_id, pt.exchange_index)
            except ValueError as e:
                logger.warning(
                    f"Invalid exchange_index for tag '{pt.tag_name}' in session {session_id[:8]}: {e}"
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
                    f"Exchange index {pt.exchange_index} not found for session {session_id[:8]}; "
                    f"tag '{pt.tag_name}' not applied"
                )

    # Unregister the session (and parent if subagent)
    unregister_session(conn, session_id)
    if parent_id:
        unregister_session(conn, parent_id)
    return applied


def _get_last_event_id(
    conn: sqlite3.Connection,
    conversation_id: str,
    kind: str,
) -> str | None:
    """Return the most-recent event ID of `kind` in this conversation, or None.

    Ordered by (timestamp DESC, id DESC) so ULID ordering breaks ties
    deterministically when multiple events share a timestamp.
    """
    cur = conn.execute(
        """
        SELECT id FROM events
        WHERE conversation_id = ? AND kind = ?
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
        """,
        (conversation_id, kind),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def _get_prompt_by_index(
    conn: sqlite3.Connection,
    conversation_id: str,
    exchange_index: int | None,
) -> str | None:
    """Get the prompt ID at a specific exchange index (1-based).

    Returns None if index is out of range or None.
    """
    if exchange_index is None:
        return None
    if exchange_index < 1:
        raise ValueError(f"exchange_index must be >= 1, got {exchange_index}")

    cur = conn.execute(
        """
        SELECT id FROM events
        WHERE kind = 'prompt' AND conversation_id = ?
        ORDER BY timestamp, id
        LIMIT 1 OFFSET ?
        """,
        (conversation_id, exchange_index - 1),
    )
    row = cur.fetchone()
    return row["id"] if row else None
