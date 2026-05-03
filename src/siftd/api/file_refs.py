"""File reference queries for search results."""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from siftd.safecall import parse_json


@dataclass
class FileRef:
    """A file operation reference from a tool call."""

    path: str
    basename: str
    op: str  # 'r' (read), 'w' (write), 'e' (edit)
    content: str | None


def _strip_line_numbers(text: str) -> str:
    """Remove line number prefixes from Read tool output (e.g. '     1→' or '   123→')."""
    return re.sub(r"^\s*\d+\u2192", "", text, flags=re.MULTILINE)


def _extract_file_content(result_json: str | None) -> str | None:
    """Parse tool_call.result JSON and return clean file text."""
    if not result_json:
        return None

    result = parse_json(result_json)
    if not isinstance(result, dict):
        return None

    content = result.get("content") or result.get("output")
    if content is None:
        return None

    # Content might be a string or a list of content blocks
    if isinstance(content, str):
        return _strip_line_numbers(content)
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return _strip_line_numbers("\n".join(parts)) if parts else None

    return None


def fetch_file_refs(
    conn: sqlite3.Connection,
    source_ids: list[str],
) -> dict[str, list[FileRef]]:
    """Batch query: prompt_ids → file references from tool calls.

    Args:
        conn: Database connection with row_factory set.
        source_ids: List of prompt IDs to fetch file refs for.

    Returns:
        Dict mapping prompt_id to list of FileRef for file.read/write/edit calls.
    """
    if not source_ids:
        return {}

    placeholders = ",".join("?" * len(source_ids))
    rows = conn.execute(
        f"""
        SELECT e_r.parent_id AS prompt_id, t.name AS tool_name,
               etc.input AS input_json,
               cb.content AS result_json
        FROM events e
        JOIN event_tool_call etc ON etc.event_id = e.id
        JOIN events e_r ON e_r.id = e.parent_id AND e_r.kind = 'response'
        JOIN tools t ON t.id = etc.tool_id
        LEFT JOIN content_blobs cb ON etc.result_hash = cb.hash
        WHERE e_r.parent_id IN ({placeholders})
          AND e.kind = 'tool_call'
          AND t.name IN ('file.read', 'file.write', 'file.edit')
        ORDER BY e.timestamp
    """,
        source_ids,
    ).fetchall()

    refs_by_prompt: dict[str, list[FileRef]] = {}
    op_map = {"file.read": "r", "file.write": "w", "file.edit": "e"}

    for row in rows:
        input_data = parse_json(row["input_json"], fallback={}) if row["input_json"] else {}

        path = input_data.get("file_path")
        if not path:
            continue

        op = op_map.get(row["tool_name"], "?")
        ref = FileRef(
            path=path,
            basename=Path(path).name,
            op=op,
            content=_extract_file_content(row["result_json"]),
        )
        refs_by_prompt.setdefault(row["prompt_id"], []).append(ref)

    return refs_by_prompt
