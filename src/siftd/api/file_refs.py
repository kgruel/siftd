"""File reference queries for search results."""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from siftd.safecall import parse_json
from siftd.storage.sql_helpers import batched_in_query
from siftd.storage.turns import TurnSpan, load_turns


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


_FILE_OPS = {"file.read": "r", "file.write": "w", "file.edit": "e"}


def fetch_turn_file_refs(
    conn: sqlite3.Connection,
    conversation_id: str,
    ordinals: Sequence[int],
    *,
    turns: Sequence[TurnSpan] | None = None,
) -> dict[int, list[FileRef]]:
    """File references of the turns at ``ordinals`` of one conversation, keyed by ordinal.

    A turn's calls are those of its responses (:mod:`siftd.storage.turns`), in
    the order they were made, so the preamble turn's calls are found like any
    other's. Every requested ordinal is a key; a turn with no file calls maps
    to an empty list. A caller that already folded the conversation passes its
    ``turns`` so the transcript is not read and folded again.
    """
    if turns is None:
        turns = load_turns(conn, conversation_id)
    wanted = {o: turns[o].tool_call_ids for o in ordinals if 0 <= o < len(turns)}
    call_ids = [cid for ids in wanted.values() for cid in ids]
    marks = ",".join("?" * len(_FILE_OPS))
    rows = batched_in_query(
        conn,
        f"""
        SELECT e.id AS tool_call_id, t.name AS tool_name,
               etc.input AS input_json,
               cb.content AS result_json
        FROM events e
        JOIN event_tool_call etc ON etc.event_id = e.id
        JOIN tools t ON t.id = etc.tool_id
        LEFT JOIN content_blobs cb ON etc.result_hash = cb.hash
        WHERE e.id IN ({{placeholders}})
          AND t.name IN ({marks})
        """,
        call_ids,
        suffix_params=tuple(_FILE_OPS),
    )

    ref_of_call: dict[str, FileRef] = {}
    for row in rows:
        input_data = parse_json(row["input_json"], fallback={}) if row["input_json"] else {}

        path = input_data.get("file_path")
        if not path:
            continue

        ref_of_call[row["tool_call_id"]] = FileRef(
            path=path,
            basename=Path(path).name,
            op=_FILE_OPS.get(row["tool_name"], "?"),
            content=_extract_file_content(row["result_json"]),
        )
    return {o: [ref_of_call[cid] for cid in ids if cid in ref_of_call] for o, ids in wanted.items()}


def fetch_file_refs(
    conn: sqlite3.Connection,
    source_ids: list[str],
) -> dict[str, list[FileRef]]:
    """Batch query: prompt_ids → file references from the tool calls of each prompt's turn.

    Args:
        conn: Database connection with row_factory set.
        source_ids: List of prompt IDs to fetch file refs for. An id that is not
            a prompt has no entry (a response or preamble turn is reached by
            ordinal, through :func:`fetch_turn_file_refs`).

    Returns:
        Dict mapping prompt_id to list of FileRef for file.read/write/edit calls.
    """
    if not source_ids:
        return {}

    events = batched_in_query(
        conn,
        "SELECT id, kind, conversation_id FROM events WHERE id IN ({placeholders})",
        source_ids,
    )
    by_conv: dict[str, list[str]] = {}
    for row in events:
        if row["kind"] == "prompt":
            by_conv.setdefault(row["conversation_id"], []).append(row["id"])

    refs_by_prompt: dict[str, list[FileRef]] = {}
    for conv_id, prompt_ids in by_conv.items():
        wanted = set(prompt_ids)
        ordinal_of = {t.prompt_id: o for o, t in enumerate(load_turns(conn, conv_id)) if t.prompt_id in wanted}
        refs = fetch_turn_file_refs(conn, conv_id, list(ordinal_of.values()))
        for pid, ordinal in ordinal_of.items():
            if refs[ordinal]:
                refs_by_prompt[pid] = refs[ordinal]
    return refs_by_prompt
