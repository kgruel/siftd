"""Writer and reader functions for the polymorphic events schema (schema v4).

Replaces insert_prompt / insert_response / insert_tool_call / insert_prompt_content /
insert_response_content from sqlite.py.  Old tables (prompts, responses, tool_calls,
prompt_content, response_content) remain until slice 8 cleanup.
"""

import json
import sqlite3

# ---------------------------------------------------------------------------
# Trigger management
# ---------------------------------------------------------------------------

def ensure_event_tool_call_triggers(conn: sqlite3.Connection) -> None:
    """Drop old tr_tool_calls_* triggers and recreate as tr_event_tool_call_*.

    Idempotent — safe to call on every open_database write path.
    """
    conn.execute("DROP TRIGGER IF EXISTS tr_tool_calls_delete_release_blob")
    conn.execute("DROP TRIGGER IF EXISTS tr_tool_calls_update_release_blob")

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tr_event_tool_call_delete_release_blob
        AFTER DELETE ON event_tool_call
        FOR EACH ROW
        WHEN OLD.result_hash IS NOT NULL
        BEGIN
            UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0) WHERE hash = OLD.result_hash;
            DELETE FROM content_blobs WHERE hash = OLD.result_hash AND ref_count <= 0;
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tr_event_tool_call_update_release_blob
        AFTER UPDATE OF result_hash ON event_tool_call
        FOR EACH ROW
        WHEN OLD.result_hash IS NOT NEW.result_hash
        BEGIN
            UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0)
                WHERE OLD.result_hash IS NOT NULL AND hash = OLD.result_hash;
            DELETE FROM content_blobs
                WHERE OLD.result_hash IS NOT NULL AND hash = OLD.result_hash AND ref_count <= 0;
            UPDATE content_blobs SET ref_count = ref_count + 1
                WHERE NEW.result_hash IS NOT NULL AND hash = NEW.result_hash;
        END
    """)


def ensure_polymorphic_cleanup_triggers(conn: sqlite3.Connection) -> None:
    """Create IF NOT EXISTS cleanup triggers for polymorphic tables.

    tag_assignments and attributes have no FK on target_id, so these triggers
    cascade-clean orphan rows when events/workspaces/conversations are deleted.
    Idempotent — safe to call on every open_database write path.
    """
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tr_polymorphic_events_cleanup
        AFTER DELETE ON events
        BEGIN
            DELETE FROM tag_assignments
            WHERE target_id = OLD.id
              AND target_kind IN ('prompt', 'response', 'tool_call', 'exchange');
            DELETE FROM attributes
            WHERE target_id = OLD.id
              AND target_kind IN ('prompt', 'response', 'tool_call', 'exchange');
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tr_polymorphic_workspaces_cleanup
        AFTER DELETE ON workspaces
        BEGIN
            DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'workspace';
            DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'workspace';
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tr_polymorphic_conversations_cleanup
        AFTER DELETE ON conversations
        BEGIN
            DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'conversation';
            DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'conversation';
        END
    """)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def insert_event(
    conn: sqlite3.Connection,
    event_id: str,
    kind: str,
    conversation_id: str,
    timestamp: str,
    *,
    parent_id: str | None = None,
    external_id: str | None = None,
) -> str:
    """Insert a row into events, return event_id."""
    conn.execute(
        """INSERT INTO events (id, kind, conversation_id, parent_id, external_id, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, kind, conversation_id, parent_id, external_id, timestamp),
    )
    return event_id


def insert_event_response(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Insert a row into event_response (sparse extension for kind='response')."""
    conn.execute(
        """INSERT INTO event_response (event_id, model_id, provider_id, input_tokens, output_tokens)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, model_id, provider_id, input_tokens, output_tokens),
    )


def insert_event_content(
    conn: sqlite3.Connection,
    *,
    content_id: str,
    event_id: str,
    block_index: int,
    block_type: str,
    content: str,
) -> str:
    """Insert a content block into event_content, return content_id."""
    conn.execute(
        """INSERT INTO event_content (id, event_id, block_index, block_type, content)
           VALUES (?, ?, ?, ?, ?)""",
        (content_id, event_id, block_index, block_type, content),
    )
    return content_id


def insert_event_tool_call(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    tool_id: str | None = None,
    input_json: str | None = None,
    result_json: str | None = None,
    status: str | None = None,
    dedupe_result: bool = True,
    filter_binary: bool = True,
) -> None:
    """Insert a row into event_tool_call (sparse extension for kind='tool_call').

    Args:
        dedupe_result: Accepted for compatibility; always stores in content_blobs.
            event_tool_call has no inline result column.
        filter_binary: If True, strip binary/base64 content from result before storage.
    """
    from siftd.content.filters import filter_tool_result_binary
    from siftd.storage.blobs import store_content

    result_hash = None

    if result_json is not None and filter_binary:
        try:
            result_data = json.loads(result_json)
            filtered_data = filter_tool_result_binary(result_data)
            if filtered_data is not result_data:
                result_json = json.dumps(filtered_data)
        except (ValueError, TypeError):
            pass

    if result_json is not None:
        result_hash = store_content(conn, result_json)

    conn.execute(
        """INSERT INTO event_tool_call (event_id, tool_id, input, result_hash, status)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, tool_id, input_json, result_hash, status),
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def get_prompts_for_conv(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[sqlite3.Row]:
    """Return all prompt events for a conversation, ordered by timestamp."""
    return conn.execute(
        """SELECT e.id, e.external_id, e.timestamp, e.parent_id
           FROM events e
           WHERE e.conversation_id = ? AND e.kind = 'prompt'
           ORDER BY e.timestamp""",
        (conversation_id,),
    ).fetchall()


def get_responses_for_prompt(
    conn: sqlite3.Connection,
    prompt_id: str,
) -> list[sqlite3.Row]:
    """Return all response events whose parent is prompt_id."""
    return conn.execute(
        """SELECT e.id, e.external_id, e.timestamp,
                  er.model_id, er.provider_id, er.input_tokens, er.output_tokens
           FROM events e
           JOIN event_response er ON er.event_id = e.id
           WHERE e.parent_id = ? AND e.kind = 'response'
           ORDER BY e.timestamp""",
        (prompt_id,),
    ).fetchall()


def get_event_content(
    conn: sqlite3.Connection,
    event_ids: list[str],
) -> list[sqlite3.Row]:
    """Return all content blocks for the given event IDs, ordered by event + block_index."""
    if not event_ids:
        return []
    placeholders = ",".join("?" * len(event_ids))
    return conn.execute(
        f"""SELECT ec.id, ec.event_id, ec.block_index, ec.block_type, ec.content
            FROM event_content ec
            WHERE ec.event_id IN ({placeholders})
            ORDER BY ec.event_id, ec.block_index""",
        event_ids,
    ).fetchall()


def get_tool_calls_for_response(
    conn: sqlite3.Connection,
    response_id: str,
    *,
    include_content: bool = False,
) -> list[sqlite3.Row]:
    """Return all tool_call events whose parent is response_id."""
    return conn.execute(
        """SELECT e.id, e.external_id, e.timestamp,
                  etc.tool_id, etc.input, etc.result_hash, etc.status
           FROM events e
           JOIN event_tool_call etc ON etc.event_id = e.id
           WHERE e.parent_id = ? AND e.kind = 'tool_call'
           ORDER BY e.timestamp""",
        (response_id,),
    ).fetchall()


def get_event_tree(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> dict:
    """Return a nested dict representing the event tree for a conversation.

    Shape:
        {
          "prompts": [
            {
              "id": ..., "external_id": ..., "timestamp": ...,
              "responses": [
                {
                  "id": ..., ..., "model_id": ..., "input_tokens": ...,
                  "tool_calls": [
                    {"id": ..., "tool_id": ..., "input": ..., "result_hash": ..., "status": ...}
                  ]
                }
              ]
            }
          ]
        }
    """
    prompts = get_prompts_for_conv(conn, conversation_id)
    result: dict = {"prompts": []}

    for prompt in prompts:
        prompt_node: dict = {
            "id": prompt["id"],
            "external_id": prompt["external_id"],
            "timestamp": prompt["timestamp"],
            "responses": [],
        }
        responses = get_responses_for_prompt(conn, prompt["id"])
        for response in responses:
            response_node: dict = {
                "id": response["id"],
                "external_id": response["external_id"],
                "timestamp": response["timestamp"],
                "model_id": response["model_id"],
                "provider_id": response["provider_id"],
                "input_tokens": response["input_tokens"],
                "output_tokens": response["output_tokens"],
                "tool_calls": [],
            }
            tool_calls = get_tool_calls_for_response(conn, response["id"])
            for tc in tool_calls:
                response_node["tool_calls"].append({
                    "id": tc["id"],
                    "external_id": tc["external_id"],
                    "timestamp": tc["timestamp"],
                    "tool_id": tc["tool_id"],
                    "input": tc["input"],
                    "result_hash": tc["result_hash"],
                    "status": tc["status"],
                })
            prompt_node["responses"].append(response_node)
        result["prompts"].append(prompt_node)

    return result
