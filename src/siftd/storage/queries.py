"""Shared SQL query helpers for prompt/response text extraction.

These queries are the common pattern across formatters and chunker:
GROUP_CONCAT of text blocks from prompt_content / response_content,
filtered to block_type='text' with non-null json text.
"""

import sqlite3
from dataclasses import dataclass


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

    Returns:
        List of ExchangeRow ordered by prompt timestamp.
    """
    if prompt_ids is not None and len(prompt_ids) == 0:
        return []

    # Build filter conditions
    conditions = []
    params: list[str] = []

    if conversation_id is not None:
        conditions.append("p.conversation_id = ?")
        params.append(conversation_id)

    if prompt_ids is not None:
        placeholders = ",".join("?" * len(prompt_ids))
        conditions.append(f"p.id IN ({placeholders})")
        params.extend(prompt_ids)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    # Get prompts first (filtered)
    prompt_sql = f"""
        SELECT p.conversation_id, p.id, p.timestamp
        FROM prompts p
        {where_clause}
        ORDER BY p.timestamp
    """
    prompt_rows = conn.execute(prompt_sql, params).fetchall()

    if not prompt_rows:
        return []

    # Build lookup of prompt_id -> (conversation_id, timestamp)
    prompt_info = {row[1]: (row[0], row[2]) for row in prompt_rows}
    prompt_id_list = list(prompt_info.keys())
    placeholders = ",".join("?" * len(prompt_id_list))

    # Fetch prompt content blocks in order
    prompt_content_sql = f"""
        SELECT prompt_id, json_extract(content, '$.text') AS text
        FROM prompt_content
        WHERE prompt_id IN ({placeholders})
          AND block_type = 'text'
          AND json_extract(content, '$.text') IS NOT NULL
        ORDER BY prompt_id, block_index
    """
    prompt_content_rows = conn.execute(prompt_content_sql, prompt_id_list).fetchall()

    # Aggregate prompt text by prompt_id
    prompt_texts: dict[str, list[str]] = {}
    for row in prompt_content_rows:
        prompt_texts.setdefault(row[0], []).append(row[1])

    # Fetch responses for these prompts
    response_sql = f"""
        SELECT r.id, r.prompt_id, r.timestamp
        FROM responses r
        WHERE r.prompt_id IN ({placeholders})
        ORDER BY r.prompt_id, r.timestamp
    """
    response_rows = conn.execute(response_sql, prompt_id_list).fetchall()

    if response_rows:
        response_ids = [row[0] for row in response_rows]
        response_placeholders = ",".join("?" * len(response_ids))

        # Fetch response content blocks in order
        response_content_sql = f"""
            SELECT response_id, json_extract(content, '$.text') AS text
            FROM response_content
            WHERE response_id IN ({response_placeholders})
              AND block_type = 'text'
              AND json_extract(content, '$.text') IS NOT NULL
            ORDER BY response_id, block_index
        """
        response_content_rows = conn.execute(response_content_sql, response_ids).fetchall()

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
) -> dict[str, list[dict]]:
    """Load prompt/response pairs grouped by conversation, ordered by timestamp.

    Each exchange is: {"text": str, "prompt_id": str}
    where text is prompt_text + response_text concatenated.

    If conversation_id is given, only loads that conversation's exchanges.
    Otherwise loads all conversations (expensive for large DBs).
    """
    exchanges = fetch_exchanges(conn, conversation_id=conversation_id)

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
