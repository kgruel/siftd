"""Orchestration: coordinate ingestion pipeline.

Design constraint: One source file → at most one conversation.
----------------------------------------------------------
The `ingested_files.path` column is UNIQUE, meaning each file can map to exactly
one conversation. This is intentional: most adapters produce one conversation per
file (JSONL session logs, markdown exports, etc.).

If an adapter's parse() yields multiple conversations from a single source, we
warn and take only the first. Supporting multi-conversation sources (e.g., SQLite
DBs containing many sessions) would require schema changes to ingested_files —
revisit if a real use case emerges.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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

    If multiple conversations are parsed, warn and return only the first.
    Returns None if the list is empty.
    """
    if not conversations:
        return None
    if len(conversations) > 1:
        logger.warning(
            "Source %s yielded %d conversations; taking first only "
            "(schema requires 1:1 file→conversation mapping)",
            source_path,
            len(conversations),
        )
    return conversations[0]


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
                    # Compare hash to detect changes
                    location = source.as_path
                    current_hash = compute_file_hash(location)

                    if current_hash == existing_info["file_hash"]:
                        # Same hash, skip
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
                        conn, source, adapter, file_path, current_hash, stats, filter_binary
                    )
                    if on_file:
                        on_file(source, "updated")
                    emit_event("updated", conversation=conv)
                    continue

                # New file - ingest normally
                conv = _ingest_file(conn, source, adapter, file_path, stats, filter_binary)
                if on_file:
                    on_file(source, "ingested")
                emit_event("ingested", conversation=conv)

            # Strategy: session-based dedup (latest wins)
            elif dedup_strategy == "session":
                # We need to parse first to get the conversation and check timestamps
                conversations = list(adapter.parse(source))
                conversation = _get_single_conversation(conversations, file_path)
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
                    # Compare timestamps
                    if _compare_timestamps(conversation.ended_at, existing["ended_at"]):
                        # New is newer, replace
                        delete_conversation(conn, existing["id"])
                        conv_id = store_conversation(conn, conversation, filter_binary=filter_binary)

                        # Record file ingestion
                        location = source.as_path
                        file_hash = compute_file_hash(location)
                        record_ingested_file(conn, file_path, file_hash, conv_id)

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
                            location = source.as_path
                            file_hash = compute_file_hash(location)
                            record_ingested_file(conn, file_path, file_hash, existing["id"])
                            conn.commit()
                        stats.files_skipped += 1
                        if on_file:
                            on_file(source, "skipped (older)")
                        emit_event("skipped (older)")
                else:
                    # New conversation
                    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary)

                    location = source.as_path
                    file_hash = compute_file_hash(location)
                    record_ingested_file(conn, file_path, file_hash, conv_id)

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
                    conversations_retry = list(adapter.parse(source))
                    conv = _get_single_conversation(conversations_retry, file_path)
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
                            fh = compute_file_hash(location)
                            record_ingested_file(conn, file_path, fh, existing["id"])
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
    """Record a file that failed ingestion so it won't retry."""
    try:
        if get_ingested_file_info(conn, file_path):
            return  # Already recorded from a previous run
        location = source.as_path
        file_hash = compute_file_hash(location)
        harness_kwargs = {}
        if hasattr(adapter, "HARNESS_SOURCE"):
            harness_kwargs["source"] = adapter.HARNESS_SOURCE
        harness_id = get_or_create_harness(conn, adapter.NAME, **harness_kwargs)
        record_failed_file(conn, file_path, file_hash, harness_id, error)
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
) -> object | None:
    """Ingest a single file (file-based dedup strategy)."""
    harness_name = adapter.NAME
    location = source.as_path
    file_hash = compute_file_hash(location)

    conversations = list(adapter.parse(source))
    conversation = _get_single_conversation(conversations, file_path)

    if conversation is None:
        # Empty file - record with NULL conversation_id
        harness_kwargs = {}
        if hasattr(adapter, "HARNESS_SOURCE"):
            harness_kwargs["source"] = adapter.HARNESS_SOURCE
        harness_id = get_or_create_harness(conn, harness_name, **harness_kwargs)
        record_empty_file(conn, file_path, file_hash, harness_id)
        conn.commit()
        stats.files_ingested += 1
        return None

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary)
    _update_stats_for_conversation(stats, harness_name, conversation)
    record_ingested_file(conn, file_path, file_hash, conv_id)

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
    stats: IngestStats,
    filter_binary: bool,
) -> object | None:
    """Re-ingest a file that has changed (file-based dedup strategy).

    Unlike _ingest_file, the old conversation has already been deleted
    and the file hash is already computed.

    Note: delete_conversation also deletes the ingested_files record,
    so we create a new record rather than updating.
    """
    harness_name = adapter.NAME

    conversations = list(adapter.parse(source))
    conversation = _get_single_conversation(conversations, file_path)

    if conversation is None:
        # File became empty - record with NULL conversation_id
        harness_kwargs = {}
        if hasattr(adapter, "HARNESS_SOURCE"):
            harness_kwargs["source"] = adapter.HARNESS_SOURCE
        harness_id = get_or_create_harness(conn, harness_name, **harness_kwargs)
        record_empty_file(conn, file_path, file_hash, harness_id)
        conn.commit()
        stats.files_replaced += 1
        stats.by_harness[harness_name]["replaced"] += 1
        return None

    conv_id = store_conversation(conn, conversation, filter_binary=filter_binary)
    _update_stats_for_conversation(stats, harness_name, conversation)
    record_ingested_file(conn, file_path, file_hash, conv_id)

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

    if not pending:
        # No pending tags, but still unregister the session
        unregister_session(conn, session_id)
        return 0

    applied = 0
    for pt in pending:
        tag_id = get_or_create_tag(conn, pt.tag_name)

        if pt.entity_type == "conversation":
            result = apply_tag(conn, "conversation", conversation_id, tag_id)
            if result:
                applied += 1
                logger.debug(f"Applied tag '{pt.tag_name}' to conversation {conversation_id[:12]}")

        elif pt.entity_type == "exchange":
            # Look up the prompt at exchange_index
            prompt_id = _get_prompt_by_index(conn, conversation_id, pt.exchange_index)
            if prompt_id:
                result = apply_tag(conn, "prompt", prompt_id, tag_id)
                if result:
                    applied += 1
                    logger.debug(
                        f"Applied tag '{pt.tag_name}' to prompt {prompt_id[:12]} "
                        f"(exchange {pt.exchange_index})"
                    )
            else:
                logger.warning(
                    f"Exchange index {pt.exchange_index} not found for session {session_id[:8]}; "
                    f"tag '{pt.tag_name}' not applied"
                )

    # Unregister the session
    unregister_session(conn, session_id)
    return applied


def _get_prompt_by_index(
    conn: sqlite3.Connection,
    conversation_id: str,
    exchange_index: int | None,
) -> str | None:
    """Get the prompt ID at a specific exchange index (0-based).

    Returns None if index is out of range or None.
    """
    if exchange_index is None:
        return None

    cur = conn.execute(
        """
        SELECT id FROM prompts
        WHERE conversation_id = ?
        ORDER BY timestamp
        LIMIT 1 OFFSET ?
        """,
        (conversation_id, exchange_index),
    )
    row = cur.fetchone()
    return row["id"] if row else None
