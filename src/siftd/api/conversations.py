"""Conversation listing and detail API."""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from siftd.paths import db_path as default_db_path
from siftd.safecall import parse_json
from siftd.safecall import read_text as _safe_read_text
from siftd.storage.conversation_stats import has_conversation_stats_table
from siftd.storage.filters import WhereBuilder
from siftd.storage.filters import tag_condition as _tag_condition
from siftd.storage.queries import (
    fetch_conversation_by_id_or_prefix,
    fetch_conversation_model,
    fetch_conversation_tags,
    fetch_conversation_token_totals,
    fetch_prompt_text_contents,
    fetch_prompts_for_conversation,
    fetch_response_content_blocks,
    fetch_responses_for_conversation,
    fetch_tags_for_conversations,
    fetch_tool_calls_for_conversation,
    has_pricing_table,
)
from siftd.storage.sql_helpers import (
    batched_in_query,
    cost_expr_sql,
    has_conversation_owners_table,
    owner_predicate,
)
from siftd.storage.sqlite import open_database


@dataclass
class ToolCallSummary:
    """Collapsed tool call for timeline display."""

    tool_name: str
    status: str
    count: int = 1


@dataclass
class ToolCallDetail:
    """Tool call with optional input/result for --tools mode."""

    tool_name: str
    status: str
    count: int = 1
    input: str | None = None
    result: str | None = None
    tool_call_id: str | None = None


@dataclass
class NarrativeBlock:
    """A single block in the response narrative."""

    block_type: str  # "text", "thinking", "tool_calls", "tool_result", "tool_output"
    content: str | None = None
    tool_calls: list[ToolCallDetail] = field(default_factory=list)
    event_id: str | None = None


@dataclass
class Turn:
    """A prompt and its full response narrative."""

    timestamp: str | None
    prompt_text: str | None
    total_input_tokens: int
    total_output_tokens: int
    narrative: list[NarrativeBlock] = field(default_factory=list)
    _tool_call_summaries: list[ToolCallSummary] = field(
        default_factory=list, repr=False,
    )
    prompt_id: str | None = None
    response_ids: list[str] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)

    @property
    def response_text(self) -> str | None:
        """Concatenated text blocks (excludes thinking/tool_calls)."""
        parts = [b.content for b in self.narrative
                 if b.block_type == "text" and b.content]
        return " ".join(parts) if parts else None

    @property
    def tool_call_summaries(self) -> list[ToolCallSummary]:
        """Collapsed tool calls for this turn."""
        return self._tool_call_summaries


@dataclass
class Exchange:
    """A prompt-response pair in the timeline."""

    timestamp: str | None
    prompt_text: str | None
    response_text: str | None
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCallSummary] = field(default_factory=list)


@dataclass
class ConversationSummary:
    """Summary row for conversation listing."""

    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    prompt_count: int
    response_count: int
    total_tokens: int
    cost: float | None
    tags: list[str] = field(default_factory=list)
    owner: str | None = None


@dataclass
class ConversationDetail:
    """Full conversation with timeline."""

    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    total_input_tokens: int
    total_output_tokens: int
    turns: list[Turn]
    tags: list[str] = field(default_factory=list)

    @property
    def exchanges(self) -> list[Exchange]:
        """Backward-compat: derive per-turn exchanges from turns."""
        return _turns_to_exchanges(self.turns)


def _turns_to_exchanges(turns: list[Turn]) -> list[Exchange]:
    """Convert turns to backward-compat exchanges (one per turn)."""
    return [
        Exchange(
            timestamp=t.timestamp,
            prompt_text=t.prompt_text,
            response_text=t.response_text,
            input_tokens=t.total_input_tokens,
            output_tokens=t.total_output_tokens,
            tool_calls=t.tool_call_summaries,
        )
        for t in turns
    ]


def list_conversations(
    *,
    db_path: Path | None = None,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    search: str | None = None,
    tool: str | None = None,
    tag: str | list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    tool_tag: str | None = None,
    n: int = 10,
    oldest: bool = False,
    owner: str | None = None,
) -> list[ConversationSummary]:
    """List conversations with optional filtering.

    Args:
        db_path: Path to database. Uses default if not specified.
        workspace: Filter by workspace path substring.
        model: Filter by model name substring.
        since: Filter conversations started after this date (ISO format).
        before: Filter conversations started before this date.
        search: FTS5 full-text search query.
        tool: Filter by canonical tool name (e.g., 'shell.execute').
        tag: OR filter — conversations with any of these tags. Also accepts
            a single string for backward compat.
        all_tags: AND filter — conversations with all of these tags.
        no_tag: NOT filter — exclude conversations with any of these tags.
        tag_kind: Scope tag/all_tags/no_tag matching to specific target_kinds
            (e.g., ['conversation'], ['response', 'tool_call']). Defaults to
            all conversation-bearing kinds when None.
        tool_tag: Filter by tool call tag (e.g., 'shell:test').
        n: Maximum results to return (0 = unlimited).
        oldest: Sort by oldest first instead of newest.
        owner: Filter to conversations owned by this user_id.

    Returns:
        List of ConversationSummary objects.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)
    try:
        return _list_conversations_impl(conn, workspace, model, since, before, search, tool, tag, all_tags, no_tag, tool_tag, n, oldest, owner, tag_kind)
    finally:
        conn.close()


def _list_conversations_impl(
    conn,
    workspace: str | None,
    model: str | None,
    since: str | None,
    before: str | None,
    search: str | None,
    tool: str | None,
    tag: str | list[str] | None,
    all_tags: list[str] | None,
    no_tag: list[str] | None,
    tool_tag: str | None,
    n: int,
    oldest: bool,
    owner: str | None = None,
    tag_kind: list[str] | None = None,
) -> list[ConversationSummary]:
    """Implementation of list_conversations with connection already open."""
    # Check if pricing table exists
    has_pricing = has_pricing_table(conn)

    # Build WHERE clauses
    wb = WhereBuilder()
    wb.workspace(workspace)
    wb.model(model)
    wb.since(since)
    wb.before(before)
    if owner and not has_conversation_owners_table(conn):
        return []
    wb.owner(owner)

    if search:
        wb.add(
            "c.id IN (SELECT conversation_id FROM content_fts WHERE content_fts MATCH ?)",
            search,
        )

    if tool:
        wb.add(
            "c.id IN (SELECT e.conversation_id FROM events e"
            " JOIN event_tool_call etc ON etc.event_id = e.id"
            " JOIN tools t ON t.id = etc.tool_id"
            " WHERE e.kind = 'tool_call' AND t.name = ?)",
            tool,
        )

    # Normalize tag: accept str (single) or list (OR filter)
    effective_tags = [tag] if isinstance(tag, str) else list(tag or [])

    wb.tags_any(effective_tags or None, kinds=tag_kind)
    wb.tags_all(all_tags, kinds=tag_kind)
    wb.tags_none(no_tag, kinds=tag_kind)

    if tool_tag:
        op, val = _tag_condition(tool_tag)
        wb.add(
            "c.id IN (SELECT e.conversation_id FROM tag_assignments ta"
            " JOIN events e ON e.id = ta.target_id"
            " JOIN tags tg ON tg.id = ta.tag_id"
            f" WHERE ta.target_kind = 'tool_call' AND {op})",
            val,
        )

    where = wb.where_sql()
    params = wb.params
    order = "ASC" if oldest else "DESC"
    limit_clause = f"LIMIT {n}" if n > 0 else ""

    # Phase 1: Identify the target conversations quickly.
    # WhereBuilder tracks which JOINs its filters actually need, so we only
    # join responses/models when a filter (e.g. --model) requires them.
    phase1_joins = wb.joins_sql()
    id_sql = f"""
        SELECT c.id
        FROM conversations c
        {phase1_joins}
        {where}
        ORDER BY c.started_at {order}
        {limit_clause}
    """
    id_rows = conn.execute(id_sql, params).fetchall()
    conv_ids = [row["id"] for row in id_rows]

    if not conv_ids:
        return []

    placeholders = ",".join("?" * len(conv_ids))
    use_stats = has_conversation_stats_table(conn)

    if use_stats:
        # Fast path: read precomputed stats from conversation_stats table.
        # COALESCEs to live subqueries for any rows missing from the stats
        # table (e.g. conversations inserted since the last ingest rebuild).
        rows = conn.execute(
            f"""SELECT c.id AS conversation_id, w.path AS workspace,
                    c.started_at,
                    COALESCE(cs.prompt_count,
                        (SELECT COUNT(*) FROM events WHERE kind = 'prompt' AND conversation_id = c.id)
                    ) AS prompts,
                    COALESCE(cs.response_count,
                        (SELECT COUNT(*) FROM events WHERE kind = 'response' AND conversation_id = c.id)
                    ) AS responses,
                    COALESCE(cs.total_tokens,
                        (SELECT COALESCE(SUM(er2.input_tokens),0) + COALESCE(SUM(er2.output_tokens),0)
                         FROM events e2 JOIN event_response er2 ON er2.event_id = e2.id
                         WHERE e2.kind = 'response' AND e2.conversation_id = c.id)
                    ) AS tokens,
                    COALESCE(cs.model_name,
                        (SELECT m2.name FROM events e2
                         JOIN event_response er2 ON er2.event_id = e2.id
                         LEFT JOIN models m2 ON m2.id = er2.model_id
                         WHERE e2.kind = 'response' AND e2.conversation_id = c.id
                         GROUP BY m2.name ORDER BY COUNT(*) DESC LIMIT 1)
                    ) AS model,
                    cs.cost
                FROM conversations c
                LEFT JOIN workspaces w ON w.id = c.workspace_id
                LEFT JOIN conversation_stats cs ON cs.conversation_id = c.id
                WHERE c.id IN ({placeholders})
                ORDER BY c.started_at {order}""",
            conv_ids,
        ).fetchall()
    else:
        # Fallback: compute from source tables (before first ingest rebuilds
        # the stats table, or if the table was dropped).
        # r2 alias: expose event_id as id so cost_expr_sql's {r}.id works against
        # the polymorphic attributes table (target_id = event_id for responses).
        cost_subquery = (
            f"""(SELECT ROUND(SUM({cost_expr_sql('r2', 'pr', coalesce_pricing=True)}) / 1000000.0, 4)
            FROM events e2
            JOIN (SELECT er2.event_id AS id, er2.model_id, er2.provider_id,
                         er2.input_tokens, er2.output_tokens
                  FROM event_response er2) r2 ON r2.id = e2.id
            LEFT JOIN pricing pr ON pr.model_id = r2.model_id
                                 AND pr.provider_id = r2.provider_id
            WHERE e2.kind = 'response' AND e2.conversation_id = c.id)"""
            if has_pricing
            else "NULL"
        )
        rows = conn.execute(
            f"""SELECT c.id AS conversation_id, w.path AS workspace,
                    (SELECT m2.name FROM events e2
                     JOIN event_response er2 ON er2.event_id = e2.id
                     LEFT JOIN models m2 ON m2.id = er2.model_id
                     WHERE e2.kind = 'response' AND e2.conversation_id = c.id
                     GROUP BY m2.name ORDER BY COUNT(*) DESC LIMIT 1) AS model,
                    c.started_at,
                    (SELECT COUNT(*) FROM events WHERE kind = 'prompt' AND conversation_id = c.id) AS prompts,
                    (SELECT COUNT(*) FROM events WHERE kind = 'response' AND conversation_id = c.id) AS responses,
                    (SELECT COALESCE(SUM(er2.input_tokens), 0) + COALESCE(SUM(er2.output_tokens), 0)
                     FROM events e2 JOIN event_response er2 ON er2.event_id = e2.id
                     WHERE e2.kind = 'response' AND e2.conversation_id = c.id) AS tokens,
                    {cost_subquery} AS cost
                FROM conversations c
                LEFT JOIN workspaces w ON w.id = c.workspace_id
                WHERE c.id IN ({placeholders})
                ORDER BY c.started_at {order}""",
            conv_ids,
        ).fetchall()

    # Bulk-fetch tags and owners
    tags_by_conv = fetch_tags_for_conversations(conn, conv_ids)
    owner_by_conv = _fetch_owners_for_conversations(conn, conv_ids)

    return [
        ConversationSummary(
            id=row["conversation_id"],
            workspace_path=row["workspace"],
            model=row["model"],
            started_at=row["started_at"],
            prompt_count=row["prompts"],
            response_count=row["responses"],
            total_tokens=row["tokens"],
            cost=row["cost"],
            tags=tags_by_conv.get(row["conversation_id"], []),
            owner=owner_by_conv.get(row["conversation_id"]),
        )
        for row in rows
    ]


def _fetch_owners_for_conversations(
    conn,
    conversation_ids: list[str],
) -> dict[str, str]:
    """Bulk fetch owners for multiple conversations.

    Returns dict mapping conversation_id to user_id.
    Returns empty dict if the conversation_owners table doesn't exist
    (e.g. hand-built DBs, pre-migration schemas).
    """
    if not conversation_ids:
        return {}

    # Table may not exist on DBs created outside open_database()
    if not has_conversation_owners_table(conn):
        return {}

    rows = batched_in_query(
        conn,
        "SELECT conversation_id, user_id "
        "FROM conversation_owners "
        "WHERE conversation_id IN ({placeholders})",
        conversation_ids,
    )
    return {row["conversation_id"]: row["user_id"] for row in rows}


def _extract_text(raw: str) -> str:
    """Extract plain text from a content block (may be JSON-wrapped)."""
    obj = parse_json(raw)
    if isinstance(obj, dict) and "text" in obj:
        return obj["text"]
    return raw


def get_conversation(
    id: str,
    *,
    db_path: Path | None = None,
    include_thinking: bool = False,
    include_tool_content: bool = False,
    tool_filter: str | None = None,
    owner: str | None = None,
) -> ConversationDetail | None:
    """Get full conversation detail by ID.

    Supports prefix matching on conversation ID.

    Args:
        id: Full or prefix of conversation ULID.
        db_path: Path to database. Uses default if not specified.
        include_thinking: Include thinking/reasoning blocks in turns.
        include_tool_content: Include tool input/result in turns.
        tool_filter: Filter tool calls — 'errors' for failed only,
            or a tool name prefix (e.g. 'shell', 'file.read').

    Returns:
        ConversationDetail with timeline, or None if not found.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)

    # Find conversation (support prefix match)
    conv = fetch_conversation_by_id_or_prefix(conn, id)
    if not conv:
        conn.close()
        return None

    conv_id = conv["id"]

    if owner:
        if not has_conversation_owners_table(conn):
            conn.close()
            return None
        row = conn.execute(
            f"SELECT 1 FROM conversations c WHERE c.id = ? AND {owner_predicate('c.id')} LIMIT 1",
            (conv_id, owner),
        ).fetchone()
        if not row:
            conn.close()
            return None

    # Model (most frequent) and token totals
    model_name = fetch_conversation_model(conn, conv_id)
    total_input, total_output = fetch_conversation_token_totals(conn, conv_id)

    # Fetch prompts and their text content (bulk)
    prompts = fetch_prompts_for_conversation(conn, conv_id)
    prompt_ids = [p["id"] for p in prompts]
    all_prompt_blocks = fetch_prompt_text_contents(conn, prompt_ids)
    prompt_texts: dict[str, str] = {}
    for pid in prompt_ids:
        blocks = all_prompt_blocks.get(pid, [])
        parts = [_extract_text(b["content"]) for b in blocks]
        prompt_texts[pid] = " ".join(parts).strip()

    # Fetch responses
    responses = fetch_responses_for_conversation(conn, conv_id)
    response_ids = [r["id"] for r in responses]

    # Fetch all content blocks for all responses
    all_content_blocks = fetch_response_content_blocks(conn, response_ids)

    # Fetch tool calls grouped by response
    tool_calls = fetch_tool_calls_for_conversation(
        conn, conv_id, include_content=include_tool_content,
    )
    tc_by_response: dict[str, list] = {}
    for tc in tool_calls:
        tc_by_response.setdefault(tc["response_id"], []).append(tc)

    # Group responses by prompt_id
    responses_by_prompt: dict[str, list] = {}
    for r in responses:
        if r["prompt_id"]:
            responses_by_prompt.setdefault(r["prompt_id"], []).append(r)

    # Build turns (exchanges are derived via property)
    turns = []

    for p in prompts:
        prompt_id = p["id"]
        prompt_text = prompt_texts.get(prompt_id, "")

        prompt_responses = responses_by_prompt.get(prompt_id, [])
        if not prompt_responses:
            # Prompt with no response yet
            turns.append(
                Turn(
                    timestamp=p["timestamp"],
                    prompt_text=prompt_text or None,
                    total_input_tokens=0,
                    total_output_tokens=0,
                    prompt_id=prompt_id,
                )
            )
            continue

        turn_input = sum(r["input_tokens"] or 0 for r in prompt_responses)
        turn_output = sum(r["output_tokens"] or 0 for r in prompt_responses)

        narrative = _build_narrative(
            prompt_responses,
            all_content_blocks,
            tc_by_response,
            include_thinking=include_thinking,
            include_tool_content=include_tool_content,
            tool_filter=tool_filter,
        )

        # Build tool call summaries from DB data (independent of narrative)
        all_tcs = []
        for r in prompt_responses:
            all_tcs.extend(tc_by_response.get(r["id"], []))
        tool_summaries = _collapse_tool_call_rows(all_tcs)

        turn_response_ids = [r["id"] for r in prompt_responses]
        turn_tool_call_ids = [tc["tool_call_id"] for tc in all_tcs]

        turns.append(
            Turn(
                timestamp=p["timestamp"],
                prompt_text=prompt_text or None,
                total_input_tokens=turn_input,
                total_output_tokens=turn_output,
                narrative=narrative,
                _tool_call_summaries=tool_summaries,
                prompt_id=prompt_id,
                response_ids=turn_response_ids,
                tool_call_ids=turn_tool_call_ids,
            )
        )

    # Fetch tags
    tags = fetch_conversation_tags(conn, conv_id)

    conn.close()

    return ConversationDetail(
        id=conv_id,
        workspace_path=conv["workspace"],
        model=model_name,
        started_at=conv["started_at"],
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        turns=turns,
        tags=tags,
    )


def _matches_tool_filter(tool_name: str, status: str, tool_filter: str | None) -> bool:
    """Check if a tool call matches the given filter."""
    if tool_filter is None:
        return True
    if tool_filter == "errors":
        return status == "error"
    name = tool_name or ""
    return name == tool_filter or name.startswith(tool_filter + ".")


def _collapse_tool_call_rows(tool_calls: list) -> list[ToolCallSummary]:
    """Collapse consecutive tool call DB rows with same name+status."""
    if not tool_calls:
        return []

    collapsed = []
    prev_key = None
    count = 0

    for tc in tool_calls:
        name = tc["tool_name"] or "unknown"
        status = tc["status"] or "unknown"
        key = (name, status)

        if key == prev_key:
            count += 1
        else:
            if prev_key is not None:
                collapsed.append(
                    ToolCallSummary(tool_name=prev_key[0], status=prev_key[1], count=count)
                )
            prev_key = key
            count = 1

    if prev_key is not None:
        collapsed.append(
            ToolCallSummary(tool_name=prev_key[0], status=prev_key[1], count=count)
        )

    return collapsed


def _build_narrative(
    prompt_responses: list,
    all_content_blocks: dict[str, list],
    tc_by_response: dict[str, list],
    *,
    include_thinking: bool,
    include_tool_content: bool,
    tool_filter: str | None,
) -> list[NarrativeBlock]:
    """Build interleaved narrative blocks from response content.

    Walks all response content blocks in order (across all responses for
    a prompt), building text/thinking/tool_calls NarrativeBlocks.
    """
    narrative: list[NarrativeBlock] = []

    for r in prompt_responses:
        resp_id = r["id"]
        content_blocks = all_content_blocks.get(resp_id, [])
        resp_tool_calls = tc_by_response.get(resp_id, [])

        # Index into tool_calls list for fallback order; tool_use blocks
        # are best matched by external_id when available.
        tc_idx = 0
        tc_by_id = {}
        for tc in resp_tool_calls:
            tc_id = tc["external_id"]
            if tc_id:
                tc_by_id[tc_id] = tc
        used_ids: set[str] = set()
        # Accumulate consecutive tool calls for collapsing
        pending_tools: list[ToolCallDetail] = []

        def _flush_tools(narrative=narrative, resp_id=resp_id):
            nonlocal pending_tools
            if pending_tools:
                narrative.append(NarrativeBlock(
                    block_type="tool_calls",
                    tool_calls=_collapse_tool_details(
                        pending_tools,
                        collapse=not include_tool_content,
                    ),
                    event_id=resp_id,
                ))
                pending_tools = []

        for block in content_blocks:
            block_type = block["block_type"]

            if block_type == "text":
                _flush_tools()

                text = _extract_text(block["content"])
                if text.strip():
                    narrative.append(NarrativeBlock(
                        block_type="text",
                        content=text,
                        event_id=resp_id,
                    ))

            elif block_type == "thinking":
                if include_thinking:
                    _flush_tools()

                    text = _extract_thinking(block["content"])
                    if text.strip():
                        narrative.append(NarrativeBlock(
                            block_type="thinking",
                            content=text,
                            event_id=resp_id,
                        ))

            elif block_type == "tool_use":
                # Match to corresponding tool_call row by external_id (preferred),
                # fallback to order when missing or unmatched.
                tc = None
                tool_use_id = _extract_tool_use_id(block["content"])
                if tool_use_id:
                    tc = tc_by_id.get(tool_use_id)
                    if tc:
                        used_ids.add(tool_use_id)
                if tc is None:
                    while tc_idx < len(resp_tool_calls):
                        candidate = resp_tool_calls[tc_idx]
                        tc_idx += 1
                        candidate_id = candidate["external_id"]
                        if candidate_id and candidate_id in used_ids:
                            continue
                        tc = candidate
                        if candidate_id:
                            used_ids.add(candidate_id)
                        break

                if tc:
                    name = tc["tool_name"] or "unknown"
                    status = tc["status"] or "unknown"

                    if _matches_tool_filter(name, status, tool_filter):
                        detail = ToolCallDetail(
                            tool_name=name,
                            status=status,
                            input=tc["input"] if include_tool_content else None,
                            result=tc["result"] if include_tool_content else None,
                            tool_call_id=tc["tool_call_id"],
                        )
                        pending_tools.append(detail)

            elif block_type in ("tool_result", "tool_output"):
                _flush_tools()

                text = _extract_tool_result(block["content"])
                if text.strip():
                    narrative.append(NarrativeBlock(
                        block_type=block_type,
                        content=text,
                        event_id=resp_id,
                    ))

        # Flush remaining tool calls
        _flush_tools()

    return narrative


def _extract_thinking(raw: str) -> str:
    """Extract thinking text from a content block."""
    obj = raw if isinstance(raw, dict) else parse_json(raw)
    if isinstance(obj, dict):
        if "thinking" in obj:
            return obj.get("thinking", "")
        if "text" in obj:
            return obj.get("text", "")
        if "description" in obj or "subject" in obj:
            subject = obj.get("subject")
            description = obj.get("description")
            if subject and description:
                return f"{subject}: {description}"
            return description or subject or ""
    return raw


def _extract_tool_use_id(raw: str) -> str | None:
    """Extract tool_use id from a content block."""
    obj = raw if isinstance(raw, dict) else parse_json(raw)
    if isinstance(obj, dict):
        return obj.get("id") or obj.get("tool_use_id") or obj.get("call_id")
    return None


def _extract_tool_result(raw: str) -> str:
    """Extract display text from a tool_result/tool_output block."""
    obj = raw if isinstance(raw, dict) else parse_json(raw)
    if isinstance(obj, dict):
        for key in ("text", "content", "output", "result"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    if "text" in val and isinstance(val["text"], str):
                        return val["text"]
                    return json.dumps(val)
                if isinstance(val, list):
                    parts = []
                    for item in val:
                        if isinstance(item, str):
                            parts.append(item)
                        elif isinstance(item, dict) and "text" in item:
                            parts.append(str(item["text"]))
                        else:
                            parts.append(json.dumps(item))
                    return "\n".join(parts)
                return str(val)
    return raw


def _collapse_tool_details(
    tools: list[ToolCallDetail],
    *,
    collapse: bool,
) -> list[ToolCallDetail]:
    """Collapse consecutive tool calls with same name+status."""
    if not tools:
        return []
    if not collapse:
        return tools

    collapsed: list[ToolCallDetail] = []
    prev = tools[0]
    count = 1

    for tc in tools[1:]:
        if tc.tool_name == prev.tool_name and tc.status == prev.status:
            count += 1
        else:
            collapsed.append(ToolCallDetail(
                tool_name=prev.tool_name,
                status=prev.status,
                count=count,
                input=prev.input,
                result=prev.result,
                tool_call_id=prev.tool_call_id if count == 1 else None,
            ))
            prev = tc
            count = 1

    collapsed.append(ToolCallDetail(
        tool_name=prev.tool_name,
        status=prev.status,
        count=count,
        input=prev.input,
        result=prev.result,
        tool_call_id=prev.tool_call_id if count == 1 else None,
    ))
    return collapsed


# =============================================================================
# User-defined SQL query files
# =============================================================================


@dataclass
class QueryFile:
    """Metadata about a user-defined SQL query file.

    Attributes:
        name: Query file stem (without .sql extension).
        path: Full path to the .sql file.
        template_vars: Variables using $var syntax (text substitution).
        param_vars: Variables using :var syntax (parameterized, safe).
        variables: All variable names (union of template_vars and param_vars).
    """

    name: str
    path: Path
    template_vars: list[str]
    param_vars: list[str]

    @property
    def variables(self) -> list[str]:
        """All variable names (template + param)."""
        return sorted(set(self.template_vars + self.param_vars))


@dataclass
class QueryResult:
    """Result of running a SQL query file."""

    columns: list[str]
    rows: list[list]


def list_query_files() -> list[QueryFile]:
    """List available user-defined SQL query files.

    Scans the queries directory for .sql files and extracts variable names.
    Distinguishes between:
    - Template variables ($var): text substitution, for structural elements
    - Param variables (:var): parameterized, for values (safe quoting)

    Returns:
        List of QueryFile with name, path, and required variables.
    """
    import re

    from siftd.paths import queries_dir

    qdir = queries_dir()
    if not qdir.exists():
        return []

    template_pattern = re.compile(r"\$\{(\w+)\}|\$(\w+)")
    # Match :var but not ::var (Postgres cast) or :=var (assignment)
    param_pattern = re.compile(r"(?<!:):(\w+)\b(?!=)")
    result = []

    for f in sorted(qdir.glob("*.sql")):
        sql = _safe_read_text(f, context="list_query_files")
        if sql is None:
            continue
        template_matches = template_pattern.findall(sql)
        template_vars = sorted(set(m[0] or m[1] for m in template_matches))

        param_matches = param_pattern.findall(sql)
        param_vars = sorted(set(param_matches))

        result.append(
            QueryFile(
                name=f.stem,
                path=f,
                template_vars=template_vars,
                param_vars=param_vars,
            )
        )

    return result


class QueryError(Exception):
    """Error running a SQL query file."""

    pass


def run_query_file(
    name: str,
    variables: dict[str, str] | None = None,
    *,
    db_path: Path | None = None,
) -> QueryResult:
    """Run a user-defined SQL query file.

    Supports two variable syntaxes:
    - $var or ${var}: Text substitution (for structural elements like tables)
    - :var: Parameterized query (for values, with safe quoting)

    Args:
        name: Query file name (without .sql extension).
        variables: Dict of variable values. Same dict serves both syntaxes.
        db_path: Path to database. Uses default if not specified.

    Returns:
        QueryResult with columns and rows.

    Raises:
        FileNotFoundError: If database or query file doesn't exist.
        QueryError: If variables are missing or SQL fails.

    Example:
        SQL file with both syntaxes::

            SELECT * FROM $table
            WHERE workspace LIKE '%' || :ws || '%'
              AND started_at > :since

        Call with: run_query_file("myquery", {"table": "conversations",
                                              "ws": "project", "since": "2025-01"})
    """
    import re
    import sqlite3
    from string import Template

    from siftd.paths import queries_dir

    db = db_path or default_db_path()
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    qdir = queries_dir()
    sql_file = qdir / f"{name}.sql"
    if not sql_file.exists():
        raise QueryError(f"Query file not found: {sql_file}")

    try:
        sql = sql_file.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise QueryError(f"Cannot read query file {sql_file}: {e}") from e
    variables = variables or {}

    # 1. Extract :param names before $var substitution
    # Match :var but not ::var (Postgres cast) or :=var (assignment)
    param_pattern = re.compile(r"(?<!:):(\w+)\b(?!=)")
    param_names = set(param_pattern.findall(sql))

    # 2. Text-substitute $var / ${var}
    sql = Template(sql).safe_substitute(variables)

    # 3. Check for unsubstituted $vars
    remaining_template = re.findall(r"\$\{(\w+)\}|\$(\w+)", sql)
    if remaining_template:
        missing = sorted(set(m[0] or m[1] for m in remaining_template))
        raise QueryError(f"Missing template variables: {', '.join(missing)}")

    # 4. Build params dict for :var (only those present in SQL)
    params = {k: v for k, v in variables.items() if k in param_names}

    # 5. Check for unbound :params
    unbound = param_names - set(params.keys())
    if unbound:
        raise QueryError(f"Missing parameter variables: {', '.join(sorted(unbound))}")

    # 6. Execute with params (query_only prevents accidental mutation)
    conn = open_database(db, read_only=False)
    conn.execute("PRAGMA query_only = ON")

    try:
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        last_rows = None
        for stmt in statements:
            # Pass params to each statement (sqlite3 uses :name syntax)
            cursor = conn.execute(stmt, params)
            if cursor.description:
                last_rows = (cursor.description, cursor.fetchall())

        if last_rows:
            desc, rows = last_rows
            columns = [d[0] for d in desc]
            row_data = [
                [v if v is not None else None for v in row] for row in rows
            ]
            return QueryResult(columns=columns, rows=row_data)
        else:
            return QueryResult(columns=[], rows=[])

    except sqlite3.Error as e:
        raise QueryError(f"SQL error: {e}") from e
    finally:
        conn.close()


def get_recent_conversation_ids(
    conn: sqlite3.Connection,
    limit: int,
    *,
    owner: str | None = None,
) -> list[str]:
    """Get IDs of the most recent conversations.

    Args:
        conn: Database connection.
        limit: Number of conversations to return.

    Returns:
        List of conversation IDs, most recent first.
    """
    if owner and not has_conversation_owners_table(conn):
        return []

    where: list[str] = []
    params: list[object] = []
    if owner:
        where.append(owner_predicate("c.id"))
        params.append(owner)

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT c.id FROM conversations c{where_sql} ORDER BY c.started_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [row["id"] for row in rows]


def resolve_entity_id(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    *,
    owner: str | None = None,
) -> str | None:
    """Resolve an entity ID, supporting prefix match for conversations.

    Args:
        conn: Database connection.
        entity_type: One of 'conversation', 'workspace', 'tool_call'.
        entity_id: Full or prefix ID to look up.

    Returns:
        Resolved full ID, or None if not found.
    """
    if entity_type == "conversation":
        if owner and not has_conversation_owners_table(conn):
            return None

        where = ["(c.id = ? OR c.id LIKE ?)"]
        params: list[object] = [entity_id, f"{entity_id}%"]
        if owner:
            where.append(owner_predicate("c.id"))
            params.append(owner)

        row = conn.execute(
            f"SELECT c.id FROM conversations c WHERE {' AND '.join(where)}",
            params,
        ).fetchone()
    elif entity_type == "workspace":
        row = conn.execute("SELECT id FROM workspaces WHERE id = ?", (entity_id,)).fetchone()
    elif entity_type in ("tool_call", "prompt", "response"):
        # Prefix-match across event kinds so `siftd query <event_prefix>`
        # works the same as conversation IDs.
        kind = entity_type
        row = conn.execute(
            "SELECT id FROM events"
            " WHERE (id = ? OR id LIKE ?) AND kind = ?",
            (entity_id, f"{entity_id}%", kind),
        ).fetchone()
    elif entity_type == "exchange":
        # exchange uses a prompt event as anchor
        row = conn.execute(
            "SELECT id FROM events"
            " WHERE (id = ? OR id LIKE ?) AND kind = 'prompt'",
            (entity_id, f"{entity_id}%"),
        ).fetchone()
    else:
        return None
    return row["id"] if row else None


def get_conversation_metadata(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> dict | None:
    """Get conversation metadata (workspace, started_at) by ID or prefix.

    Args:
        conn: Database connection.
        conversation_id: Full or prefix ID to look up.

    Returns:
        Dict with keys 'id', 'workspace', 'started_at', or None if not found.
    """
    row = conn.execute(
        "SELECT c.id, c.started_at, w.path AS workspace "
        "FROM conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id "
        "WHERE c.id = ? OR c.id LIKE ? "
        "LIMIT 1",
        (conversation_id, f"{conversation_id}%"),
    ).fetchone()
    return dict(row) if row else None
