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
from siftd.model_names import parse_model_name
from siftd.storage.sqlite import get_or_create_provider, insert_response_attribute
from siftd.storage.tags import (
    DERIVATIVE_TAG,
    apply_tag,
    get_or_create_tag,
    is_derivative_tool_call,
)


def backfill_models(conn: sqlite3.Connection) -> int:
    """Backfill parsed fields for existing model rows with NULL fields.

    Updates rows where creator/family/version/variant are NULL.
    Returns count of rows updated.
    """
    cur = conn.execute(
        "SELECT id, raw_name FROM models WHERE creator IS NULL OR family IS NULL"
    )
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        parsed = parse_model_name(row["raw_name"])
        # Skip if parsing produced no useful info (fallback case)
        if parsed["creator"] is None:
            continue
        conn.execute(
            """UPDATE models
               SET name = ?, creator = ?, family = ?, version = ?, variant = ?, released = ?
               WHERE id = ?""",
            (parsed["name"], parsed["creator"], parsed["family"],
             parsed["version"], parsed["variant"], parsed["released"], row["id"]),
        )
        updated += 1
    conn.commit()
    return updated


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

        # Update responses that belong to conversations from this harness
        cur = conn.execute("""
            UPDATE responses SET provider_id = ?
            WHERE provider_id IS NULL
              AND conversation_id IN (
                  SELECT id FROM conversations WHERE harness_id = ?
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
        SELECT tc.id, tc.input
        FROM tool_calls tc
        WHERE tc.tool_id = ?
        AND tc.id NOT IN (
            SELECT tct.tool_call_id
            FROM tool_call_tags tct
            JOIN tags t ON t.id = tct.tag_id
            WHERE t.name LIKE 'shell:%'
        )
    """, (shell_tool_id,))

    # Cache for tag IDs
    tag_cache: dict[str, str] = {}
    counts: dict[str, int] = {}

    for row in cur.fetchall():
        tool_call_id = row["id"]
        raw_input = row["input"]

        # Extract command from JSON input
        try:
            data = json.loads(raw_input)
            cmd = data.get("command") or data.get("cmd") or ""
        except (json.JSONDecodeError, TypeError):
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
    then stores them as response_attributes.

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
                "SELECT id FROM responses WHERE conversation_id = ? AND external_id = ?",
                (conversation_id, response_external_id)
            ).fetchone()
            if not row:
                continue
            response_id = row["id"]

            if cache_creation:
                insert_response_attribute(
                    conn, response_id, "cache_creation_input_tokens",
                    str(cache_creation), scope="provider"
                )
                inserted += 1
            if cache_read:
                insert_response_attribute(
                    conn, response_id, "cache_read_input_tokens",
                    str(cache_read), scope="provider"
                )
                inserted += 1

    conn.commit()
    return inserted


def backfill_derivative_tags(conn: sqlite3.Connection) -> int:
    """Backfill siftd:derivative tags on conversations with siftd ask/query tool calls.

    Scans all tool calls for shell.execute commands containing 'siftd ask' or
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
            "SELECT conversation_id FROM conversation_tags WHERE tag_id = ?",
            (tag_row["id"],)
        ).fetchall()
        already_tagged = {r["conversation_id"] for r in rows}

    # Find candidate tool calls from relevant tools
    placeholders = ",".join("?" * len(tool_ids))
    tool_id_list = list(tool_ids.values())
    cur = conn.execute(f"""
        SELECT tc.conversation_id, tc.input, t.name AS tool_name
        FROM tool_calls tc
        JOIN tools t ON t.id = tc.tool_id
        WHERE tc.tool_id IN ({placeholders})
    """, tool_id_list)

    # Collect conversation IDs that need tagging
    derivative_conv_ids: set[str] = set()
    for row in cur.fetchall():
        conv_id = row["conversation_id"]
        if conv_id in already_tagged or conv_id in derivative_conv_ids:
            continue

        raw_input = row["input"]
        try:
            data = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
        except (json.JSONDecodeError, TypeError):
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
