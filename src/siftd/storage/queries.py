"""Centralized SQL read queries for siftd storage.

This module is the canonical read layer:
- Conversation listing and detail queries
- Stats and aggregation queries
- Prompt/response text extraction

All functions accept a connection and return rows/dicts.
The API layer handles parameter validation and dataclass mapping.
"""

import sqlite3
from dataclasses import dataclass

from siftd.storage.sql_helpers import batched_in_query


@dataclass
class ExchangeRow:
    """A single prompt-response exchange."""

    conversation_id: str
    prompt_id: str
    prompt_timestamp: str
    prompt_text: str
    response_text: str


def fetch_exchanges(
    conn: sqlite3.Connection,
    *,
    conversation_id: str | None = None,
    prompt_ids: list[str] | None = None,
    exclude_conversation_ids: set[str] | None = None,
) -> list[ExchangeRow]:
    """Fetch prompt-response exchanges with deterministic ordering.

    Returns rows with prompt and response text, where:
    - prompt text is ordered by prompt_content.block_index
    - response text is ordered by responses.timestamp, then response_content.block_index
    - multiple responses per prompt are concatenated in timestamp order

    Args:
        conn: Database connection.
        conversation_id: Filter to a single conversation.
        prompt_ids: Filter to specific prompt IDs.
        exclude_conversation_ids: Conversation IDs to exclude from results.

    Returns:
        List of ExchangeRow ordered by prompt timestamp.
    """
    if prompt_ids is not None and len(prompt_ids) == 0:
        return []

    # Build filter conditions and params dynamically
    # This allows combining conversation_id, prompt_ids, and exclude_conversation_ids
    conditions: list[str] = []
    params: list[str] = []

    if conversation_id is not None:
        conditions.append("conversation_id = ?")
        params.append(conversation_id)

    if exclude_conversation_ids:
        # Batch NOT IN clauses to avoid SQLite variable limits (max ~999)
        exclude_list = list(exclude_conversation_ids)
        batch_size = 500
        not_in_clauses = []
        for i in range(0, len(exclude_list), batch_size):
            batch = exclude_list[i : i + batch_size]
            ph = ",".join("?" * len(batch))
            not_in_clauses.append(f"conversation_id NOT IN ({ph})")
            params.extend(batch)
        # All batches must pass (AND), so combine them
        conditions.append("(" + " AND ".join(not_in_clauses) + ")")

    # Get prompts (with optional prompt_ids batching)
    if prompt_ids is not None:
        # Use batched_in_query for prompt_ids, with other conditions as prefix
        # Note: ORDER BY in query is per-batch; we sort globally after
        where_prefix = " AND ".join(conditions) + " AND " if conditions else ""
        prompt_rows = batched_in_query(
            conn,
            f"SELECT conversation_id, id, timestamp FROM prompts "
            f"WHERE {where_prefix}id IN ({{placeholders}}) ORDER BY timestamp",
            prompt_ids,
            prefix_params=tuple(params),
        )
        # Restore global timestamp ordering across batches
        prompt_rows = sorted(prompt_rows, key=lambda r: r["timestamp"])
    elif conditions:
        # Only non-batched filters
        where_clause = "WHERE " + " AND ".join(conditions)
        prompt_rows = conn.execute(
            f"SELECT conversation_id, id, timestamp FROM prompts "
            f"{where_clause} ORDER BY timestamp",
            params,
        ).fetchall()
    else:
        # No filters
        prompt_rows = conn.execute(
            "SELECT conversation_id, id, timestamp FROM prompts ORDER BY timestamp"
        ).fetchall()

    if not prompt_rows:
        return []

    # Build lookup of prompt_id -> (conversation_id, timestamp)
    prompt_info = {row[1]: (row[0], row[2]) for row in prompt_rows}
    prompt_id_list = list(prompt_info.keys())

    # Fetch prompt content blocks in order (batched)
    prompt_content_rows = batched_in_query(
        conn,
        "SELECT prompt_id, json_extract(content, '$.text') AS text "
        "FROM prompt_content "
        "WHERE prompt_id IN ({placeholders}) "
        "AND block_type = 'text' "
        "AND json_extract(content, '$.text') IS NOT NULL "
        "ORDER BY prompt_id, block_index",
        prompt_id_list,
    )

    # Aggregate prompt text by prompt_id
    prompt_texts: dict[str, list[str]] = {}
    for row in prompt_content_rows:
        prompt_texts.setdefault(row[0], []).append(row[1])

    # Fetch responses for these prompts (batched)
    response_rows = batched_in_query(
        conn,
        "SELECT id, prompt_id, timestamp FROM responses "
        "WHERE prompt_id IN ({placeholders}) "
        "ORDER BY prompt_id, timestamp",
        prompt_id_list,
    )

    if response_rows:
        response_ids = [row[0] for row in response_rows]

        # Fetch response content blocks in order (batched)
        response_content_rows = batched_in_query(
            conn,
            "SELECT response_id, json_extract(content, '$.text') AS text "
            "FROM response_content "
            "WHERE response_id IN ({placeholders}) "
            "AND block_type = 'text' "
            "AND json_extract(content, '$.text') IS NOT NULL "
            "ORDER BY response_id, block_index",
            response_ids,
        )

        # Aggregate response content by response_id
        response_content_texts: dict[str, list[str]] = {}
        for row in response_content_rows:
            response_content_texts.setdefault(row[0], []).append(row[1])

        # Build response_id -> prompt_id mapping and ordered response list per prompt
        responses_by_prompt: dict[str, list[tuple[str, str]]] = {}
        for row in response_rows:
            resp_id, prompt_id, timestamp = row
            responses_by_prompt.setdefault(prompt_id, []).append((resp_id, timestamp))

        # Build response text by prompt (multiple responses concatenated)
        response_texts: dict[str, str] = {}
        for prompt_id, resp_list in responses_by_prompt.items():
            # resp_list is already ordered by timestamp from the query
            parts = []
            for resp_id, _ in resp_list:
                blocks = response_content_texts.get(resp_id, [])
                if blocks:
                    parts.append("\n".join(blocks))
            if parts:
                response_texts[prompt_id] = "\n\n".join(parts)
    else:
        response_texts = {}

    # Build final result in prompt timestamp order
    result = []
    for prompt_id in prompt_id_list:
        conv_id, timestamp = prompt_info[prompt_id]
        prompt_text_parts = prompt_texts.get(prompt_id, [])
        prompt_text = "\n".join(prompt_text_parts) if prompt_text_parts else ""
        response_text = response_texts.get(prompt_id, "")

        result.append(
            ExchangeRow(
                conversation_id=conv_id,
                prompt_id=prompt_id,
                prompt_timestamp=timestamp,
                prompt_text=prompt_text.strip(),
                response_text=response_text.strip(),
            )
        )

    return result


def fetch_prompt_response_texts(
    conn: sqlite3.Connection,
    prompt_ids: list[str],
) -> list[tuple[str, str, str]]:
    """Fetch prompt and response text for a list of prompt IDs.

    Returns list of (prompt_id, prompt_text, response_text) tuples,
    ordered by prompt timestamp. Text values are stripped; missing
    text returns empty string.

    Note: Multiple responses per prompt are concatenated in timestamp order.
    """
    exchanges = fetch_exchanges(conn, prompt_ids=prompt_ids)
    return [
        (ex.prompt_id, ex.prompt_text, ex.response_text)
        for ex in exchanges
    ]


def fetch_conversation_exchanges(
    conn: sqlite3.Connection,
    *,
    conversation_id: str | None = None,
    exclude_conversation_ids: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Load prompt/response pairs grouped by conversation, ordered by timestamp.

    Each exchange is: {"text": str, "prompt_id": str}
    where text is prompt_text + response_text concatenated.

    Args:
        conn: Database connection.
        conversation_id: Filter to a single conversation.
        exclude_conversation_ids: Conversation IDs to exclude from results.

    If conversation_id is given, only loads that conversation's exchanges.
    Otherwise loads all conversations (expensive for large DBs).
    """
    exchanges = fetch_exchanges(
        conn,
        conversation_id=conversation_id,
        exclude_conversation_ids=exclude_conversation_ids,
    )

    result: dict[str, list[dict]] = {}
    for ex in exchanges:
        if not ex.prompt_text and not ex.response_text:
            continue

        if ex.conversation_id not in result:
            result[ex.conversation_id] = []

        # Combine prompt and response text
        exchange_text = ""
        if ex.prompt_text:
            exchange_text = ex.prompt_text
        if ex.response_text:
            if exchange_text:
                exchange_text += "\n\n"
            exchange_text += ex.response_text

        result[ex.conversation_id].append({
            "text": exchange_text,
            "prompt_id": ex.prompt_id,
        })

    return result


# =============================================================================
# Conversation queries
# =============================================================================


def has_pricing_table(conn: sqlite3.Connection) -> bool:
    """Check if pricing table exists in database."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pricing'"
    ).fetchone()
    return row[0] > 0


def fetch_conversation_by_id_or_prefix(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> dict | None:
    """Find conversation by exact ID or prefix match.

    Returns dict with id, started_at, workspace or None if not found.
    """
    row = conn.execute(
        "SELECT c.id, c.started_at, w.path AS workspace "
        "FROM conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id "
        "WHERE c.id = ? OR c.id LIKE ?",
        (conversation_id, f"{conversation_id}%"),
    ).fetchone()
    return dict(row) if row else None


def fetch_conversation_model(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> str | None:
    """Get most frequently used model name for a conversation."""
    row = conn.execute(
        "SELECT m.name FROM responses r "
        "LEFT JOIN models m ON m.id = r.model_id "
        "WHERE r.conversation_id = ? "
        "GROUP BY m.name ORDER BY COUNT(*) DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return row["name"] if row else None


def fetch_conversation_token_totals(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> tuple[int, int]:
    """Get total input and output tokens for a conversation.

    Returns (input_tokens, output_tokens).
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS input_tok, "
        "COALESCE(SUM(output_tokens), 0) AS output_tok "
        "FROM responses WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return row["input_tok"], row["output_tok"]


def fetch_prompts_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[sqlite3.Row]:
    """Fetch all prompts for a conversation, ordered by timestamp."""
    return conn.execute(
        "SELECT id, timestamp FROM prompts WHERE conversation_id = ? ORDER BY timestamp",
        (conversation_id,),
    ).fetchall()


def fetch_prompt_text_content(
    conn: sqlite3.Connection,
    prompt_id: str,
) -> list[sqlite3.Row]:
    """Fetch text content blocks for a prompt."""
    return conn.execute(
        "SELECT content FROM prompt_content "
        "WHERE prompt_id = ? AND block_type = 'text' ORDER BY block_index",
        (prompt_id,),
    ).fetchall()


def fetch_responses_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[sqlite3.Row]:
    """Fetch all responses for a conversation, ordered by timestamp."""
    return conn.execute(
        "SELECT id, prompt_id, timestamp, input_tokens, output_tokens "
        "FROM responses WHERE conversation_id = ? ORDER BY timestamp",
        (conversation_id,),
    ).fetchall()


def fetch_response_text_content(
    conn: sqlite3.Connection,
    response_id: str,
) -> list[sqlite3.Row]:
    """Fetch text content blocks for a response."""
    return conn.execute(
        "SELECT content FROM response_content "
        "WHERE response_id = ? AND block_type = 'text' ORDER BY block_index",
        (response_id,),
    ).fetchall()


def fetch_tool_calls_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[sqlite3.Row]:
    """Fetch tool calls for a conversation with tool names."""
    return conn.execute(
        "SELECT tc.response_id, t.name AS tool_name, tc.status "
        "FROM tool_calls tc "
        "LEFT JOIN tools t ON t.id = tc.tool_id "
        "WHERE tc.conversation_id = ? "
        "ORDER BY tc.timestamp",
        (conversation_id,),
    ).fetchall()


def fetch_conversation_tags(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[str]:
    """Fetch tag names for a conversation."""
    rows = conn.execute(
        "SELECT t.name FROM conversation_tags ct "
        "JOIN tags t ON t.id = ct.tag_id "
        "WHERE ct.conversation_id = ? ORDER BY t.name",
        (conversation_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def fetch_tags_for_conversations(
    conn: sqlite3.Connection,
    conversation_ids: list[str],
) -> dict[str, list[str]]:
    """Bulk fetch tags for multiple conversations.

    Returns dict mapping conversation_id to list of tag names.
    """
    if not conversation_ids:
        return {}

    rows = batched_in_query(
        conn,
        "SELECT ct.conversation_id, t.name "
        "FROM conversation_tags ct "
        "JOIN tags t ON t.id = ct.tag_id "
        "WHERE ct.conversation_id IN ({placeholders}) "
        "ORDER BY t.name",
        conversation_ids,
    )

    tags_by_conv: dict[str, list[str]] = {}
    for row in rows:
        tags_by_conv.setdefault(row["conversation_id"], []).append(row["name"])
    return tags_by_conv


# =============================================================================
# Stats queries
# =============================================================================


def fetch_table_count(conn: sqlite3.Connection, table_name: str) -> int:
    """Get row count for a table."""
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def fetch_harnesses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Fetch all harness records."""
    return conn.execute("SELECT name, source, log_format FROM harnesses").fetchall()


def fetch_top_workspaces(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Fetch workspaces with conversation counts, ordered by count desc."""
    return conn.execute(
        """
        SELECT w.path, COUNT(c.id) as convs
        FROM workspaces w
        LEFT JOIN conversations c ON c.workspace_id = w.id
        GROUP BY w.id
        ORDER BY convs DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def fetch_model_names(conn: sqlite3.Connection) -> list[str]:
    """Fetch all model raw_names."""
    rows = conn.execute("SELECT raw_name FROM models").fetchall()
    return [row["raw_name"] for row in rows]


def fetch_top_tools(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Fetch tools by usage count, ordered by count desc."""
    return conn.execute(
        """
        SELECT t.name, COUNT(tc.id) as uses
        FROM tools t
        JOIN tool_calls tc ON tc.tool_id = t.id
        GROUP BY t.id
        ORDER BY uses DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# =============================================================================
# Tool tag queries
# =============================================================================


def fetch_tool_tags_by_prefix(
    conn: sqlite3.Connection,
    prefix: str,
) -> list[sqlite3.Row]:
    """Fetch tool call tag usage counts filtered by prefix."""
    return conn.execute(
        """
        SELECT t.name, COUNT(tct.id) as count
        FROM tags t
        JOIN tool_call_tags tct ON tct.tag_id = t.id
        WHERE t.name LIKE ?
        GROUP BY t.id
        ORDER BY count DESC
        """,
        (f"{prefix}%",),
    ).fetchall()


def fetch_tool_tags_by_workspace(
    conn: sqlite3.Connection,
    prefix: str,
) -> list[sqlite3.Row]:
    """Fetch per-workspace tool tag usage counts."""
    return conn.execute(
        """
        SELECT
            COALESCE(w.path, '(no workspace)') as workspace,
            t.name as tag,
            COUNT(tct.id) as count
        FROM tool_call_tags tct
        JOIN tags t ON t.id = tct.tag_id
        JOIN tool_calls tc ON tc.id = tct.tool_call_id
        JOIN conversations c ON c.id = tc.conversation_id
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE t.name LIKE ?
        GROUP BY w.id, t.id
        ORDER BY workspace, count DESC
        """,
        (f"{prefix}%",),
    ).fetchall()


# =============================================================================
# Search queries
# =============================================================================


def fetch_conversation_timestamps(
    conn: sqlite3.Connection,
    conversation_ids: list[str],
) -> dict[str, str]:
    """Fetch started_at timestamps for conversations.

    Returns dict mapping conversation_id to started_at (or empty string).
    """
    if not conversation_ids:
        return {}

    rows = batched_in_query(
        conn,
        "SELECT id, started_at FROM conversations WHERE id IN ({placeholders})",
        conversation_ids,
    )
    return {row["id"]: row["started_at"] or "" for row in rows}


def fetch_prompt_timestamps(
    conn: sqlite3.Connection,
    prompt_ids: list[str],
) -> dict[str, str]:
    """Fetch timestamps for prompts.

    Returns dict mapping prompt_id to timestamp (or empty string).
    """
    if not prompt_ids:
        return {}

    rows = batched_in_query(
        conn,
        "SELECT id, timestamp FROM prompts WHERE id IN ({placeholders})",
        prompt_ids,
    )
    return {row["id"]: row["timestamp"] or "" for row in rows}
