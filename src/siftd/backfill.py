"""Backfill operations for siftd.

These are maintenance operations that re-derive data from existing records.
They use storage primitives but are not storage primitives themselves.
"""

import json
import sqlite3
from pathlib import Path

from siftd.domain.shell_categories import (
    SHELL_TAG_PREFIX,
    categorize_shell_command,
)
from siftd.safecall import parse_json
from siftd.storage.attributes import set_attribute
from siftd.storage.sqlite import get_or_create_provider
from siftd.storage.tags import (
    DERIVATIVE_TAG,
    apply_tag,
    get_or_create_tag,
    is_derivative_tool_call,
)


def backfill_models(conn: sqlite3.Connection, *, commit: bool = True) -> int:
    """Re-parse ``raw_name`` → canonical fields for model rows the parser fell back on.

    Thin wrapper over the storage primitive :func:`recanonicalize_model_names`
    (the logic lives in storage so a schema migration can reuse it without an
    ingestion-layer import). Returns the count of rows updated.
    """
    from siftd.storage.sqlite import recanonicalize_model_names

    return recanonicalize_model_names(conn, commit=commit)


def backfill_providers(conn: sqlite3.Connection) -> int:
    """Backfill provider_id on responses where it's NULL.

    Derives provider from the conversation's harness source field.
    Returns count of rows updated.
    """
    # Get harness name → source mapping
    cur = conn.execute("SELECT id, name, source FROM harnesses WHERE source IS NOT NULL")
    harness_rows = cur.fetchall()
    if not harness_rows:
        return 0

    updated = 0
    for harness_row in harness_rows:
        harness_id = harness_row["id"]
        source = harness_row["source"]
        provider_id = get_or_create_provider(conn, source)

        # Update event_response rows that belong to conversations from this harness
        cur = conn.execute("""
            UPDATE event_response SET provider_id = ?
            WHERE provider_id IS NULL
              AND event_id IN (
                  SELECT e.id FROM events e
                  JOIN conversations c ON c.id = e.conversation_id
                  WHERE c.harness_id = ?
              )
        """, (provider_id, harness_id))
        updated += cur.rowcount

    conn.commit()
    return updated


def backfill_shell_tags(conn: sqlite3.Connection) -> dict[str, int]:
    """Backfill shell command tags for all shell.execute tool calls.

    Categorizes each shell.execute call and applies the appropriate shell:* tag.
    Skips tool calls that already have a shell:* tag.

    Returns dict of category -> count of newly tagged calls.
    """
    # Get shell.execute tool id
    cur = conn.execute("SELECT id FROM tools WHERE name = 'shell.execute'")
    row = cur.fetchone()
    if not row:
        return {}
    shell_tool_id = row["id"]

    # Find all shell.execute calls that don't already have a shell:* tag
    cur = conn.execute("""
        SELECT e.id, etc.input
        FROM events e
        JOIN event_tool_call etc ON etc.event_id = e.id
        WHERE e.kind = 'tool_call'
        AND etc.tool_id = ?
        AND e.id NOT IN (
            SELECT ta.target_id
            FROM tag_assignments ta
            JOIN tags t ON t.id = ta.tag_id
            WHERE ta.target_kind = 'tool_call' AND t.name LIKE 'shell:%'
        )
    """, (shell_tool_id,))

    # Cache for tag IDs
    tag_cache: dict[str, str] = {}
    counts: dict[str, int] = {}

    for row in cur.fetchall():
        tool_call_id = row["id"]
        raw_input = row["input"]

        # Extract command from JSON input
        data = parse_json(raw_input)
        if isinstance(data, dict):
            cmd = data.get("command") or data.get("cmd") or ""
        else:
            cmd = raw_input or ""

        # Categorize
        category = categorize_shell_command(cmd)
        if not category:
            continue

        # Get or create tag
        tag_name = f"{SHELL_TAG_PREFIX}{category}"
        if tag_name not in tag_cache:
            tag_cache[tag_name] = get_or_create_tag(conn, tag_name)

        # Apply tag
        result = apply_tag(conn, "tool_call", tool_call_id, tag_cache[tag_name])
        if result:
            counts[category] = counts.get(category, 0) + 1

    conn.commit()
    return counts


def backfill_response_attributes(conn: sqlite3.Connection) -> int:
    """Backfill cache token attributes by re-reading raw JSONL files.

    For each ingested claude_code file, re-parses the JSONL and extracts
    cache_creation_input_tokens / cache_read_input_tokens from message.usage,
    then stores them as polymorphic attributes (target_kind='response').

    Returns count of attributes inserted.
    """
    from siftd.adapters._jsonl import load_jsonl

    # Find all ingested claude_code files
    harness_row = conn.execute(
        "SELECT id FROM harnesses WHERE name = ?", ("claude_code",)
    ).fetchone()
    if not harness_row:
        return 0
    harness_id = harness_row["id"]

    files = conn.execute(
        "SELECT path, conversation_id FROM ingested_files WHERE harness_id = ?",
        (harness_id,)
    ).fetchall()

    inserted = 0
    for file_row in files:
        file_path = Path(file_row["path"])
        conversation_id = file_row["conversation_id"]
        if not file_path.exists():
            continue

        # Re-read the raw JSONL to extract cache tokens
        records = load_jsonl(file_path)

        # Match responses by external_id
        for record in records:
            if record.get("type") != "assistant":
                continue
            message_data = record.get("message") or {}
            usage_data = message_data.get("usage") or {}
            external_msg_id = record.get("uuid")
            if not external_msg_id:
                continue

            cache_creation = usage_data.get("cache_creation_input_tokens")
            cache_read = usage_data.get("cache_read_input_tokens")
            if not cache_creation and not cache_read:
                continue

            # Find the response in DB
            response_external_id = f"claude_code::{external_msg_id}"
            row = conn.execute(
                "SELECT id FROM events WHERE kind = 'response' AND conversation_id = ? AND external_id = ?",
                (conversation_id, response_external_id)
            ).fetchone()
            if not row:
                continue
            response_id = row["id"]

            if cache_creation:
                set_attribute(conn, "response", response_id, "cache_creation_input_tokens",
                              str(cache_creation), scope="provider")
                inserted += 1
            if cache_read:
                set_attribute(conn, "response", response_id, "cache_read_input_tokens",
                              str(cache_read), scope="provider")
                inserted += 1

    conn.commit()
    return inserted


def backfill_derivative_tags(conn: sqlite3.Connection) -> int:
    """Backfill siftd:derivative tags on conversations with siftd search/query tool calls.

    Scans all tool calls for shell.execute commands containing 'siftd search' or
    'siftd query', and skill.invoke calls for the 'siftd' skill. Tags the
    parent conversation. Skips conversations already tagged.

    Returns count of newly tagged conversations.
    """
    # Find tool IDs for shell.execute and skill.invoke
    tool_ids = {}
    for name in ("shell.execute", "skill.invoke"):
        row = conn.execute("SELECT id FROM tools WHERE name = ?", (name,)).fetchone()
        if row:
            tool_ids[name] = row["id"]

    if not tool_ids:
        return 0

    # Get conversations already tagged as derivative
    already_tagged = set()
    tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (DERIVATIVE_TAG,)).fetchone()
    if tag_row:
        rows = conn.execute(
            "SELECT target_id FROM tag_assignments WHERE target_kind='conversation' AND tag_id = ?",
            (tag_row["id"],)
        ).fetchall()
        already_tagged = {r["target_id"] for r in rows}

    # Find candidate tool calls from relevant tools
    placeholders = ",".join("?" * len(tool_ids))
    tool_id_list = list(tool_ids.values())
    cur = conn.execute(f"""
        SELECT e.conversation_id, etc.input, t.name AS tool_name
        FROM events e
        JOIN event_tool_call etc ON etc.event_id = e.id
        JOIN tools t ON t.id = etc.tool_id
        WHERE e.kind = 'tool_call'
        AND etc.tool_id IN ({placeholders})
    """, tool_id_list)

    # Collect conversation IDs that need tagging
    derivative_conv_ids: set[str] = set()
    for row in cur.fetchall():
        conv_id = row["conversation_id"]
        if conv_id in already_tagged or conv_id in derivative_conv_ids:
            continue

        raw_input = row["input"]
        data = parse_json(raw_input) if isinstance(raw_input, str) else raw_input
        if data is None:
            continue

        if is_derivative_tool_call(row["tool_name"], data):
            derivative_conv_ids.add(conv_id)

    # Apply tags
    if derivative_conv_ids:
        tag_id = get_or_create_tag(conn, DERIVATIVE_TAG)
        for conv_id in derivative_conv_ids:
            apply_tag(conn, "conversation", conv_id, tag_id)

    conn.commit()
    return len(derivative_conv_ids)


def backfill_filter_binary(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, int]:
    """Filter binary content from existing content_blobs.

    Scans content_blobs for binary content (images, base64 data) and replaces
    with filtered versions. Since content_blobs uses content-addressable storage,
    this creates new filtered blobs and updates event_tool_call.result_hash to point
    to them.

    Args:
        conn: Database connection
        dry_run: If True, only report what would be filtered without making changes

    Returns:
        Dict with counts: filtered, skipped, errors
    """
    from siftd.content.filters import filter_tool_result_binary
    from siftd.storage.blobs import compute_content_hash

    stats = {"filtered": 0, "skipped": 0, "errors": 0}

    # Find all content_blobs that might contain binary data
    cur = conn.execute("""
        SELECT hash, content FROM content_blobs
        WHERE content LIKE '%"type": "base64"%'
           OR content LIKE '%"type":"base64"%'
           OR content LIKE '%iVBORw0KGgo%'
           OR content LIKE '%JVBERi0%'
           OR content LIKE '%/9j/%'
    """)

    rows = cur.fetchall()
    hash_mapping: dict[str, str] = {}  # old_hash -> new_hash

    for row in rows:
        old_hash = row["hash"]
        content = row["content"]

        data = parse_json(content)
        if not isinstance(data, dict):
            stats["errors"] += 1
            continue

        filtered_data = filter_tool_result_binary(data)

        # Check if anything changed
        if filtered_data is data:
            stats["skipped"] += 1
            continue

        filtered_json = json.dumps(filtered_data)
        new_hash = compute_content_hash(filtered_json)

        if new_hash == old_hash:
            stats["skipped"] += 1
            continue

        if not dry_run:
            # Insert blob with ref_count=0; the AFTER UPDATE trigger on
            # event_tool_call.result_hash handles all ref_count bookkeeping.
            from datetime import UTC, datetime

            conn.execute(
                "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(hash) DO NOTHING",
                (new_hash, filtered_json, datetime.now(UTC).isoformat()),
            )
            hash_mapping[old_hash] = new_hash

        stats["filtered"] += 1

    # Update event_tool_call to point to new hashes — the AFTER UPDATE trigger
    # decrements old blob ref_count and increments new blob ref_count for
    # each row, then garbage-collects blobs that reach ref_count <= 0.
    if not dry_run and hash_mapping:
        for old_hash, new_hash in hash_mapping.items():
            conn.execute(
                "UPDATE event_tool_call SET result_hash = ? WHERE result_hash = ?",
                (new_hash, old_hash),
            )

        conn.commit()

    return stats
