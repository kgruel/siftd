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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from siftd.domain import Source
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


def ingest_all(
    conn: sqlite3.Connection,
    adapters: list[AdapterModule],
    *,
    on_file: Callable[[Source, str], None] | None = None,
) -> IngestStats:
    """Discover and ingest all new files from all adapters.

    Handles two dedup strategies:
    - "file": one conversation per file, skip if file already ingested
    - "session": one conversation per session, replace if newer

    Args:
        conn: Database connection
        adapters: List of adapter modules
        on_file: Optional callback for progress reporting

    Returns:
        IngestStats with counts
    """
    stats = IngestStats()

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

    for source, adapter in discover_all(adapters):
        stats.files_found += 1
        file_path = str(source.location)
        harness_name = adapter.NAME
        dedup_strategy = getattr(adapter, "DEDUP_STRATEGY", "file")

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
                        continue

                    # Hash changed - re-ingest
                    # Delete old conversation/record
                    if existing_info["conversation_id"]:
                        delete_conversation(conn, existing_info["conversation_id"])
                    else:
                        # No conversation (empty or errored file) — remove old record
                        clear_ingested_file_error(conn, file_path)

                    # Re-ingest and update the record
                    _reingest_file(conn, source, adapter, file_path, current_hash, stats)
                    if on_file:
                        on_file(source, "updated")
                    continue

                # New file - ingest normally
                _ingest_file(conn, source, adapter, file_path, stats)
                if on_file:
                    on_file(source, "ingested")

            # Strategy: session-based dedup (latest wins)
            elif dedup_strategy == "session":
                # We need to parse first to get the conversation and check timestamps
                conversations = list(adapter.parse(source))
                conversation = _get_single_conversation(conversations, file_path)
                if conversation is None:
                    stats.files_skipped += 1
                    if on_file:
                        on_file(source, "skipped (empty)")
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
                        conv_id = store_conversation(conn, conversation)

                        # Record file ingestion
                        location = source.as_path
                        file_hash = compute_file_hash(location)
                        record_ingested_file(conn, file_path, file_hash, conv_id)

                        conn.commit()

                        # Update stats
                        _update_stats_for_conversation(stats, harness_name, conversation)
                        stats.files_replaced += 1
                        stats.by_harness[harness_name]["replaced"] += 1

                        if on_file:
                            on_file(source, "replaced")
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
                else:
                    # New conversation
                    conv_id = store_conversation(conn, conversation)

                    location = source.as_path
                    file_hash = compute_file_hash(location)
                    record_ingested_file(conn, file_path, file_hash, conv_id)

                    conn.commit()

                    _update_stats_for_conversation(stats, harness_name, conversation)
                    stats.files_ingested += 1

                    if on_file:
                        on_file(source, "ingested")

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
                            continue
                    _record_file_error(conn, source, adapter, file_path, error_msg, stats, on_file)
                    continue
                except Exception:
                    pass
            # Other IntegrityError (not duplicate conversation) — record as error
            _record_file_error(conn, source, adapter, file_path, error_msg, stats, on_file)

        except Exception as e:
            conn.rollback()
            _record_file_error(conn, source, adapter, file_path, str(e), stats, on_file)

    return stats


def _record_file_error(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    file_path: str,
    error: str,
    stats: IngestStats,
    on_file: Callable[[Source, str], None] | None,
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


def _ingest_file(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    file_path: str,
    stats: IngestStats,
) -> None:
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
        return

    conv_id = store_conversation(conn, conversation)
    _update_stats_for_conversation(stats, harness_name, conversation)
    record_ingested_file(conn, file_path, file_hash, conv_id)

    conn.commit()
    stats.files_ingested += 1


def _reingest_file(
    conn: sqlite3.Connection,
    source: Source,
    adapter: AdapterModule,
    file_path: str,
    file_hash: str,
    stats: IngestStats,
) -> None:
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
        return

    conv_id = store_conversation(conn, conversation)
    _update_stats_for_conversation(stats, harness_name, conversation)
    record_ingested_file(conn, file_path, file_hash, conv_id)

    conn.commit()
    stats.files_replaced += 1
    stats.by_harness[harness_name]["replaced"] += 1


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
