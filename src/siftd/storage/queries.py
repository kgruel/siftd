"""Shared SQL query helpers for prompt/response text extraction.

These queries are the common pattern across formatters and chunker:
GROUP_CONCAT of text blocks from prompt_content / response_content,
filtered to block_type='text' with non-null json text.
"""

import sqlite3


def fetch_prompt_response_texts(
    conn: sqlite3.Connection,
    prompt_ids: list[str],
) -> list[tuple[str, str, str]]:
    """Fetch prompt and response text for a list of prompt IDs.

    Returns list of (prompt_id, prompt_text, response_text) tuples,
    ordered by prompt timestamp. Text values are stripped; missing
    text returns empty string.
    """
    if not prompt_ids:
        return []

    placeholders = ",".join("?" * len(prompt_ids))

    prompt_rows = conn.execute(
        f"""
        SELECT p.id, GROUP_CONCAT(json_extract(pc.content, '$.text'), '\n') AS text
        FROM prompts p
        JOIN prompt_content pc ON pc.prompt_id = p.id
        WHERE p.id IN ({placeholders})
          AND pc.block_type = 'text'
          AND json_extract(pc.content, '$.text') IS NOT NULL
        GROUP BY p.id
        ORDER BY p.timestamp
    """,
        prompt_ids,
    ).fetchall()

    response_rows = conn.execute(
        f"""
        SELECT r.prompt_id, GROUP_CONCAT(json_extract(rc.content, '$.text'), '\n') AS text
        FROM responses r
        JOIN response_content rc ON rc.response_id = r.id
        WHERE r.prompt_id IN ({placeholders})
          AND rc.block_type = 'text'
          AND json_extract(rc.content, '$.text') IS NOT NULL
        GROUP BY r.id
    """,
        prompt_ids,
    ).fetchall()
    resp_by_prompt = {row[0]: row[1] for row in response_rows}

    return [
        (row[0], (row[1] or "").strip(), (resp_by_prompt.get(row[0]) or "").strip())
        for row in prompt_rows
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
    """
    conditions = []
    params: list[str] = []
    if conversation_id:
        conditions.append("p.conversation_id = ?")
        params.append(conversation_id)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    prompt_rows = conn.execute(
        f"""
        SELECT
            p.id AS prompt_id,
            p.conversation_id,
            p.timestamp,
            GROUP_CONCAT(json_extract(pc.content, '$.text'), '\n') AS text
        FROM prompts p
        JOIN prompt_content pc ON pc.prompt_id = p.id
        {where}
        AND pc.block_type = 'text'
        AND json_extract(pc.content, '$.text') IS NOT NULL
        GROUP BY p.id
        ORDER BY p.timestamp
    """,
        params,
    ).fetchall()

    response_texts: dict[str, str] = {}
    resp_rows = conn.execute(
        """
        SELECT
            r.prompt_id,
            GROUP_CONCAT(json_extract(rc.content, '$.text'), '\n') AS text
        FROM responses r
        JOIN response_content rc ON rc.response_id = r.id
        WHERE rc.block_type = 'text'
          AND json_extract(rc.content, '$.text') IS NOT NULL
          AND r.prompt_id IS NOT NULL
        GROUP BY r.id
    """
    ).fetchall()
    for row in resp_rows:
        response_texts[row[0]] = row[1]

    exchanges: dict[str, list[dict]] = {}
    for row in prompt_rows:
        conv_id = row[1]
        prompt_text = (row[3] or "").strip()
        response_text = (response_texts.get(row[0]) or "").strip()

        if not prompt_text and not response_text:
            continue

        if conv_id not in exchanges:
            exchanges[conv_id] = []

        exchange_text = ""
        if prompt_text:
            exchange_text += prompt_text
        if response_text:
            if exchange_text:
                exchange_text += "\n\n"
            exchange_text += response_text

        exchanges[conv_id].append({"text": exchange_text, "prompt_id": row[0]})

    return exchanges
