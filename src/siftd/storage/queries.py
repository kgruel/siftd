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

from siftd.storage.sql_helpers import (
    batched_in_query,
    has_conversation_owners_table,
    owner_predicate,
)


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
        kind_cond = "kind = 'prompt'"
        where_prefix = " AND ".join(conditions + [kind_cond]) + " AND " if conditions else f"{kind_cond} AND "
        prompt_rows = batched_in_query(
            conn,
            f"SELECT conversation_id, id, timestamp FROM events "
            f"WHERE {where_prefix}id IN ({{placeholders}}) ORDER BY timestamp",
            prompt_ids,
            prefix_params=tuple(params),
        )
        # Restore global timestamp ordering across batches
        prompt_rows = sorted(prompt_rows, key=lambda r: r["timestamp"])
    elif conditions:
        # Only non-batched filters
        where_clause = "WHERE " + " AND ".join(conditions) + " AND kind = 'prompt'"
        prompt_rows = conn.execute(
            f"SELECT conversation_id, id, timestamp FROM events "
            f"{where_clause} ORDER BY timestamp",
            params,
        ).fetchall()
    else:
        # No filters
        prompt_rows = conn.execute(
            "SELECT conversation_id, id, timestamp FROM events WHERE kind = 'prompt' ORDER BY timestamp"
        ).fetchall()

    if not prompt_rows:
        return []

    # Build lookup of prompt_id -> (conversation_id, timestamp)
    prompt_info = {row[1]: (row[0], row[2]) for row in prompt_rows}
    prompt_id_list = list(prompt_info.keys())

    # Fetch prompt content blocks in order (batched)
    prompt_content_rows = batched_in_query(
        conn,
        "SELECT event_id AS prompt_id, json_extract(content, '$.text') AS text "
        "FROM event_content "
        "WHERE event_id IN ({placeholders}) "
        "AND block_type = 'text' "
        "AND json_extract(content, '$.text') IS NOT NULL "
        "ORDER BY event_id, block_index",
        prompt_id_list,
    )

    # Aggregate prompt text by prompt_id
    prompt_texts: dict[str, list[str]] = {}
    for row in prompt_content_rows:
        prompt_texts.setdefault(row[0], []).append(row[1])

    # Fetch responses for these prompts (batched)
    response_rows = batched_in_query(
        conn,
        "SELECT id, parent_id AS prompt_id, timestamp FROM events "
        "WHERE kind = 'response' AND parent_id IN ({placeholders}) "
        "ORDER BY parent_id, timestamp",
        prompt_id_list,
    )

    if response_rows:
        response_ids = [row[0] for row in response_rows]

        # Fetch response content blocks in order (batched)
        response_content_rows = batched_in_query(
            conn,
            "SELECT event_id AS response_id, json_extract(content, '$.text') AS text "
            "FROM event_content "
            "WHERE event_id IN ({placeholders}) "
            "AND block_type = 'text' "
            "AND json_extract(content, '$.text') IS NOT NULL "
            "ORDER BY event_id, block_index",
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
    else:  # pragma: no cover — prompts with zero responses (rare in ingested data)
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
        if not ex.prompt_text and not ex.response_text:  # pragma: no cover
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

    Uses fetchone() — silently returns the first matching row when multiple
    conversations share a prefix. Callers passing a raw prefix (not yet
    resolved to a full ID) must pre-resolve via api.resolve_entity_id at the
    CLI boundary, which raises AmbiguousPrefix instead of silently first-matching.

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
        "SELECT m.name FROM events e "
        "JOIN event_response er ON er.event_id = e.id "
        "LEFT JOIN models m ON m.id = er.model_id "
        "WHERE e.conversation_id = ? AND e.kind = 'response' "
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
        "SELECT COALESCE(SUM(er.input_tokens), 0) AS input_tok, "
        "COALESCE(SUM(er.output_tokens), 0) AS output_tok "
        "FROM events e JOIN event_response er ON er.event_id = e.id "
        "WHERE e.conversation_id = ? AND e.kind = 'response'",
        (conversation_id,),
    ).fetchone()
    return row["input_tok"], row["output_tok"]


def fetch_prompts_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[sqlite3.Row]:
    """Fetch all prompts for a conversation, ordered by timestamp."""
    return conn.execute(
        "SELECT id, timestamp FROM events WHERE conversation_id = ? AND kind = 'prompt' ORDER BY timestamp",
        (conversation_id,),
    ).fetchall()


def fetch_prompt_text_content(
    conn: sqlite3.Connection,
    prompt_id: str,
) -> list[sqlite3.Row]:
    """Fetch text content blocks for a prompt."""
    return conn.execute(
        "SELECT content FROM event_content "
        "WHERE event_id = ? AND block_type = 'text' ORDER BY block_index",
        (prompt_id,),
    ).fetchall()


def fetch_prompt_text_contents(
    conn: sqlite3.Connection,
    prompt_ids: list[str],
) -> dict[str, list[sqlite3.Row]]:
    """Fetch text content blocks for multiple prompts, ordered by block_index.

    Returns dict mapping prompt_id to list of rows with content.
    """
    if not prompt_ids:
        return {}

    rows = batched_in_query(
        conn,
        "SELECT event_id AS prompt_id, content FROM event_content "
        "WHERE event_id IN ({placeholders}) "
        "AND block_type = 'text' "
        "ORDER BY event_id, block_index",
        prompt_ids,
    )

    result: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        result.setdefault(row["prompt_id"], []).append(row)
    return result


def fetch_responses_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[sqlite3.Row]:
    """Fetch all responses for a conversation, ordered by timestamp."""
    return conn.execute(
        "SELECT e.id, e.parent_id AS prompt_id, e.timestamp, er.input_tokens, er.output_tokens "
        "FROM events e JOIN event_response er ON er.event_id = e.id "
        "WHERE e.conversation_id = ? AND e.kind = 'response' ORDER BY e.timestamp",
        (conversation_id,),
    ).fetchall()


def fetch_response_text_content(
    conn: sqlite3.Connection,
    response_id: str,
) -> list[sqlite3.Row]:
    """Fetch text content blocks for a response."""
    return conn.execute(
        "SELECT content FROM event_content "
        "WHERE event_id = ? AND block_type = 'text' ORDER BY block_index",
        (response_id,),
    ).fetchall()


def fetch_tool_calls_for_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    include_content: bool = False,
) -> list[sqlite3.Row]:
    """Fetch tool calls for a conversation with tool names.

    Args:
        conn: Database connection.
        conversation_id: Conversation ULID.
        include_content: If True, join content_blobs for tool result.
    """
    if include_content:
        return conn.execute(
            "SELECT e.id AS tool_call_id, e.parent_id AS response_id, "
            "e.external_id, t.name AS tool_name, "
            "etc.status, etc.input, cb.content AS result "
            "FROM events e "
            "JOIN event_tool_call etc ON etc.event_id = e.id "
            "LEFT JOIN tools t ON t.id = etc.tool_id "
            "LEFT JOIN content_blobs cb ON cb.hash = etc.result_hash "
            "WHERE e.conversation_id = ? AND e.kind = 'tool_call' "
            "ORDER BY e.timestamp",
            (conversation_id,),
        ).fetchall()
    return conn.execute(
        "SELECT e.id AS tool_call_id, e.parent_id AS response_id, "
        "e.external_id, t.name AS tool_name, etc.status "
        "FROM events e "
        "JOIN event_tool_call etc ON etc.event_id = e.id "
        "LEFT JOIN tools t ON t.id = etc.tool_id "
        "WHERE e.conversation_id = ? AND e.kind = 'tool_call' "
        "ORDER BY e.timestamp",
        (conversation_id,),
    ).fetchall()


def fetch_response_content_blocks(
    conn: sqlite3.Connection,
    response_ids: list[str],
    block_types: tuple[str, ...] | None = None,
) -> dict[str, list[sqlite3.Row]]:
    """Fetch all content blocks for responses, ordered by block_index.

    Returns dict mapping response_id to list of rows with
    block_type, content, and block_index.
    """
    if not response_ids:
        return {}

    sql = (
        "SELECT event_id AS response_id, block_type, content, block_index "
        "FROM event_content "
        "WHERE event_id IN ({placeholders})"
    )
    if block_types:
        sql += " AND block_type IN (" + ",".join("?" * len(block_types)) + ")"
    sql += " ORDER BY event_id, block_index"
    rows = batched_in_query(
        conn, sql, response_ids, suffix_params=list(block_types or ()),
    )

    result: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        result.setdefault(row["response_id"], []).append(row)
    return result


def fetch_conversation_tags(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> list[str]:
    """Fetch tag names for a conversation."""
    rows = conn.execute(
        "SELECT t.name FROM tag_assignments ta "
        "JOIN tags t ON t.id = ta.tag_id "
        "WHERE ta.target_kind = 'conversation' AND ta.target_id = ? ORDER BY t.name",
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
        "SELECT ta.target_id AS conversation_id, t.name "
        "FROM tag_assignments ta "
        "JOIN tags t ON t.id = ta.tag_id "
        "WHERE ta.target_kind = 'conversation' AND ta.target_id IN ({placeholders}) "
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

_COUNTABLE_TABLES = frozenset({
    "conversations",
    "prompts",    # counts events WHERE kind='prompt'
    "responses",  # counts events WHERE kind='response'
    "tool_calls", # counts events WHERE kind='tool_call'
    "harnesses",
    "workspaces",
    "tools",
    "models",
    "ingested_files",
})


def fetch_table_count(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    owner: str | None = None,
) -> int:
    """Get a row count for a table, optionally scoped to an owner.

    When owner is provided, counts are computed in a tenant-safe way (e.g. distinct
    vocabulary "in use" vs total rows) to avoid leaking cross-tenant metadata.
    """
    if table_name not in _COUNTABLE_TABLES:
        raise ValueError(
            f"unknown table: {table_name!r}; allowed: {sorted(_COUNTABLE_TABLES)}"
        )
    if owner and not has_conversation_owners_table(conn):
        return 0
    if not owner:
        if table_name == "prompts":
            return conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'prompt'").fetchone()[0]
        if table_name == "responses":
            return conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'response'").fetchone()[0]
        if table_name == "tool_calls":
            return conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'tool_call'").fetchone()[0]
        if table_name == "models":
            # Canonical grain: one logical model, not one raw spelling. Two
            # spellings of the same model (claude-haiku-4.5 / claude-haiku-4-5)
            # are one model here, matching the ledger's GROUP BY models.name.
            return conn.execute("SELECT COUNT(DISTINCT name) FROM models").fetchone()[0]
        return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]

    if table_name == "conversations":
        return conn.execute(
            f"SELECT COUNT(*) FROM conversations c WHERE {owner_predicate('c.id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "prompts":
        return conn.execute(
            f"SELECT COUNT(*) FROM events e WHERE e.kind = 'prompt' AND {owner_predicate('e.conversation_id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "responses":
        return conn.execute(
            f"SELECT COUNT(*) FROM events e WHERE e.kind = 'response' AND {owner_predicate('e.conversation_id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "tool_calls":
        return conn.execute(
            f"SELECT COUNT(*) FROM events e WHERE e.kind = 'tool_call' AND {owner_predicate('e.conversation_id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "harnesses":
        return conn.execute(
            f"SELECT COUNT(DISTINCT c.harness_id) FROM conversations c "
            f"WHERE c.harness_id IS NOT NULL AND {owner_predicate('c.id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "workspaces":
        return conn.execute(
            f"SELECT COUNT(DISTINCT c.workspace_id) FROM conversations c "
            f"WHERE c.workspace_id IS NOT NULL AND {owner_predicate('c.id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "tools":
        return conn.execute(
            f"SELECT COUNT(DISTINCT etc.tool_id) FROM events e "
            f"JOIN event_tool_call etc ON etc.event_id = e.id "
            f"WHERE e.kind = 'tool_call' AND etc.tool_id IS NOT NULL "
            f"AND {owner_predicate('e.conversation_id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "models":
        # Canonical grain (COUNT DISTINCT models.name), not raw model_id, so the
        # owner-scoped count collapses spellings the same way the ledger does.
        return conn.execute(
            f"SELECT COUNT(DISTINCT m.name) FROM events e "
            f"JOIN event_response er ON er.event_id = e.id "
            f"JOIN models m ON m.id = er.model_id "
            f"WHERE e.kind = 'response' AND er.model_id IS NOT NULL "
            f"AND {owner_predicate('e.conversation_id')}",
            (owner,),
        ).fetchone()[0]
    if table_name == "ingested_files":
        return conn.execute(
            f"SELECT COUNT(*) FROM ingested_files f WHERE {owner_predicate('f.conversation_id')}",
            (owner,),
        ).fetchone()[0]

    raise ValueError(f"no owner-scoped branch for table: {table_name!r}")  # pragma: no cover


def fetch_harnesses(conn: sqlite3.Connection, *, owner: str | None = None) -> list[sqlite3.Row]:
    """Fetch all harness records, optionally scoped to an owner."""
    if owner and not has_conversation_owners_table(conn):
        return []
    if owner:
        return conn.execute(
            f"SELECT DISTINCT h.name, h.source, h.log_format "
            f"FROM harnesses h "
            f"JOIN conversations c ON c.harness_id = h.id "
            f"WHERE {owner_predicate('c.id')} "
            f"ORDER BY h.name",
            (owner,),
        ).fetchall()
    return conn.execute("SELECT name, source, log_format FROM harnesses").fetchall()


def fetch_top_workspaces(
    conn: sqlite3.Connection,
    limit: int = 10,
    *,
    owner: str | None = None,
    with_usage: bool = False,
) -> list[sqlite3.Row]:
    """Fetch workspaces with conversation counts and last activity.

    Uses subquery pattern: aggregate first on indexed column (workspace_id),
    then join only the top N rows with the workspaces table for paths.

    ``with_usage`` adds ``inp``/``out``/``cost`` columns from the rollup
    (``usage_by_conv_model``), aggregated at the *workspace_id* (ULID) grain so
    duplicate workspaces that share a git_remote stay distinct rows rather than
    collapsing the way the path-grouped dashboard mix does. Cost carries no
    COALESCE: a workspace with no priced usage sums to NULL → ``None``
    ("unknown"), never a fabricated $0. Default off keeps the query byte-identical
    for the name-only callers (the workspaces CLI listing, ``/meta``, the JSON
    master route) — it is the depth-gated enrichment the Workspaces view opts into.
    """
    if owner and not has_conversation_owners_table(conn):
        return []
    owner_where = ""
    owner_params: tuple[object, ...] = ()
    join_type = "JOIN"
    if owner:
        join_type = "LEFT JOIN"
        owner_where = f"WHERE {owner_predicate('c.id')}"
        owner_params = (owner,)
    if with_usage:
        # COUNT(DISTINCT c.id) — the rollup LEFT JOIN fans each conversation by
        # model, so a plain COUNT(*) would overcount sessions; MAX/SUM are
        # fan-stable so last_activity and the token/cost sums stay correct.
        convs_expr = "COUNT(DISTINCT c.id)"
        usage_inner = (
            ", COALESCE(SUM(u.input_tokens), 0) AS inp"
            ", COALESCE(SUM(u.output_tokens), 0) AS out"
            ", SUM(u.cost) AS cost"
        )
        usage_join = "LEFT JOIN usage_by_conv_model u ON u.conversation_id = c.id"
        usage_outer = ", counts.inp, counts.out, counts.cost"
    else:
        convs_expr = "COUNT(*)"
        usage_inner = usage_join = usage_outer = ""
    return conn.execute(
        f"""
        SELECT w.id, w.path, w.git_remote, counts.convs, counts.last_activity{usage_outer}
        FROM (
            SELECT
                c.workspace_id,
                {convs_expr} as convs,
                MAX(COALESCE(c.ended_at, c.started_at)) as last_activity{usage_inner}
            FROM conversations c
            {usage_join}
            {owner_where}
            GROUP BY c.workspace_id
            ORDER BY convs DESC
            LIMIT ?
        ) counts
        {join_type} workspaces w ON w.id = counts.workspace_id
        ORDER BY counts.convs DESC
        """,
        (*owner_params, limit),
    ).fetchall()


def fetch_conversation_time_window(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
) -> tuple[str | None, str | None]:
    """Fetch earliest and latest conversation start times, optionally scoped to an owner."""
    if owner and not has_conversation_owners_table(conn):
        return None, None
    sql = "SELECT MIN(started_at) AS earliest, MAX(started_at) AS latest FROM conversations c"
    params: tuple[object, ...] = ()
    if owner:
        sql += f" WHERE {owner_predicate('c.id')}"
        params = (owner,)
    row = conn.execute(sql, params).fetchone()
    # Aggregate queries always return a row; values are None on empty table
    return row["earliest"], row["latest"]


def fetch_harness_conversation_counts(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
) -> list[sqlite3.Row]:
    """Fetch conversation counts per harness, optionally scoped to an owner."""
    if owner and not has_conversation_owners_table(conn):
        return []
    if owner:
        return conn.execute(
            f"""
            SELECT h.name, COUNT(c.id) AS conversations
            FROM harnesses h
            JOIN conversations c ON c.harness_id = h.id
            WHERE {owner_predicate('c.id')}
            GROUP BY h.id
            ORDER BY conversations DESC, h.name
            """,
            (owner,),
        ).fetchall()
    return conn.execute(
        """
        SELECT h.name, COUNT(c.id) AS conversations
        FROM harnesses h
        LEFT JOIN conversations c ON c.harness_id = h.id
        GROUP BY h.id
        ORDER BY conversations DESC, h.name
        """
    ).fetchall()


def fetch_top_conversation_tags(
    conn: sqlite3.Connection,
    limit: int = 5,
    *,
    owner: str | None = None,
) -> list[sqlite3.Row]:
    """Fetch top conversation tags by usage."""
    if owner and not has_conversation_owners_table(conn):
        return []
    where_clauses = ["ta.target_kind = 'conversation'"]
    params: list[object] = []
    if owner:
        where_clauses.append(owner_predicate("ta.target_id"))
        params.append(owner)
    params.append(limit)
    where_sql = "WHERE " + " AND ".join(where_clauses)
    return conn.execute(
        f"""
        SELECT t.name, COUNT(ta.id) AS count
        FROM tags t
        JOIN tag_assignments ta ON ta.tag_id = t.id
        {where_sql}
        GROUP BY t.id
        ORDER BY count DESC, t.name
        LIMIT ?
        """,
        params,
    ).fetchall()


def fetch_last_ingest_time(conn: sqlite3.Connection, *, owner: str | None = None) -> str | None:
    """Fetch the most recent ingest timestamp, optionally scoped to an owner."""
    if owner and not has_conversation_owners_table(conn):
        return None
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingested_files'"
    ).fetchone()
    if row is None:
        return None
    sql = "SELECT MAX(ingested_at) AS last_ingest FROM ingested_files f"
    params: tuple[object, ...] = ()
    if owner:
        sql += f" WHERE {owner_predicate('f.conversation_id')}"
        params = (owner,)
    row = conn.execute(sql, params).fetchone()
    # Aggregate queries always return a row; value is None on empty table
    return row["last_ingest"]


def fetch_model_names(conn: sqlite3.Connection, *, owner: str | None = None) -> list[str]:
    """Fetch model names at the v11 canonical grain, optionally scoped to an owner.

    Returns ``models.name`` (canonical) deduped, not ``raw_name`` (adapter spelling),
    so one logical model collapses its spellings into a single entry — matching the
    dashboard ledger archetype (``get_usage_by_model``). The model filter matches
    ``raw_name LIKE ? OR name LIKE ?`` (filters.py), so a canonical value still
    selects every raw spelling it subsumes.
    """
    if owner and not has_conversation_owners_table(conn):
        return []
    if owner:
        rows = conn.execute(
            f"SELECT DISTINCT COALESCE(m.name, m.raw_name, 'unknown') AS name "
            f"FROM events e "
            f"JOIN event_response er ON er.event_id = e.id "
            f"LEFT JOIN models m ON m.id = er.model_id "
            f"WHERE e.kind = 'response' AND {owner_predicate('e.conversation_id')} "
            f"ORDER BY name",
            (owner,),
        ).fetchall()
        return [row["name"] for row in rows if row["name"]]
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(name, raw_name, 'unknown') AS name "
        "FROM models ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows if row["name"]]


def fetch_top_tools(
    conn: sqlite3.Connection,
    limit: int = 10,
    *,
    owner: str | None = None,
) -> list[sqlite3.Row]:
    """Fetch tools by usage count, ordered by count desc.

    Uses subquery pattern: aggregate first on indexed column (tool_id),
    then join only the top N rows with the tools table for names.
    This avoids joining 100k+ rows before aggregating.
    """
    if owner and not has_conversation_owners_table(conn):
        return []
    where_sql = "WHERE e.kind = 'tool_call' AND etc.tool_id IS NOT NULL"
    params: list[object] = [limit]
    if owner:
        where_sql += f" AND {owner_predicate('e.conversation_id')}"
        params = [owner, limit]
    return conn.execute(
        f"""
        SELECT t.name, counts.uses
        FROM (
            SELECT etc.tool_id, COUNT(*) as uses
            FROM events e JOIN event_tool_call etc ON etc.event_id = e.id
            {where_sql}
            GROUP BY etc.tool_id
            ORDER BY uses DESC
            LIMIT ?
        ) counts
        JOIN tools t ON t.id = counts.tool_id
        ORDER BY counts.uses DESC
        """,
        params,
    ).fetchall()


def fetch_response_token_coverage(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
) -> tuple[int, int]:
    """Fetch total responses and count with any token usage, optionally scoped to an owner.

    Reads the ``usage_by_conv_model`` rollup (S3): ``response_count`` and
    ``responses_with_tokens`` are materialized from the same
    ``events JOIN event_response`` flatten this used to scan, so the totals are
    identical — a GROUP-BY sum over the rollup instead of a full response-event
    scan.  Every path that bulk-writes raw rows (ingest, slice, merge) rebuilds
    the rollup, so a populated DB always has it; an empty rollup means an empty
    corpus (correctly ``(0, 0)``), while a DB missing the table entirely is
    malformed and surfaces a loud error rather than a silently-zero answer.
    """
    if owner and not has_conversation_owners_table(conn):
        return 0, 0
    sql = (
        "SELECT COALESCE(SUM(response_count), 0) AS total, "
        "COALESCE(SUM(responses_with_tokens), 0) AS with_tokens "
        "FROM usage_by_conv_model u"
    )
    params: tuple[object, ...] = ()
    if owner:
        sql += f" WHERE {owner_predicate('u.conversation_id')}"
        params = (owner,)
    row = conn.execute(sql, params).fetchone()
    return row["total"], row["with_tokens"]


def fetch_token_coverage_by_harness(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
) -> list[sqlite3.Row]:
    """Fetch response token coverage grouped by harness, optionally scoped to an owner.

    Reads the ``usage_by_conv_model`` rollup (S3), joined up to the harness via
    ``conversations.harness_id``.  The INNER JOINs to ``conversations`` and
    ``harnesses`` mirror the pre-rollup query, so responses on harness-less
    conversations are dropped identically.  Like
    :func:`fetch_response_token_coverage`, the rollup is a maintained invariant
    of any populated DB; a missing table is malformed and errors loudly.
    """
    if owner and not has_conversation_owners_table(conn):
        return []
    extra_where = ""
    params: tuple[object, ...] = ()
    if owner:
        extra_where = f"WHERE {owner_predicate('u.conversation_id')}"
        params = (owner,)
    return conn.execute(
        f"""
        SELECT h.name AS harness,
               COALESCE(SUM(u.response_count), 0) AS responses,
               COALESCE(SUM(u.responses_with_tokens), 0) AS with_tokens
        FROM usage_by_conv_model u
        JOIN conversations c ON c.id = u.conversation_id
        JOIN harnesses h ON h.id = c.harness_id
        {extra_where}
        GROUP BY h.name
        ORDER BY responses DESC
        """,
        params,
    ).fetchall()

def fetch_all_conversation_ids(conn: sqlite3.Connection) -> list[str]:
    """Fetch all conversation IDs."""
    rows = conn.execute("SELECT id FROM conversations").fetchall()
    return [row["id"] for row in rows]


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
        "SELECT id, timestamp FROM events WHERE kind = 'prompt' AND id IN ({placeholders})",
        prompt_ids,
    )
    return {row["id"]: row["timestamp"] or "" for row in rows}
