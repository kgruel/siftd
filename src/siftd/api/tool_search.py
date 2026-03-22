"""API for tool-oriented search over the derived tool_search projection."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from siftd.api.database import open_database
from siftd.storage.filters import tag_condition as _tag_condition
from siftd.storage.tool_search import ensure_tool_search_tables, rebuild_tool_search_index
from siftd.tool_query import (
    ToolQuery,
    build_fts5_query,
    expand_tool_names_for_matching,
    normalize_tool_name,
    parse_tool_query,
)


@dataclass
class ToolSearchResult:
    """Single tool-call search result."""

    tool_call_id: str
    conversation_id: str
    response_id: str
    timestamp: str | None
    tool_name: str | None
    tool_family: str | None
    status: str | None
    path: str | None
    basename: str | None
    ext: str | None
    command: str | None
    command_verb: str | None
    pattern: str | None
    arg: str | None
    result_snippet: str | None
    workspace_path: str | None
    rank: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolSearchGroup:
    """Conversation-level grouping for tool-search presentation."""

    conversation_id: str
    workspace_path: str | None
    first_timestamp: str | None
    last_timestamp: str | None
    tool_call_count: int
    tool_names: list[str] = field(default_factory=list)
    results: list[ToolSearchResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["results"] = [r.to_dict() for r in self.results]
        return data


def search_tool_calls(
    q: str,
    *,
    db_path: Path | None = None,
    n: int = 20,
    rebuild_index: bool = False,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
    owner: str | None = None,
) -> tuple[ToolQuery, list[ToolSearchResult]]:
    """Search tool calls using structured fields + FTS over the projection."""
    parsed = _merge_cli_filters(
        parse_tool_query(q),
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        tag=tag,
        all_tags=all_tags,
        no_tag=no_tag,
        tool=tool,
        tool_tag=tool_tag,
    )
    if rebuild_index:
        conn = open_database(db_path, read_only=False)
        try:
            rebuild_tool_search_index(conn, commit=True)
            results = _search_tool_calls_impl(conn, parsed, limit=n, owner=owner)
            return parsed, results
        finally:
            conn.close()

    # Open read-only first; only escalate to writable if the index is stale.
    conn = open_database(db_path, read_only=True)
    try:
        if _needs_rebuild(conn):
            conn.close()
            conn = open_database(db_path, read_only=False)
            ensure_tool_search_tables(conn)
            rebuild_tool_search_index(conn, commit=True)
        results = _search_tool_calls_impl(conn, parsed, limit=n, owner=owner)
        return parsed, results
    finally:
        conn.close()


def _needs_rebuild(conn: sqlite3.Connection) -> bool:
    """Check if the projection is missing or behind tool_calls."""
    try:
        ts_count = conn.execute("SELECT COUNT(*) FROM tool_search").fetchone()[0]
        tc_count = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        return ts_count < tc_count
    except sqlite3.OperationalError:
        return True  # tables missing


def _search_tool_calls_impl(
    conn: sqlite3.Connection,
    parsed: ToolQuery,
    *,
    limit: int,
    owner: str | None = None,
) -> list[ToolSearchResult]:
    base_from = (
        " FROM tool_search ts"
        " LEFT JOIN conversations c ON c.id = ts.conversation_id"
        " LEFT JOIN responses r ON r.id = ts.response_id"
        " LEFT JOIN models m ON m.id = r.model_id"
        " LEFT JOIN providers p ON p.id = r.provider_id"
        " LEFT JOIN harnesses h ON h.id = c.harness_id"
    )

    where: list[str] = []
    params: list[object] = []

    if owner and not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_owners'"
    ).fetchone():
        return []
    _add_owner_clause(where, params, owner)
    _add_tool_name_clauses(where, params, parsed.fields.get("tool"))
    _add_eq_or_clauses(where, params, "ts.tool_family", parsed.fields.get("tool_family"))
    status_values = [*(parsed.fields.get("status") or []), *(parsed.fields.get("result_status") or [])]
    _add_eq_or_clauses(where, params, "ts.status", status_values or None)
    _add_like_or_clauses(where, params, "ts.path", parsed.fields.get("path"))
    _add_like_or_clauses(where, params, "ts.basename", parsed.fields.get("basename"))
    _add_eq_or_clauses(where, params, "ts.ext", parsed.fields.get("ext"))
    _add_like_or_clauses(where, params, "ts.command", parsed.fields.get("cmd"))
    _add_like_or_clauses(where, params, "ts.pattern", parsed.fields.get("pattern"))
    _add_like_or_clauses(where, params, "ts.arg", parsed.fields.get("arg"))
    _add_like_or_clauses(where, params, "ts.result_snippet", parsed.fields.get("result"))
    _add_like_or_clauses(where, params, "ts.workspace_path", parsed.fields.get("workspace"))
    _add_like_or_clauses(where, params, "m.name", parsed.fields.get("model"))
    _add_like_or_clauses(where, params, "p.name", parsed.fields.get("provider"))
    _add_like_or_clauses(where, params, "h.name", parsed.fields.get("harness"))
    _add_since_before(where, params, "c.started_at", parsed.fields.get("since"), op=">=")
    _add_since_before(where, params, "c.started_at", parsed.fields.get("before"), op="<")
    _add_conversation_tags_any(where, params, parsed.fields.get("tag"))
    _add_conversation_tags_all(where, params, parsed.fields.get("all_tags"))
    _add_conversation_tags_none(where, params, parsed.fields.get("no_tag"))
    _add_tool_call_tags(where, params, parsed.fields.get("tool_tag"))

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    fts_query = build_fts5_query(parsed.bare_terms)

    if parsed.free_text and fts_query:
        sql = (
            "SELECT ts.tool_call_id, ts.conversation_id, ts.response_id, ts.timestamp,"
            " ts.tool_name, ts.tool_family, ts.status, ts.path, ts.basename, ts.ext,"
            " ts.command, ts.command_verb, ts.pattern, ts.arg, ts.result_snippet,"
            " ts.workspace_path, bm25(tool_search_fts) AS rank"
            " FROM tool_search_fts"
            " JOIN tool_search ts ON ts.rowid = tool_search_fts.rowid"
            " LEFT JOIN conversations c ON c.id = ts.conversation_id"
            " LEFT JOIN responses r ON r.id = ts.response_id"
            " LEFT JOIN models m ON m.id = r.model_id"
            " LEFT JOIN providers p ON p.id = r.provider_id"
            " LEFT JOIN harnesses h ON h.id = c.harness_id"
            " WHERE tool_search_fts MATCH ?"
        )
        fts_params: list[object] = [fts_query]
        if where:
            sql += " AND " + " AND ".join(where)
            fts_params.extend(params)
        sql += " ORDER BY rank, ts.timestamp DESC LIMIT ?"
        fts_params.append(limit)
        rows = conn.execute(sql, fts_params).fetchall()
    else:
        sql = (
            "SELECT ts.tool_call_id, ts.conversation_id, ts.response_id, ts.timestamp,"
            " ts.tool_name, ts.tool_family, ts.status, ts.path, ts.basename, ts.ext,"
            " ts.command, ts.command_verb, ts.pattern, ts.arg, ts.result_snippet,"
            " ts.workspace_path, NULL AS rank"
            + base_from
            + where_sql
            + " ORDER BY ts.timestamp DESC LIMIT ?"
        )
        rows = conn.execute(sql, [*params, limit]).fetchall()

    return [ToolSearchResult(**dict(row)) for row in rows]


def group_tool_search_results(results: list[ToolSearchResult]) -> list[ToolSearchGroup]:
    """Collapse tool-call results into conversation groups for display."""
    grouped: dict[str, list[ToolSearchResult]] = {}
    order: list[str] = []
    for result in results:
        if result.conversation_id not in grouped:
            grouped[result.conversation_id] = []
            order.append(result.conversation_id)
        grouped[result.conversation_id].append(result)

    groups: list[ToolSearchGroup] = []
    for conversation_id in order:
        items = grouped[conversation_id]
        timestamps = [item.timestamp for item in items if item.timestamp]
        tool_names = sorted({item.tool_name for item in items if item.tool_name})
        groups.append(
            ToolSearchGroup(
                conversation_id=conversation_id,
                workspace_path=items[0].workspace_path,
                first_timestamp=min(timestamps) if timestamps else None,
                last_timestamp=max(timestamps) if timestamps else None,
                tool_call_count=len(items),
                tool_names=tool_names,
                results=items,
            )
        )
    return groups


def _merge_cli_filters(
    parsed: ToolQuery,
    **filters,
) -> ToolQuery:
    fields = {k: list(v) for k, v in parsed.fields.items()}

    def add(field: str, values: str | list[str] | None, *, normalize_tool: bool = False) -> None:
        if not values:
            return
        vals = values if isinstance(values, list) else [values]
        if normalize_tool:
            vals = [normalize_tool_name(v) for v in vals]
        fields.setdefault(field, []).extend(vals)

    add("workspace", filters.get("workspace"))
    add("model", filters.get("model"))
    add("since", filters.get("since"))
    add("before", filters.get("before"))
    add("tag", filters.get("tag"))
    add("all_tags", filters.get("all_tags"))
    add("no_tag", filters.get("no_tag"))
    add("tool", filters.get("tool"), normalize_tool=True)
    add("tool_tag", filters.get("tool_tag"))

    return ToolQuery(
        raw=parsed.raw,
        terms=parsed.terms,
        fields=fields,
        bare_terms=parsed.bare_terms,
        unknown_fields=parsed.unknown_fields,
    )


def _add_eq_or_clauses(where: list[str], params: list[object], column: str, values: list[str] | None) -> None:
    if not values:
        return
    where.append("(" + " OR ".join(f"{column} = ?" for _ in values) + ")")
    params.extend(values)


def _add_tool_name_clauses(where: list[str], params: list[object], values: list[str] | None) -> None:
    if not values:
        return
    expanded: list[str] = []
    seen: set[str] = set()
    for value in values:
        for variant in expand_tool_names_for_matching(value):
            if variant not in seen:
                expanded.append(variant)
                seen.add(variant)
    where.append("(" + " OR ".join("ts.tool_name = ?" for _ in expanded) + ")")
    params.extend(expanded)


def _add_like_or_clauses(where: list[str], params: list[object], column: str, values: list[str] | None) -> None:
    if not values:
        return
    where.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in values) + ")")
    params.extend([f"%{_escape_like(value)}%" for value in values])


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _add_since_before(
    where: list[str],
    params: list[object],
    column: str,
    values: list[str] | None,
    *,
    op: str,
) -> None:
    if not values:
        return
    # Date bounds use AND — each constraint tightens the range.
    for value in values:
        where.append(f"{column} {op} ?")
        params.append(value)


def _add_conversation_tags_any(where: list[str], params: list[object], tags: list[str] | None) -> None:
    if not tags:
        return
    parts = []
    for tag in tags:
        op, val = _tag_condition(tag)
        parts.append(op)
        params.append(val)
    clause = " OR ".join(parts)
    where.append(
        "ts.conversation_id IN (SELECT ct.conversation_id FROM conversation_tags ct"
        f" JOIN tags tg ON tg.id = ct.tag_id WHERE {clause})"
    )


def _add_conversation_tags_all(where: list[str], params: list[object], tags: list[str] | None) -> None:
    if not tags:
        return
    for tag in tags:
        op, val = _tag_condition(tag)
        where.append(
            "ts.conversation_id IN (SELECT ct.conversation_id FROM conversation_tags ct"
            f" JOIN tags tg ON tg.id = ct.tag_id WHERE {op})"
        )
        params.append(val)


def _add_conversation_tags_none(where: list[str], params: list[object], tags: list[str] | None) -> None:
    if not tags:
        return
    parts = []
    for tag in tags:
        op, val = _tag_condition(tag)
        parts.append(op)
        params.append(val)
    clause = " OR ".join(parts)
    where.append(
        "ts.conversation_id NOT IN (SELECT ct.conversation_id FROM conversation_tags ct"
        f" JOIN tags tg ON tg.id = ct.tag_id WHERE {clause})"
    )


def _add_owner_clause(where: list[str], params: list[object], owner: str | None) -> None:
    if not owner:
        return
    where.append(
        "ts.conversation_id IN (SELECT conversation_id FROM conversation_owners WHERE user_id = ?)"
    )
    params.append(owner)


def _add_tool_call_tags(where: list[str], params: list[object], tags: list[str] | None) -> None:
    if not tags:
        return
    parts = []
    for tag in tags:
        op, val = _tag_condition(tag)
        parts.append(op)
        params.append(val)
    clause = " OR ".join(parts)
    where.append(
        "ts.tool_call_id IN (SELECT tct.tool_call_id FROM tool_call_tags tct"
        f" JOIN tags tg ON tg.id = tct.tag_id WHERE {clause})"
    )
