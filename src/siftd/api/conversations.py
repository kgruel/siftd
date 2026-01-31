"""Conversation listing and detail API."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from siftd.paths import db_path as default_db_path
from siftd.storage.filters import WhereBuilder
from siftd.storage.filters import tag_condition as _tag_condition
from siftd.storage.queries import (
    fetch_conversation_by_id_or_prefix,
    fetch_conversation_model,
    fetch_conversation_tags,
    fetch_conversation_token_totals,
    fetch_prompt_text_content,
    fetch_prompts_for_conversation,
    fetch_response_text_content,
    fetch_responses_for_conversation,
    fetch_tags_for_conversations,
    fetch_tool_calls_for_conversation,
    has_pricing_table,
)
from siftd.storage.sqlite import open_database


@dataclass
class ToolCallSummary:
    """Collapsed tool call for timeline display."""

    tool_name: str
    status: str
    count: int = 1


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


@dataclass
class ConversationDetail:
    """Full conversation with timeline."""

    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    total_input_tokens: int
    total_output_tokens: int
    exchanges: list[Exchange]
    tags: list[str] = field(default_factory=list)


def list_conversations(
    *,
    db_path: Path | None = None,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    search: str | None = None,
    tool: str | None = None,
    tag: str | None = None,
    tags: list[str] | None = None,
    all_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    tool_tag: str | None = None,
    limit: int = 10,
    oldest_first: bool = False,
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
        tag: Filter by tag name (single, backward compat — prefer tags).
        tags: OR filter — conversations with any of these tags.
        all_tags: AND filter — conversations with all of these tags.
        exclude_tags: NOT filter — exclude conversations with any of these tags.
        tool_tag: Filter by tool call tag (e.g., 'shell:test').
        limit: Maximum results to return (0 = unlimited).
        oldest_first: Sort by oldest first instead of newest.

    Returns:
        List of ConversationSummary objects.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)

    # Check if pricing table exists
    has_pricing = has_pricing_table(conn)

    # Build WHERE clauses
    wb = WhereBuilder()
    wb.workspace(workspace)
    wb.model(model)
    wb.since(since)
    wb.before(before)

    if search:
        wb.add(
            "c.id IN (SELECT conversation_id FROM content_fts WHERE content_fts MATCH ?)",
            search,
        )

    if tool:
        wb.add(
            "c.id IN (SELECT tc.conversation_id FROM tool_calls tc"
            " JOIN tools t ON t.id = tc.tool_id WHERE t.name = ?)",
            tool,
        )

    # Legacy single-tag support: fold into the OR list
    effective_tags = list(tags or [])
    if tag:
        effective_tags.append(tag)

    wb.tags_any(effective_tags or None)
    wb.tags_all(all_tags)
    wb.tags_none(exclude_tags)

    if tool_tag:
        op, val = _tag_condition(tool_tag)
        wb.add(
            "c.id IN (SELECT tc.conversation_id FROM tool_calls tc"
            " JOIN tool_call_tags tct ON tct.tool_call_id = tc.id"
            f" JOIN tags tg ON tg.id = tct.tag_id WHERE {op})",
            val,
        )

    where = wb.where_sql()
    params = wb.params
    order = "ASC" if oldest_first else "DESC"
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""

    cost_expr = (
        """ROUND(SUM(
            COALESCE(r.input_tokens, 0) * COALESCE(pr.input_per_mtok, 0)
            + COALESCE(r.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
        ) / 1000000.0, 4)"""
        if has_pricing
        else "NULL"
    )
    pricing_join = (
        "LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id"
        if has_pricing
        else ""
    )

    sql = f"""
        SELECT
            c.id AS conversation_id,
            w.path AS workspace,
            (SELECT m2.name FROM responses r2
             LEFT JOIN models m2 ON m2.id = r2.model_id
             WHERE r2.conversation_id = c.id
             GROUP BY m2.name
             ORDER BY COUNT(*) DESC
             LIMIT 1) AS model,
            c.started_at,
            (SELECT COUNT(*) FROM prompts WHERE conversation_id = c.id) AS prompts,
            COUNT(DISTINCT r.id) AS responses,
            COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0) AS tokens,
            {cost_expr} AS cost
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        LEFT JOIN responses r ON r.conversation_id = c.id
        LEFT JOIN models m ON m.id = r.model_id
        LEFT JOIN providers pv ON pv.id = r.provider_id
        {pricing_join}
        {where}
        GROUP BY c.id
        ORDER BY c.started_at {order}
        {limit_clause}
    """

    rows = conn.execute(sql, params).fetchall()

    # Bulk-fetch tags for returned conversations (single query, no N+1)
    conv_ids = [row["conversation_id"] for row in rows]
    tags_by_conv = fetch_tags_for_conversations(conn, conv_ids)

    conn.close()

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
        )
        for row in rows
    ]


def _extract_text(raw: str) -> str:
    """Extract plain text from a content block (may be JSON-wrapped)."""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "text" in obj:
            return obj["text"]
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


def get_conversation(
    conversation_id: str,
    *,
    db_path: Path | None = None,
) -> ConversationDetail | None:
    """Get full conversation detail by ID.

    Supports prefix matching on conversation ID.

    Args:
        conversation_id: Full or prefix of conversation ULID.
        db_path: Path to database. Uses default if not specified.

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
    conv = fetch_conversation_by_id_or_prefix(conn, conversation_id)
    if not conv:
        conn.close()
        return None

    conv_id = conv["id"]

    # Model (most frequent) and token totals
    model_name = fetch_conversation_model(conn, conv_id)
    total_input, total_output = fetch_conversation_token_totals(conn, conv_id)

    # Fetch prompts and their text content
    prompts = fetch_prompts_for_conversation(conn, conv_id)
    prompt_texts: dict[str, str] = {}
    for p in prompts:
        blocks = fetch_prompt_text_content(conn, p["id"])
        parts = [_extract_text(b["content"]) for b in blocks]
        prompt_texts[p["id"]] = " ".join(parts).strip()

    # Fetch responses and their text content
    responses = fetch_responses_for_conversation(conn, conv_id)
    response_texts: dict[str, str] = {}
    for r in responses:
        blocks = fetch_response_text_content(conn, r["id"])
        parts = [_extract_text(b["content"]) for b in blocks]
        response_texts[r["id"]] = " ".join(parts).strip()

    # Fetch tool calls grouped by response
    tool_calls = fetch_tool_calls_for_conversation(conn, conv_id)
    tc_by_response: dict[str, list] = {}
    for tc in tool_calls:
        tc_by_response.setdefault(tc["response_id"], []).append(tc)

    # Build exchanges: pair prompts with their responses
    # Group responses by prompt_id
    responses_by_prompt: dict[str, list] = {}
    for r in responses:
        if r["prompt_id"]:
            responses_by_prompt.setdefault(r["prompt_id"], []).append(r)

    exchanges = []
    for p in prompts:
        prompt_id = p["id"]
        prompt_text = prompt_texts.get(prompt_id, "")

        # Get responses for this prompt
        prompt_responses = responses_by_prompt.get(prompt_id, [])
        if not prompt_responses:
            # Prompt with no response yet
            exchanges.append(
                Exchange(
                    timestamp=p["timestamp"],
                    prompt_text=prompt_text or None,
                    response_text=None,
                    input_tokens=0,
                    output_tokens=0,
                    tool_calls=[],
                )
            )
            continue

        # Usually one response per prompt, but handle multiple
        for r in prompt_responses:
            response_text = response_texts.get(r["id"], "")

            # Collapse consecutive same tool+status calls
            tcs = tc_by_response.get(r["id"], [])
            collapsed_tools = _collapse_tool_calls(tcs)

            exchanges.append(
                Exchange(
                    timestamp=r["timestamp"] or p["timestamp"],
                    prompt_text=prompt_text or None,
                    response_text=response_text or None,
                    input_tokens=r["input_tokens"] or 0,
                    output_tokens=r["output_tokens"] or 0,
                    tool_calls=collapsed_tools,
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
        exchanges=exchanges,
        tags=tags,
    )


def _collapse_tool_calls(tool_calls: list) -> list[ToolCallSummary]:
    """Collapse consecutive tool calls with same name+status."""
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
        sql = f.read_text()
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
        raise FileNotFoundError(f"Query file not found: {sql_file}")

    sql = sql_file.read_text()
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

    # 6. Execute with params
    conn = open_database(db, read_only=False)

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
