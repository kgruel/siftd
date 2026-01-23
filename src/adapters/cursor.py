"""Cursor adapter for tbd-v2.

Reads Cursor's SQLite key-value store (state.vscdb) and yields Conversation
domain objects. Uses two-phase lookup: composer → bubble IDs → bubble data.

The DB can be 50+ GB — all queries use targeted key lookups, never full scans.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

from domain import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    ToolCall,
    Usage,
)
from domain.source import Source

# Adapter self-description
NAME = "cursor"
DEFAULT_LOCATIONS = [
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",  # macOS
    "~/.config/Cursor/User/globalStorage/state.vscdb",  # Linux
]
SOURCE_KINDS = ["sqlite"]
DEDUP_STRATEGY = "session"  # one conversation per composer ID

# Harness metadata
HARNESS_SOURCE = "cursor"
HARNESS_LOG_FORMAT = "sqlite_kv"
HARNESS_DISPLAY_NAME = "Cursor"

# Raw tool name → canonical tool name
# toolFormerData contains structured tool calls but names aren't well-documented.
# Map what we can identify from the data structure.
TOOL_ALIASES: dict[str, str] = {
    "file_edit": "file.edit",
    "terminal_command": "shell.execute",
    "search": "search.grep",
}

# Bubble type constants
_BUBBLE_USER = 1
_BUBBLE_ASSISTANT = 2


def discover() -> Iterable[Source]:
    """Yield one Source per composer (conversation) found in Cursor's DB."""
    for location in DEFAULT_LOCATIONS:
        db_path = Path(location).expanduser()
        if not db_path.exists():
            continue

        table = _detect_table(db_path)
        if table is None:
            continue

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT key FROM [{table}] WHERE key LIKE 'composerData:%'"
            )
            for row in cursor:
                composer_id = row["key"].removeprefix("composerData:")
                yield Source(
                    kind="sqlite",
                    location=db_path,
                    metadata={"composer_id": composer_id, "table": table},
                )

            conn.close()
        except sqlite3.Error as e:
            print(f"Warning: cursor adapter: error reading {db_path}: {e}", file=sys.stderr)
            continue


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "sqlite":
        return False
    path = Path(source.location)
    return path.name == "state.vscdb"


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Cursor composer and yield a Conversation object.

    Each Source represents one composer (conversation). Reconstructs the
    conversation by fetching the composer metadata then each bubble.
    """
    db_path = Path(source.location)
    composer_id = source.metadata.get("composer_id")
    table = source.metadata.get("table")

    if not composer_id or not table:
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        print(f"Warning: cursor adapter: cannot open {db_path}: {e}", file=sys.stderr)
        return

    try:
        conversation = _parse_composer(conn, table, composer_id)
        if conversation is not None:
            yield conversation
    finally:
        conn.close()


def _detect_table(db_path: Path) -> str | None:
    """Detect which table holds KV data: cursorDiskKV (current) or ItemTable (legacy)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('cursorDiskKV', 'ItemTable')"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        if "cursorDiskKV" in tables:
            return "cursorDiskKV"
        if "ItemTable" in tables:
            return "ItemTable"
        return None
    except sqlite3.Error:
        return None


def _get_value(conn: sqlite3.Connection, table: str, key: str) -> dict | None:
    """Fetch and parse a JSON value from the KV table."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT value FROM [{table}] WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_composer(conn: sqlite3.Connection, table: str, composer_id: str) -> Conversation | None:
    """Parse a single composer into a Conversation."""
    composer = _get_value(conn, table, f"composerData:{composer_id}")
    if composer is None:
        return None

    # Extract metadata
    created_at = composer.get("createdAt")
    last_updated = composer.get("lastUpdatedAt")
    is_agentic = composer.get("isAgentic", False)

    # Timestamps are in milliseconds
    started_at = _ms_to_iso(created_at) if created_at else _now()
    ended_at = _ms_to_iso(last_updated) if last_updated else None

    # Model and cost from usageData
    usage_data = composer.get("usageData", {})
    default_model = None
    if usage_data:
        # usageData is keyed by model name; pick the one with highest cost as "default"
        default_model = max(usage_data.keys(), key=lambda m: usage_data[m].get("costInCents", 0), default=None)

    harness = Harness(
        name=NAME,
        source=HARNESS_SOURCE,
        log_format=HARNESS_LOG_FORMAT,
        display_name=HARNESS_DISPLAY_NAME,
    )

    external_id = f"{NAME}::{composer_id}"

    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at,
        ended_at=ended_at,
        default_model=default_model,
    )

    # Get bubble list from composer
    headers = composer.get("fullConversationHeadersOnly", [])
    if not headers:
        return conversation

    # Two-phase: fetch each bubble
    current_prompt: Prompt | None = None

    for header in headers:
        bubble_id = header.get("bubbleId")
        bubble_type = header.get("type")
        if not bubble_id:
            continue

        bubble = _get_value(conn, table, f"bubble:{bubble_id}")
        if bubble is None:
            continue

        if bubble_type == _BUBBLE_USER:
            current_prompt = _parse_user_bubble(bubble, bubble_id, started_at)
            conversation.prompts.append(current_prompt)

        elif bubble_type == _BUBBLE_ASSISTANT:
            response = _parse_assistant_bubble(bubble, bubble_id, usage_data, started_at)
            if current_prompt is not None:
                current_prompt.responses.append(response)
            else:
                # Orphan assistant bubble — create a synthetic prompt
                current_prompt = Prompt(
                    timestamp=started_at,
                    external_id=f"{NAME}::synthetic::{bubble_id}",
                )
                current_prompt.responses.append(response)
                conversation.prompts.append(current_prompt)

    return conversation


def _parse_user_bubble(bubble: dict, bubble_id: str, fallback_ts: str) -> Prompt:
    """Parse a user bubble into a Prompt."""
    prompt = Prompt(
        timestamp=fallback_ts,
        external_id=f"{NAME}::{bubble_id}",
    )

    text = bubble.get("text", "")
    if text:
        prompt.content.append(
            ContentBlock(block_type="text", content={"text": text})
        )

    # Attach context as content blocks
    relevant_files = bubble.get("relevantFiles", [])
    if relevant_files:
        prompt.content.append(
            ContentBlock(
                block_type="context",
                content={"relevant_files": relevant_files},
            )
        )

    return prompt


def _parse_assistant_bubble(
    bubble: dict, bubble_id: str, composer_usage: dict, fallback_ts: str
) -> Response:
    """Parse an assistant bubble into a Response."""
    # Token counts
    token_count = bubble.get("tokenCount", {})
    usage = None
    if token_count:
        usage = Usage(
            input_tokens=token_count.get("input"),
            output_tokens=token_count.get("output"),
        )

    response = Response(
        timestamp=fallback_ts,
        usage=usage,
        external_id=f"{NAME}::{bubble_id}",
    )

    # Thinking content (separate field)
    thinking = bubble.get("thinking")
    if thinking:
        response.content.append(
            ContentBlock(block_type="thinking", content={"thinking": thinking})
        )

    # Main text
    text = bubble.get("text", "")
    if text:
        response.content.append(
            ContentBlock(block_type="text", content={"text": text})
        )

    # Tool calls from toolFormerData
    tool_data = bubble.get("toolFormerData")
    if tool_data:
        _extract_tool_calls(tool_data, response)

    return response


def _extract_tool_calls(tool_data, response: Response) -> None:
    """Extract tool calls from toolFormerData and add to response.

    toolFormerData structure is not fully documented. It can be a dict or list
    containing file edits, terminal commands, etc. We extract what we can.
    """
    if isinstance(tool_data, dict):
        items = [tool_data]
    elif isinstance(tool_data, list):
        items = tool_data
    else:
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        # Try to identify the tool type from available keys
        tool_name = "unknown"
        tool_input = item

        if "command" in item or "terminalCommand" in item:
            tool_name = "terminal_command"
            tool_input = {"command": item.get("command") or item.get("terminalCommand")}
        elif "filePath" in item or "path" in item:
            tool_name = "file_edit"
            tool_input = {
                "path": item.get("filePath") or item.get("path"),
                "content": item.get("content"),
            }

        canonical = TOOL_ALIASES.get(tool_name, tool_name)

        tool_call = ToolCall(
            tool_name=canonical,
            input=tool_input,
            result=None,
            status="success",
        )
        response.tool_calls.append(tool_call)


def _ms_to_iso(ms: int) -> str:
    """Convert milliseconds epoch to ISO timestamp."""
    return datetime.fromtimestamp(ms / 1000).isoformat()


def _now() -> str:
    """ISO timestamp for now."""
    return datetime.now().isoformat()
