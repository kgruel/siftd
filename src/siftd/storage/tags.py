"""Tag CRUD operations for siftd storage."""

import sqlite3
from datetime import UTC, datetime

from siftd.ids import ulid as _ulid
from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

_VALID_TARGET_KINDS: frozenset[str] = frozenset(
    {"conversation", "workspace", "prompt", "response", "tool_call", "exchange"}
)

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
        (ulid, name, description, datetime.now(UTC).isoformat())
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
    target_kind: str,
    target_id: str,
    tag_id: str,
    *,
    applied_at: str | None = None,
    commit: bool = False,
) -> str | None:
    """Apply a tag to an entity via tag_assignments. Returns assignment id or None if already applied.

    target_kind: 'conversation' | 'workspace' | 'prompt' | 'response' | 'tool_call' | 'exchange'
    target_id: ULID of the target entity
    tag_id: ULID of the tag (pre-resolved; use get_or_create_tag if needed)
    """
    if target_kind not in _VALID_TARGET_KINDS:
        raise ValueError(f"Unknown target_kind {target_kind!r}. Valid: {sorted(_VALID_TARGET_KINDS)}")
    ulid = _ulid()
    ts = applied_at or datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ulid, target_kind, target_id, tag_id, ts),
    )
    if cur.rowcount == 0:
        return None  # Already applied
    if commit:
        conn.commit()
    return ulid


def remove_tag(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Remove a tag from an entity. Returns True if a row was deleted, False if not applied.

    target_kind: 'conversation' | 'workspace' | 'prompt' | 'response' | 'tool_call' | 'exchange'
    """
    if target_kind not in _VALID_TARGET_KINDS:
        raise ValueError(f"Unknown target_kind {target_kind!r}. Valid: {sorted(_VALID_TARGET_KINDS)}")
    cur = conn.execute(
        "DELETE FROM tag_assignments WHERE target_kind = ? AND target_id = ? AND tag_id = ?",
        (target_kind, target_id, tag_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def get_tags_for(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
) -> list[sqlite3.Row]:
    """Return tag rows for a given target."""
    return conn.execute(
        "SELECT t.name, t.description, ta.applied_at "
        "FROM tag_assignments ta JOIN tags t ON t.id = ta.tag_id "
        "WHERE ta.target_kind = ? AND ta.target_id = ?",
        (target_kind, target_id),
    ).fetchall()


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

    # Delete all assignments from the polymorphic table
    cur = conn.execute("DELETE FROM tag_assignments WHERE tag_id = ?", (tag_id,))
    removed = cur.rowcount

    # Delete the tag itself
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    delete_pending_tag(conn, name)
    _invalidate_tag_cache(name)

    if commit:
        conn.commit()
    return removed


def tag_used_by_other_owners(
    conn: sqlite3.Connection,
    tag_id: str,
    owner: str | None,
) -> bool:
    """Return True when a tag is associated with entities owned by other users."""
    if not owner:
        return False
    if not has_conversation_owners_table(conn):
        return False

    # conversation-kind: direct ownership join
    row = conn.execute(
        "SELECT 1 FROM tag_assignments ta "
        "JOIN conversation_owners co ON co.conversation_id = ta.target_id "
        "WHERE ta.tag_id = ? AND ta.target_kind = 'conversation' AND co.user_id != ? LIMIT 1",
        (tag_id, owner),
    ).fetchone()
    if row:
        return True

    # tool_call/prompt/response/exchange-kind: ownership via event → conversation
    row = conn.execute(
        "SELECT 1 FROM tag_assignments ta "
        "JOIN events e ON e.id = ta.target_id "
        "JOIN conversation_owners co ON co.conversation_id = e.conversation_id "
        "WHERE ta.tag_id = ? AND ta.target_kind IN ('tool_call','prompt','response','exchange') "
        "AND co.user_id != ? LIMIT 1",
        (tag_id, owner),
    ).fetchone()
    if row:
        return True

    # workspace-kind: conservative — if any conversation in the workspace belongs to another user
    row = conn.execute(
        "SELECT 1 FROM tag_assignments ta "
        "JOIN conversations c ON c.workspace_id = ta.target_id "
        "JOIN conversation_owners co ON co.conversation_id = c.id "
        "WHERE ta.tag_id = ? AND ta.target_kind = 'workspace' AND co.user_id != ? LIMIT 1",
        (tag_id, owner),
    ).fetchone()
    if row:
        return True

    return False


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

    # conversation count
    conv_joins: list[str] = []
    conv_where = ["ta.tag_id = t.id", "ta.target_kind = 'conversation'"]
    conv_params: list[object] = []
    if has_time_filter:
        conv_joins.append("JOIN conversations c ON c.id = ta.target_id")
    if owner:
        conv_where.append(owner_predicate("c.id" if has_time_filter else "ta.target_id"))
        conv_params.append(owner)
    if since:
        conv_where.append("c.started_at >= ?")
        conv_params.append(since)
    if before:
        conv_where.append("c.started_at < ?")
        conv_params.append(before)
    conversation_count_sql = (
        "SELECT COUNT(*) FROM tag_assignments ta "
        f"{' '.join(conv_joins)} "
        f"WHERE {' AND '.join(conv_where)}"
    )

    # workspace count
    ws_where = ["ta.tag_id = t.id", "ta.target_kind = 'workspace'"]
    ws_params: list[object] = []
    if owner:
        ws_where.append(owner_predicate("c.id"))
        ws_params.append(owner)
        workspace_count_sql = (
            "SELECT COUNT(DISTINCT ta.target_id) FROM tag_assignments ta "
            "JOIN conversations c ON c.workspace_id = ta.target_id "
            f"WHERE {' AND '.join(ws_where)}"
        )
    else:
        workspace_count_sql = (
            "SELECT COUNT(*) FROM tag_assignments ta "
            f"WHERE {' AND '.join(ws_where)}"
        )

    # tool_call count — join to events/conversations when owner or time filter needed
    tc_where = ["ta.tag_id = t.id", "ta.target_kind = 'tool_call'"]
    tc_params: list[object] = []
    tc_joins: list[str] = []
    if has_time_filter or owner:
        tc_joins.append("JOIN events e ON e.id = ta.target_id")
        tc_joins.append("JOIN conversations c ON c.id = e.conversation_id")
    if owner:
        tc_where.append(owner_predicate("e.conversation_id"))
        tc_params.append(owner)
    if since:
        tc_where.append("c.started_at >= ?")
        tc_params.append(since)
    if before:
        tc_where.append("c.started_at < ?")
        tc_params.append(before)
    tool_call_count_sql = (
        "SELECT COUNT(*) FROM tag_assignments ta "
        f"{' '.join(tc_joins)} "
        f"WHERE {' AND '.join(tc_where)}"
    )

    # exchange count: assignments where target_kind='exchange' (prompt anchor for an exchange)
    exchange_where = ["ta.tag_id = t.id", "ta.target_kind = 'exchange'"]
    exchange_params: list[object] = []
    if owner:
        exchange_where.append(owner_predicate("p.conversation_id"))
        exchange_params.append(owner)
        exchange_count_sql = (
            "SELECT COUNT(*) FROM tag_assignments ta "
            "JOIN events p ON p.id = ta.target_id "
            f"WHERE {' AND '.join(exchange_where)}"
        )
    else:
        exchange_count_sql = (
            "SELECT COUNT(*) FROM tag_assignments ta "
            f"WHERE {' AND '.join(exchange_where)}"
        )

    sql = f"""
        SELECT
            t.name,
            t.description,
            t.created_at,
            ({conversation_count_sql}) as conversation_count,
            ({workspace_count_sql}) as workspace_count,
            ({tool_call_count_sql}) as tool_call_count,
            ({exchange_count_sql}) as exchange_count
        FROM tags t
        ORDER BY t.name
    """
    all_params = [*conv_params, *ws_params, *tc_params, *exchange_params]

    cur = conn.execute(sql, all_params)
    rows = [
        {
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
            "conversation_count": row["conversation_count"],
            "workspace_count": row["workspace_count"],
            "tool_call_count": row["tool_call_count"],
            "exchange_count": row["exchange_count"],
        }
        for row in cur.fetchall()
    ]
    if owner:
        rows = [r for r in rows if (r["conversation_count"] or r["workspace_count"] or r["tool_call_count"] or r["exchange_count"])]
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
        tool_call_id: The tool_call's event ULID
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
