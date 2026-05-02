"""Derived projection and FTS index for tool-oriented retrieval."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

_TOOL_NAME_SPLIT_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


def ensure_tool_search_tables(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Create tool-search projection tables if they do not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_search (
            tool_call_id TEXT PRIMARY KEY REFERENCES tool_calls(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            response_id TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
            timestamp TEXT,
            tool_name TEXT,
            tool_family TEXT,
            tool_description TEXT,
            status TEXT,
            path TEXT,
            basename TEXT,
            ext TEXT,
            command TEXT,
            command_verb TEXT,
            pattern TEXT,
            arg TEXT,
            result_snippet TEXT,
            workspace_path TEXT,
            search_text TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS tool_search_fts USING fts5(
            search_text,
            tool_call_id UNINDEXED,
            content=''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_search_conversation ON tool_search(conversation_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_search_tool_name ON tool_search(tool_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_search_status ON tool_search(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_search_path ON tool_search(path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_search_command_verb ON tool_search(command_verb)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_search_timestamp ON tool_search(timestamp)"
    )
    # Keep contentless FTS in sync when tool_search rows are cascade-deleted.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_tool_search_fts_delete
        AFTER DELETE ON tool_search
        BEGIN
            INSERT INTO tool_search_fts(tool_search_fts, rowid, search_text, tool_call_id)
            VALUES ('delete', OLD.rowid, OLD.search_text, OLD.tool_call_id);
        END
        """
    )
    if commit:  # pragma: no cover — never called with commit=True internally
        conn.commit()


def rebuild_tool_search_index(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Rebuild the tool-search projection from tool_calls and related tables."""
    ensure_tool_search_tables(conn)
    conn.execute("DELETE FROM tool_search")
    conn.execute("DELETE FROM tool_search_fts")

    rows = conn.execute(
        """
        SELECT
            e.id AS tool_call_id,
            e.conversation_id,
            e.parent_id AS response_id,
            e.timestamp,
            etc.status,
            etc.input AS input_json,
            cb.content AS result_json,
            t.name AS tool_name,
            t.description AS tool_description,
            w.path AS workspace_path
        FROM events e
        JOIN event_tool_call etc ON etc.event_id = e.id
        LEFT JOIN tools t ON t.id = etc.tool_id
        LEFT JOIN content_blobs cb ON cb.hash = etc.result_hash
        LEFT JOIN conversations c ON c.id = e.conversation_id
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE e.kind = 'tool_call'
        ORDER BY e.timestamp, e.id
        """
    ).fetchall()

    for row in rows:
        proj = _project_tool_call(row)
        conn.execute(
            """
            INSERT INTO tool_search (
                tool_call_id, conversation_id, response_id, timestamp,
                tool_name, tool_family, tool_description, status,
                path, basename, ext, command, command_verb,
                pattern, arg, result_snippet, workspace_path, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["tool_call_id"],
                row["conversation_id"],
                row["response_id"],
                row["timestamp"],
                proj["tool_name"],
                proj["tool_family"],
                row["tool_description"],
                row["status"],
                proj["path"],
                proj["basename"],
                proj["ext"],
                proj["command"],
                proj["command_verb"],
                proj["pattern"],
                proj["arg"],
                proj["result_snippet"],
                row["workspace_path"],
                proj["search_text"],
            ),
        )
        conn.execute(
            "INSERT INTO tool_search_fts (rowid, search_text, tool_call_id) VALUES (last_insert_rowid(), ?, ?)",
            (proj["search_text"], row["tool_call_id"]),
        )

    if commit:
        conn.commit()


def _project_tool_call(row: sqlite3.Row) -> dict[str, str | None]:
    input_data = _loads_dict(row["input_json"])
    result_data = _loads_dict(row["result_json"])

    tool_name = row["tool_name"]
    tool_family = _tool_family(tool_name)
    path = _extract_path(input_data)
    basename = Path(path).name if path else None
    ext = Path(path).suffix[1:].lower() if path and Path(path).suffix else None
    command = _extract_command(input_data)
    command_verb = _command_verb(command)
    pattern = _extract_pattern(input_data)
    arg = _extract_arg(input_data)
    result_snippet = _extract_result_snippet(result_data)
    search_text = _build_search_text(
        tool_name=tool_name,
        tool_description=row["tool_description"],
        tool_family=tool_family,
        status=row["status"],
        path=path,
        basename=basename,
        ext=ext,
        command=command,
        command_verb=command_verb,
        pattern=pattern,
        arg=arg,
        result_snippet=result_snippet,
        workspace_path=row["workspace_path"],
    )

    return {
        "tool_name": tool_name,
        "tool_family": tool_family,
        "path": path,
        "basename": basename,
        "ext": ext,
        "command": command,
        "command_verb": command_verb,
        "pattern": pattern,
        "arg": arg,
        "result_snippet": result_snippet,
        "search_text": search_text,
    }


def _loads_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        obj = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _tool_family(tool_name: str | None) -> str | None:
    if not tool_name:
        return None
    return tool_name.split(".", 1)[0]


def _extract_path(input_data: dict) -> str | None:
    for key in ("file_path", "path"):
        value = input_data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_command(input_data: dict) -> str | None:
    value = input_data.get("command")
    return value if isinstance(value, str) and value else None


def _command_verb(command: str | None) -> str | None:
    if not command:
        return None
    return command.strip().split()[0] if command.strip() else None


def _extract_pattern(input_data: dict) -> str | None:
    value = input_data.get("pattern")
    return value if isinstance(value, str) and value else None


def _extract_arg(input_data: dict) -> str | None:
    for key in ("query", "url", "arg", "args"):
        value = input_data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_result_snippet(result_data: dict) -> str | None:
    for key in ("error", "stderr", "output", "content", "result"):
        value = result_data.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.strip().split())
            return text[:280]
    return None


def _normalize_tool_tokens(tool_name: str | None) -> str | None:
    if not tool_name:
        return None
    parts = [p for p in _TOOL_NAME_SPLIT_RE.split(tool_name.replace(".", " ")) if p]
    return " ".join(parts) if parts else tool_name


def _build_search_text(**parts: str | None) -> str:
    fields: list[str] = []
    if parts.get("tool_name"):
        fields.append(str(parts["tool_name"]))
        normalized = _normalize_tool_tokens(parts["tool_name"])
        if normalized and normalized != parts["tool_name"]:
            fields.append(normalized)
    for key in (
        "tool_description",
        "tool_family",
        "status",
        "path",
        "basename",
        "ext",
        "command",
        "command_verb",
        "pattern",
        "arg",
        "result_snippet",
        "workspace_path",
    ):
        value = parts.get(key)
        if value:
            fields.append(f"{key.replace('_', ' ')} {value}")
    return "\n".join(f for f in fields if f)
