"""Tag CRUD operations for siftd storage."""

import sqlite3
from datetime import datetime

from siftd.ids import ulid as _ulid
from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

# In-process cache for tag name -> id lookups.
# Only valid within a single connection lifetime. Cleared on module reload.
_tag_cache: dict[str, str] = {}


def _invalidate_tag_cache(*names: str) -> None:
    """Drop cached tag-name lookups that are no longer valid."""
    for name in names:
        _tag_cache.pop(name, None)


def get_or_create_tag(conn: sqlite3.Connection, name: str, description: str | None = None) -> str:
    """Get or create a tag by name, return id (ULID)."""
    if name in _tag_cache:
        return _tag_cache[name]

    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        _tag_cache[name] = row["id"]
        return row["id"]

    ulid = _ulid()
    conn.execute(
        "INSERT INTO tags (id, name, description, created_at) VALUES (?, ?, ?, ?)",
        (ulid, name, description, datetime.now().isoformat())
    )
    _tag_cache[name] = ulid
    return ulid


def get_tag_id(conn: sqlite3.Connection, name: str) -> str | None:
    """Return tag id for name, or None if not found."""
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    return row["id"] if row else None


def apply_tag(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> str | None:
    """Apply a tag to an entity. Returns assignment id or None if already applied.

    entity_type: 'conversation', 'workspace', 'tool_call', or 'prompt'
    """
    if entity_type == "conversation":
        table = "conversation_tags"
        fk_col = "conversation_id"
    elif entity_type == "workspace":
        table = "workspace_tags"
        fk_col = "workspace_id"
    elif entity_type == "tool_call":
        table = "tool_call_tags"
        fk_col = "tool_call_id"
    elif entity_type == "prompt":
        table = "prompt_tags"
        fk_col = "prompt_id"
    else:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    # Use INSERT OR IGNORE to skip duplicate check SELECT
    ulid = _ulid()
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {table} (id, {fk_col}, tag_id, applied_at) VALUES (?, ?, ?, ?)",
        (ulid, entity_id, tag_id, datetime.now().isoformat())
    )
    if cur.rowcount == 0:
        return None  # Already applied
    if commit:
        conn.commit()
    return ulid


def remove_tag(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Remove a tag from an entity. Returns True if a row was deleted, False if not applied.

    entity_type: 'conversation', 'workspace', 'tool_call', or 'prompt'
    """
    if entity_type == "conversation":
        table = "conversation_tags"
        fk_col = "conversation_id"
    elif entity_type == "workspace":
        table = "workspace_tags"
        fk_col = "workspace_id"
    elif entity_type == "tool_call":
        table = "tool_call_tags"
        fk_col = "tool_call_id"
    elif entity_type == "prompt":
        table = "prompt_tags"
        fk_col = "prompt_id"
    else:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    cur = conn.execute(
        f"DELETE FROM {table} WHERE {fk_col} = ? AND tag_id = ?",
        (entity_id, tag_id)
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def rename_tag(conn: sqlite3.Connection, old_name: str, new_name: str, *, commit: bool = False) -> bool:
    """Rename a tag. Returns True if renamed, False if old_name not found.

    Raises ValueError if new_name already exists.
    """
    from siftd.storage.sessions import rename_pending_tag

    # Check new_name doesn't already exist
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (new_name,))
    if cur.fetchone():
        raise ValueError(f"Tag '{new_name}' already exists")

    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (old_name,))
    row = cur.fetchone()
    if not row:
        return False

    tag_id = row["id"]
    cur = conn.execute("UPDATE tags SET name = ? WHERE name = ?", (new_name, old_name))
    rename_pending_tag(conn, old_name, new_name)
    _invalidate_tag_cache(old_name, new_name)
    _tag_cache[new_name] = tag_id
    if commit:
        conn.commit()
    return cur.rowcount > 0


def delete_tag(conn: sqlite3.Connection, name: str, *, commit: bool = False) -> int:
    """Delete a tag and all its associations. Returns count of entity associations removed."""
    from siftd.storage.sessions import delete_pending_tag

    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if not row:
        return -1  # tag not found

    tag_id = row["id"]

    # Count and delete associations
    removed = 0
    for table in ("conversation_tags", "workspace_tags", "tool_call_tags", "prompt_tags"):
        cur = conn.execute(f"DELETE FROM {table} WHERE tag_id = ?", (tag_id,))
        removed += cur.rowcount

    # Delete the tag itself
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    delete_pending_tag(conn, name)
    _invalidate_tag_cache(name)

    if commit:
        conn.commit()
    return removed


def list_tags(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    before: str | None = None,
    owner: str | None = None,
) -> list[dict]:
    """List all tags with usage counts.

    Args:
        conn: Database connection.
        since: Only count associations where the conversation started after this ISO date.
        before: Only count associations where the conversation started before this ISO date.
        owner: Only count associations owned by this user_id.
    """
    if owner and not has_conversation_owners_table(conn):
        return []

    has_time_filter = bool(since or before)

    conv_joins: list[str] = []
    conv_where = ["ct.tag_id = t.id"]
    conv_params: list[object] = []
    if has_time_filter:
        conv_joins.append("JOIN conversations c ON c.id = ct.conversation_id")
    if owner:
        conv_where.append(owner_predicate("c.id" if has_time_filter else "ct.conversation_id"))
        conv_params.append(owner)
    if since:
        conv_where.append("c.started_at >= ?")
        conv_params.append(since)
    if before:
        conv_where.append("c.started_at < ?")
        conv_params.append(before)
    conversation_count_sql = (
        "SELECT COUNT(*) FROM conversation_tags ct "
        f"{' '.join(conv_joins)} "
        f"WHERE {' AND '.join(conv_where)}"
    )

    ws_where = ["wt.tag_id = t.id"]
    ws_params: list[object] = []
    if owner:
        ws_where.append(owner_predicate("c.id"))
        ws_params.append(owner)
        workspace_count_sql = (
            "SELECT COUNT(DISTINCT wt.workspace_id) FROM workspace_tags wt "
            "JOIN conversations c ON c.workspace_id = wt.workspace_id "
            f"WHERE {' AND '.join(ws_where)}"
        )
    else:
        workspace_count_sql = (
            "SELECT COUNT(*) FROM workspace_tags wt "
            f"WHERE {' AND '.join(ws_where)}"
        )

    tc_joins: list[str] = []
    tc_where = ["tt.tag_id = t.id"]
    tc_params: list[object] = []
    if has_time_filter:
        tc_joins.append("JOIN conversations c ON c.id = tc.conversation_id")
    if owner:
        tc_where.append(owner_predicate("tc.conversation_id"))
        tc_params.append(owner)
    if since:
        tc_where.append("c.started_at >= ?")
        tc_params.append(since)
    if before:
        tc_where.append("c.started_at < ?")
        tc_params.append(before)
    tool_call_count_sql = (
        "SELECT COUNT(*) FROM tool_call_tags tt "
        "JOIN tool_calls tc ON tc.id = tt.tool_call_id "
        f"{' '.join(tc_joins)} "
        f"WHERE {' AND '.join(tc_where)}"
    )

    prompt_where = ["pt.tag_id = t.id"]
    prompt_params: list[object] = []
    if owner:
        prompt_where.append(owner_predicate("p.conversation_id"))
        prompt_params.append(owner)
        prompt_count_sql = (
            "SELECT COUNT(*) FROM prompt_tags pt "
            "JOIN prompts p ON p.id = pt.prompt_id "
            f"WHERE {' AND '.join(prompt_where)}"
        )
    else:
        prompt_count_sql = (
            "SELECT COUNT(*) FROM prompt_tags pt "
            f"WHERE {' AND '.join(prompt_where)}"
        )

    sql = f"""
        SELECT
            t.name,
            t.description,
            t.created_at,
            ({conversation_count_sql}) as conversation_count,
            ({workspace_count_sql}) as workspace_count,
            ({tool_call_count_sql}) as tool_call_count,
            CASE WHEN EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_tags')
                THEN ({prompt_count_sql})
                ELSE 0
            END as prompt_count
        FROM tags t
        ORDER BY t.name
    """
    all_params = [*conv_params, *ws_params, *tc_params, *prompt_params]

    cur = conn.execute(sql, all_params)
    rows = [
        {
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
            "conversation_count": row["conversation_count"],
            "workspace_count": row["workspace_count"],
            "tool_call_count": row["tool_call_count"],
            "prompt_count": row["prompt_count"],
        }
        for row in cur.fetchall()
    ]
    if owner:
        rows = [r for r in rows if (r["conversation_count"] or r["workspace_count"] or r["tool_call_count"] or r["prompt_count"])]
    return rows


def tag_shell_command(
    conn: sqlite3.Connection,
    tool_call_id: str,
    tool_name: str,
    input_data: dict | None,
) -> str | None:
    """Tag a shell.execute tool call with its category at ingest time.

    Args:
        conn: Database connection
        tool_call_id: The tool_call's ULID
        tool_name: Canonical tool name (e.g., "shell.execute")
        input_data: The tool call input dict

    Returns:
        The category name if tagged, None otherwise.
    """
    from siftd.domain.shell_categories import (
        SHELL_TAG_PREFIX,
        categorize_shell_command,
    )

    if tool_name != "shell.execute":
        return None

    if not input_data:
        return None

    # Extract command
    cmd = input_data.get("command") or input_data.get("cmd") or ""
    if not cmd:
        return None

    # Categorize
    category = categorize_shell_command(cmd)
    if not category:
        return None

    # Get or create tag and apply
    tag_name = f"{SHELL_TAG_PREFIX}{category}"
    tag_id = get_or_create_tag(conn, tag_name)
    apply_tag(conn, "tool_call", tool_call_id, tag_id)

    return category


DERIVATIVE_TAG = "siftd:derivative"


def is_derivative_tool_call(tool_name: str, input_data: dict | None) -> bool:
    """Check if a tool call indicates a derivative conversation.

    Derivative conversations invoke `siftd ask`, `siftd query`, or `siftd search` —
    their content pollutes future searches with repeated search results.

    Detects two patterns:
    - shell.execute with command containing 'siftd ask', 'siftd query', or 'siftd search'
    - skill.invoke with skill='siftd' (the siftd CLI skill)
    """
    if not input_data:
        return False

    if tool_name == "shell.execute":
        cmd = input_data.get("command") or input_data.get("cmd") or ""
        return "siftd ask" in cmd or "siftd query" in cmd or "siftd search" in cmd

    if tool_name == "skill.invoke":
        skill = input_data.get("skill") or ""
        return skill == "siftd"

    return False


def tag_derivative_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    tool_name: str,
    input_data: dict | None,
) -> bool:
    """Tag a conversation as derivative if a tool call matches.

    Called at ingest time for each tool call. Applies the conversation-level
    'siftd:derivative' tag on the first matching tool call.

    Returns True if the tag was newly applied.
    """
    if not is_derivative_tool_call(tool_name, input_data):
        return False

    tag_id = get_or_create_tag(conn, DERIVATIVE_TAG)
    result = apply_tag(conn, "conversation", conversation_id, tag_id)
    return result is not None
