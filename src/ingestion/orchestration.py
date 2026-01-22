"""Orchestration: coordinate ingestion pipeline."""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from domain import Source
from storage.sqlite import (
    store_conversation,
    check_file_ingested,
    record_ingested_file,
    compute_file_hash,
    find_conversation_by_external_id,
    get_harness_id_by_name,
    delete_conversation,
    get_or_create_harness,
)
from .discovery import discover_all


@dataclass
class IngestStats:
    """Statistics from an ingestion run."""
    files_found: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    files_replaced: int = 0
    conversations: int = 0
    prompts: int = 0
    responses: int = 0
    tool_calls: int = 0
    by_harness: dict = field(default_factory=dict)


def _compare_timestamps(new_ts: str | None, existing_ts: str | None) -> bool:
    """Return True if new_ts is newer than existing_ts.

    None is treated as oldest (so any timestamp beats None).
    """
    if new_ts is None:
        return False
    if existing_ts is None:
        return True
    return new_ts > existing_ts


def ingest_all(
    conn: sqlite3.Connection,
    adapters: list,
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

    for source, adapter in discover_all(adapters):
        stats.files_found += 1
        file_path = str(source.location)
        harness_name = adapter.NAME
        dedup_strategy = getattr(adapter, "DEDUP_STRATEGY", "file")

        # Initialize per-harness stats
        if harness_name not in stats.by_harness:
            stats.by_harness[harness_name] = {
                "files": 0, "conversations": 0,
                "prompts": 0, "responses": 0, "tool_calls": 0,
                "replaced": 0,
            }

        try:
            # Strategy: file-based dedup
            if dedup_strategy == "file":
                if check_file_ingested(conn, file_path):
                    stats.files_skipped += 1
                    if on_file:
                        on_file(source, "skipped")
                    continue

                # Ingest the file
                _ingest_file(conn, source, adapter, file_path, stats)
                if on_file:
                    on_file(source, "ingested")

            # Strategy: session-based dedup (latest wins)
            elif dedup_strategy == "session":
                # We need to parse first to get the conversation and check timestamps
                conversations = list(adapter.parse(source))
                if not conversations:
                    stats.files_skipped += 1
                    if on_file:
                        on_file(source, "skipped (empty)")
                    continue

                for conversation in conversations:
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
                            location = Path(source.location) if not isinstance(source.location, Path) else source.location
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
                            stats.files_skipped += 1
                            if on_file:
                                on_file(source, "skipped (older)")
                    else:
                        # New conversation
                        conv_id = store_conversation(conn, conversation)

                        location = Path(source.location) if not isinstance(source.location, Path) else source.location
                        file_hash = compute_file_hash(location)
                        record_ingested_file(conn, file_path, file_hash, conv_id)

                        conn.commit()

                        _update_stats_for_conversation(stats, harness_name, conversation)
                        stats.files_ingested += 1

                        if on_file:
                            on_file(source, "ingested")

        except Exception as e:
            conn.rollback()
            if on_file:
                on_file(source, f"error: {e}")

    return stats


def _ingest_file(
    conn: sqlite3.Connection,
    source: Source,
    adapter,
    file_path: str,
    stats: IngestStats,
) -> None:
    """Ingest a single file (file-based dedup strategy)."""
    harness_name = adapter.NAME

    for conversation in adapter.parse(source):
        conv_id = store_conversation(conn, conversation)

        _update_stats_for_conversation(stats, harness_name, conversation)

        # Record ingestion
        location = Path(source.location) if not isinstance(source.location, Path) else source.location
        file_hash = compute_file_hash(location)
        record_ingested_file(conn, file_path, file_hash, conv_id)

    conn.commit()
    stats.files_ingested += 1


def _update_stats_for_conversation(
    stats: IngestStats,
    harness_name: str,
    conversation,
) -> None:
    """Update stats counters for a conversation."""
    stats.conversations += 1
    stats.by_harness[harness_name]["conversations"] += 1
    stats.by_harness[harness_name]["files"] += 1

    for prompt in conversation.prompts:
        stats.prompts += 1
        stats.by_harness[harness_name]["prompts"] += 1
        for response in prompt.responses:
            stats.responses += 1
            stats.by_harness[harness_name]["responses"] += 1
            stats.tool_calls += len(response.tool_calls)
            stats.by_harness[harness_name]["tool_calls"] += len(response.tool_calls)
