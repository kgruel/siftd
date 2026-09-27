"""Private fixture-only Pi diagnostic snapshot; no writes or schema repair."""

import sqlite3
from pathlib import Path

from siftd.storage.sessions import _covered_keys
from siftd.storage.sqlite import SCHEMA_VERSION, open_database

# Closed identifiers, never interpolated caller input.
_COLUMNS = {
    "harnesses": "id name",
    "conversations": "id external_id harness_id",
    "ingested_files": "path file_hash harness_id conversation_id error",
    "events": "id kind conversation_id parent_id external_id",
    "event_content": "id event_id block_index block_type content",
    "event_tool_call": "event_id input result_hash status",
    "content_blobs": "hash content",
    "tag_assignments": "target_kind target_id",
    "conversation_owners": "conversation_id",
    "pending_tags": "harness_session_id entity_type last_marker",
    "active_sessions": "harness_session_id adapter_name",
}


def read_snapshot(db_path: Path, paths: list[str], orphans: list[str]):
    """Return a scoped detached snapshot or a fixed refusal, on one transaction."""
    conn = None
    try:
        conn = open_database(db_path, read_only=True, auto_upgrade=False)
        conn.execute("BEGIN")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return None, "unsupported_version"
        tables = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT name, type, rootpage FROM sqlite_schema")}
        for table, columns in _COLUMNS.items():
            kind, root = tables.get(table, (None, 0))
            if kind != "table" or root <= 0:
                return None, "unsupported_schema"
            actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not set(columns.split()).issubset(actual):
                return None, "unsupported_schema"
        harnesses = dict(conn.execute("SELECT id, name FROM harnesses"))
        conversations = {r["id"]: dict(r) for r in conn.execute("SELECT id, external_id, harness_id FROM conversations")}
        files = [dict(r) for r in conn.execute(
            "SELECT path, file_hash, harness_id, conversation_id, error IS NOT NULL AS has_error FROM ingested_files"
        )]
        selected = {r["path"]: r for r in files if r["path"] in paths}
        scoped = set()
        for row in selected.values():
            cid = row["conversation_id"]
            if harnesses.get(row["harness_id"]) != "pi_agent":
                row["state"] = "non_pi_source"
            elif cid is None:
                row["state"] = "unlinked"
            elif cid not in conversations or harnesses.get(conversations[cid]["harness_id"]) != "pi_agent":
                row["state"] = "invalid_link"
            else:
                row["state"] = "linked"
                scoped.add(cid)
        for cid in orphans:
            if (cid not in conversations or harnesses.get(conversations[cid]["harness_id"]) != "pi_agent"
                    or any(r["conversation_id"] == cid for r in files)):
                return None, "invalid_orphan_selector"
            scoped.add(cid)
        graph = {}
        for cid in sorted(scoped):
            events = [dict(r) for r in conn.execute(
                "SELECT id, kind, parent_id, external_id FROM events WHERE conversation_id = ?", (cid,)
            )]
            blocks = [dict(r) for r in conn.execute(
                "SELECT b.id, b.event_id, b.block_index, b.block_type, b.content FROM event_content b "
                "JOIN events e ON e.id = b.event_id WHERE e.conversation_id = ?", (cid,)
            )]
            tools = [dict(r) for r in conn.execute(
                "SELECT t.event_id, t.input, t.result_hash, t.status, b.content AS result "
                "FROM event_tool_call t JOIN events e ON e.id = t.event_id "
                "LEFT JOIN content_blobs b ON b.hash = t.result_hash WHERE e.conversation_id = ?", (cid,)
            )]
            targets = {cid} | {r["id"] for r in events} | {r["id"] for r in blocks}
            assignments = [dict(r) for r in conn.execute("SELECT target_kind, target_id FROM tag_assignments")
                           if r["target_id"] in targets]
            graph[cid] = {
                "external_id": conversations[cid]["external_id"], "events": events, "blocks": blocks,
                "tools": tools, "assignments": assignments,
                "owners": conn.execute("SELECT COUNT(*) FROM conversation_owners WHERE conversation_id = ?", (cid,)).fetchone()[0],
                "references": [r for r in files if r["conversation_id"] == cid],
            }
        active = {r[0] for r in conn.execute(
            "SELECT harness_session_id FROM active_sessions WHERE adapter_name = 'pi_agent'"
        )}
        matches = {}
        for cid, row in conversations.items():
            key = row["external_id"]
            if key is not None and "::agent::" not in key:
                for form in _covered_keys(key):
                    matches.setdefault(form, set()).add(cid)
        pending = []
        for row in conn.execute("SELECT harness_session_id, entity_type, last_marker FROM pending_tags"):
            key = row["harness_session_id"]
            candidates = matches.get(key, set())
            if candidates & scoped:
                state = "unique_selected" if len(candidates) == 1 else "ambiguous_touching_scope"
            elif not candidates and (key.startswith("pi_agent::") or key in active):
                state = "unmatched_pi_attributed"
            else:
                state = "unattributed_or_out_of_scope"
            pending.append((state, row["entity_type"], row["last_marker"]))
        return {"selected": selected, "graph": graph, "pending": pending}, None
    except (sqlite3.Error, OSError):
        return None, "database_unavailable"
    finally:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()
