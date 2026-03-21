"""HTML route handlers for htmx-driven web UI.

Returns HTML fragments for htmx swaps. The page shell (GET /) serves the
single full-page response; everything else is a fragment.

Content negotiation: routes check for HX-Request header or Accept: text/html.
JSON API routes in routes.py remain untouched.
"""

from __future__ import annotations

from pathlib import Path

from litestar import Request, get
from litestar.params import Parameter
from litestar.response import Response


def _wants_html(request: Request) -> bool:
    """True if this is an htmx request or explicitly wants HTML."""
    if request.headers.get("HX-Request"):
        return True
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


def _html_response(content: str) -> Response:
    return Response(content=content, media_type="text/html")


def _fidelity(
    depth: int = 1,
    chars: int = 200,
    *,
    tools: bool = False,
    thinking: bool = False,
):
    from painted import Fidelity

    visible: set[str] = {"text"}
    if tools:
        visible.add("tools")
    if thinking:
        visible.add("thinking")
    return Fidelity(depth=depth, visible=frozenset(visible), chars=chars)


def _tool_chars(fidelity) -> int:
    """Derive tool content char limit from fidelity (mirrors cli_common logic)."""
    if fidelity.depth >= 3:
        return 0
    return 120


# ---------------------------------------------------------------------------
# Page shell — the only full-page response
# ---------------------------------------------------------------------------

def _page_shell(
    *,
    conv_id: str | None = None,
    search_q: str | None = None,
    follow_sid: str | None = None,
) -> str:
    """Build the page shell with optional initial state for deep links."""
    from html import escape as esc

    # Search bar: pre-populate if search_q provided
    search_val = f' value="{esc(search_q)}"' if search_q else ""

    # List pane: search results, peek sessions, or conversation list
    if search_q:
        list_url = f"/ui/search?q={esc(search_q)}"
    elif follow_sid:
        list_url = "/ui/peek"
    else:
        list_url = "/ui/query"

    # Detail pane: auto-load conversation, follow session, or empty
    if follow_sid:
        detail_attr = f' hx-get="/ui/follow?sid={esc(follow_sid)}" hx-trigger="load" hx-swap="innerHTML"'
        detail_content = '<p class="empty">Loading session...</p>'
    elif conv_id:
        detail_attr = f' hx-get="/ui/query?id={esc(conv_id)}" hx-trigger="load" hx-swap="innerHTML"'
        detail_content = '<p class="empty">Loading...</p>'
    else:
        detail_attr = ""
        detail_content = '<p class="empty">Select a conversation</p>'

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>siftd</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<link rel="stylesheet" href="/static/siftd.css">
</head>
<body>
<nav>
  <span class="brand">siftd</span>
  <input type="search" name="q" placeholder="Search..."{search_val}
    hx-get="/ui/search" hx-target="#list" hx-trigger="keyup changed delay:300ms"
    hx-include="this">
  <a href="/ui" hx-get="/ui/query" hx-target="#list" hx-push-url="/ui"
    hx-on::before-request="document.querySelectorAll('#filters select,#filters input').forEach(e=>e.value='')"
    style="color:var(--accent);text-decoration:none">Recent</a>
  <a href="#" hx-get="/ui/peek" hx-target="#list"
    style="color:var(--accent);text-decoration:none">Live</a>
  <a href="#" hx-get="/ui/stats" hx-target="#detail" hx-swap="innerHTML"
    style="color:var(--accent);text-decoration:none">Stats</a>
</nav>
<main>
  <div id="list-pane">
    <div id="filters" hx-get="/ui/meta" hx-trigger="load" hx-swap="innerHTML">
    </div>
    <div id="list" hx-get="{esc(list_url)}" hx-trigger="load" hx-swap="innerHTML"
      hx-include="#filters">
    </div>
  </div>
  <div id="divider"></div>
  <div id="detail-pane">
    <div id="detail"{detail_attr}>
      {detail_content}
    </div>
  </div>
</main>
<script>
(function() {{
  const d = document.getElementById('divider');
  const lp = document.getElementById('list-pane');
  const m = document.querySelector('main');
  let dragging = false;
  d.addEventListener('mousedown', function(e) {{
    dragging = true; e.preventDefault();
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }});
  document.addEventListener('mousemove', function(e) {{
    if (!dragging) return;
    const pct = ((e.clientX - m.offsetLeft) / m.offsetWidth) * 100;
    if (pct > 15 && pct < 85) {{
      lp.style.width = pct + '%';
      document.getElementById('detail-pane').style.width = (100 - pct) + '%';
    }}
  }});
  document.addEventListener('mouseup', function() {{
    if (dragging) {{
      dragging = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }}
  }});
}})();
</script>
</body>
</html>"""


@get("/ui", opt={"no_auth": True})
async def ui_shell(
    id: str | None = Parameter(query="id", default=None),
    q: str | None = Parameter(query="q", default=None),
    follow: str | None = Parameter(query="follow", default=None),
) -> Response:
    """Serve the page shell — the single full-page HTML response.

    Accepts optional params for deep-linkable URLs:
        ?id=   — open a conversation
        ?q=    — pre-populate search
        ?follow= — follow a live session
    """
    return Response(
        content=_page_shell(conv_id=id, search_q=q, follow_sid=follow),
        media_type="text/html",
    )


# ---------------------------------------------------------------------------
# Fragment endpoints — htmx swap targets
# ---------------------------------------------------------------------------


@get("/ui/meta", opt={"no_auth": True})
async def ui_meta(db_path: Path) -> Response:
    """Return filter dropdowns populated from the database."""
    from html import escape

    from siftd.api.stats import get_stats, list_workspaces
    from siftd.api.tags import list_tags

    stats = None
    try:
        stats = get_stats(db_path=db_path)
    except Exception:
        pass

    ws_rows: list = []
    try:
        ws_rows = list_workspaces(db_path=db_path, limit=200)
    except Exception:
        pass

    tag_names: list[str] = []
    try:
        tag_names = [t.name for t in list_tags(db_path=db_path)]
    except Exception:
        pass

    def _select(name: str, label: str, options: list[tuple[str, str]]) -> str:
        """Build a <select> from (value, display_text) tuples."""
        opts = [f'<option value="">All {label}</option>']
        for val, display in options:
            if val:
                opts.append(f'<option value="{escape(val)}">{escape(display)}</option>')
        return (
            f'<select name="{name}"'
            f' hx-get="/ui/query" hx-target="#list" hx-trigger="change"'
            f' hx-include="#filters">'
            f'{"".join(opts)}</select>'
        )

    from siftd.output.common import fmt_workspace

    ws_opts = [(r["path"], fmt_workspace(r["path"])) for r in ws_rows if r["path"]]
    model_opts = [(m, m) for m in (stats.models if stats else [])]
    tag_opts = [(t, t) for t in tag_names]

    def _date(name: str, label: str) -> str:
        return (
            f'<input type="date" name="{name}" title="{label}"'
            f' hx-get="/ui/query" hx-target="#list" hx-trigger="change"'
            f' hx-include="#filters">'
        )

    parts = [
        _select("workspace", "workspaces", ws_opts),
        _select("model", "models", model_opts),
        _select("tag", "tags", tag_opts),
        _date("since", "Since"),
        _date("before", "Before"),
    ]
    return _html_response("".join(parts))


@get("/ui/query", opt={"no_auth": True})
async def ui_query(
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    search: str | None = Parameter(query="search", default=None),
    n: int = Parameter(query="n", default=50),
    id: str | None = Parameter(query="id", default=None),
    # Fidelity controls (detail view)
    tools: bool = Parameter(query="tools", default=False),
    thinking: bool = Parameter(query="thinking", default=False),
    full: bool = Parameter(query="full", default=False),
    brief: bool = Parameter(query="brief", default=False),
) -> Response:
    """List or detail conversations as HTML fragments."""
    # Normalize empty strings to None (htmx sends "" for blank inputs)
    workspace = workspace or None
    model = model or None
    search = search or None
    since = since or None
    before = before or None
    tag = [t for t in (tag or []) if t] or None

    from siftd.output.format_registry import get_format

    fmt = get_format("html")

    if id is not None:
        from siftd.api.conversations import get_conversation

        # Build fidelity from query params — same logic as CLI flags
        if full:
            fidelity = _fidelity(depth=3, chars=0, tools=True, thinking=True)
        elif brief:
            fidelity = _fidelity(depth=0, chars=80)
        else:
            fidelity = _fidelity(depth=2, chars=0, tools=tools, thinking=thinking)

        detail = get_conversation(
            id,
            db_path=db_path,
            include_thinking=fidelity.shows("thinking"),
            include_tool_content=fidelity.shows("tools"),
        )
        if detail is None:
            return _html_response(f'<p class="empty">Not found: {id[:12]}</p>')

        tc = _tool_chars(fidelity)
        html = fmt.render_detail(
            detail.turns,
            fidelity,
            detail=detail,
            tool_chars=tc,
            detail_base="/ui/query",
            shell_base="/ui",
            controls={"id": id, "tools": tools, "thinking": thinking,
                      "full": full, "brief": brief},
        )
        return _html_response(html)

    from siftd.api.conversations import list_conversations

    rows = list_conversations(
        db_path=db_path,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        search=search,
        tags=tag,
        limit=n,
    )
    html = fmt.render_list(rows, _fidelity(), detail_base="/ui/query", shell_base="/ui")
    return _html_response(html)


@get("/ui/search", opt={"no_auth": True})
async def ui_search(
    db_path: Path,
    q: str = Parameter(query="q", default=""),
    mode: str = Parameter(query="mode", default="chunks"),
) -> Response:
    """Search conversations, return HTML fragment.

    Tries semantic search (embeddings) first, falls back to FTS5.
    Modes: chunks (default), conversations (aggregated scores).
    """
    from html import escape

    from siftd.output.format_registry import get_format

    if not q.strip():
        return _html_response('<p class="empty">Type to search...</p>')

    fmt = get_format("html")
    ctx = {"detail_base": "/ui/query", "shell_base": "/ui", "query": q, "mode": mode}

    # Mode toggle tabs
    def _tab(label: str, m: str) -> str:
        active = " active" if m == mode else ""
        return (
            f'<button class="toggle{active}"'
            f' hx-get="/ui/search?q={escape(q)}&mode={m}"'
            f' hx-target="#list" hx-swap="innerHTML">{label}</button>'
        )

    mode_bar = (
        '<div class="fidelity-controls" style="padding:0.35rem 0.5rem">'
        + _tab("Chunks", "chunks")
        + _tab("Conversations", "conversations")
        + "</div>"
    )

    # Try semantic search if embeddings are available
    try:
        from siftd.api.search import aggregate_by_conversation, hybrid_search

        results = hybrid_search(q, db_path=db_path, limit=30, fts5_passthrough=True)
        if results:
            if mode == "conversations":
                convs = aggregate_by_conversation(results, limit=20)
                conv_hits = [
                    {
                        "conversation_id": c.conversation_id,
                        "max_score": c.max_score,
                        "mean_score": c.mean_score,
                        "chunk_count": c.chunk_count,
                        "_workspace": c.workspace_path or "",
                        "_started_at": c.started_at or "",
                    }
                    for c in convs
                ]
                html = fmt.render_search(conv_hits, _fidelity(), **ctx)
                return _html_response(mode_bar + html)

            # chunks mode (default)
            hits = [
                {
                    "conversation_id": r.conversation_id,
                    "score": r.score,
                    "text": r.text,
                    "chunk_type": r.chunk_type,
                    "_workspace": r.workspace_path or "",
                    "_started_at": r.started_at or "",
                }
                for r in results[:20]
            ]
            html = fmt.render_search(hits, _fidelity(), **ctx)
            return _html_response(mode_bar + html)
    except Exception:
        pass  # Embeddings not installed or DB missing — fall through to FTS

    # FTS5 fallback (always available)
    from siftd.api.conversations import list_conversations

    rows = list_conversations(db_path=db_path, search=q, limit=20)
    if rows:
        html = fmt.render_list(rows, _fidelity(), **ctx)
        return _html_response(mode_bar + html)

    return _html_response(mode_bar + f'<p class="empty">No results for: {q}</p>')


# ---------------------------------------------------------------------------
# Live session endpoints — peek/follow
# ---------------------------------------------------------------------------


@get("/ui/peek", opt={"no_auth": True})
async def ui_peek() -> Response:
    """List active sessions as HTML — the entry point for follow mode."""
    from html import escape

    from siftd.api.peek import list_active_sessions

    sessions = list_active_sessions(include_inactive=False, limit=30)

    if not sessions:
        return _html_response('<p class="empty">No active sessions</p>')

    parts: list[str] = ['<table class="conversation-list">']
    parts.append(
        "<thead><tr>"
        '<th class="identifier">Session</th>'
        '<th class="workspace">Workspace</th>'
        '<th class="model">Model</th>'
        '<th class="metric">Exchanges</th>'
        '<th class="adapter">Adapter</th>'
        "</tr></thead><tbody>"
    )
    for s in sessions:
        ws = s.workspace_name or ""
        if s.branch:
            ws = f"{ws} [{s.branch}]" if ws else f"[{s.branch}]"
        parts.append(
            f'<tr hx-get="/ui/follow?sid={escape(s.session_id)}"'
            f' hx-target="#detail" hx-swap="innerHTML"'
            f' hx-push-url="/ui?follow={escape(s.session_id)}">'
            f'<td class="identifier">{escape(s.session_id[:8])}</td>'
            f'<td class="workspace">{escape(ws)}</td>'
            f'<td class="model">{escape(s.model or "")}</td>'
            f'<td class="metric">{s.exchange_count}</td>'
            f'<td class="adapter">{escape(s.adapter_name or "")}</td>'
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return _html_response("\n".join(parts))


@get("/ui/follow", opt={"no_auth": True})
async def ui_follow(
    sid: str = Parameter(query="sid", default=""),
    poll: bool = Parameter(query="poll", default=False),
) -> Response:
    """Follow a live session — returns the latest exchanges as HTML.

    When poll=true, returns just the exchanges (for htmx polling swap).
    On first load (poll=false), returns the session header + exchanges
    with a polling div that auto-refreshes.
    """
    from html import escape

    from siftd.api.peek import AmbiguousSessionError, find_session_file
    from siftd.output.format_registry import get_format

    if not sid:
        return _html_response('<p class="empty">No session ID</p>')

    try:
        path = find_session_file(sid)
    except AmbiguousSessionError:
        return _html_response(f'<p class="empty">Ambiguous session ID: {escape(sid[:12])}</p>')
    if path is None:
        return _html_response(f'<p class="empty">Session not found: {escape(sid[:12])}</p>')

    from siftd.api.peek import read_session_detail

    detail = read_session_detail(path, last_n=10, include_thinking=True)
    if detail is None:
        return _html_response(f'<p class="empty">Cannot read session: {escape(sid[:12])}</p>')

    fmt = get_format("html")
    fidelity = _fidelity(depth=2, chars=0, tools=True, thinking=True)
    tc = _tool_chars(fidelity)

    exchanges_html = fmt.render_detail(
        detail.exchanges,
        fidelity,
        no_header=True,
        tool_chars=tc,
        detail_base="/ui/query",
        shell_base="/ui",
    )

    if poll:
        return _html_response(exchanges_html)

    # First load: session header + polling wrapper
    info = detail.info
    ws = info.workspace_name or ""
    if info.branch:
        ws = f"{ws} [{info.branch}]" if ws else f"[{info.branch}]"

    header = (
        f'<header class="conversation-header">'
        f'<h2 class="identifier">{escape(info.session_id[:12])}'
        f' <span class="adapter">[{escape(info.adapter_name or "peek")}]</span></h2>'
        f'<div class="meta">'
        f'<span class="workspace">{escape(ws)}</span>'
        f'<span class="model">{escape(info.model or "")}</span>'
        f'<span class="metric">{info.exchange_count} exchanges</span>'
        f'</div></header>'
    )

    poll_url = f"/ui/follow?sid={escape(sid)}&poll=true"
    body = (
        f'<article class="conversation-detail follow-mode">'
        f'{header}'
        f'<div id="follow-content"'
        f' hx-get="{poll_url}" hx-trigger="every 2s"'
        f' hx-swap="innerHTML">'
        f'{exchanges_html}'
        f'</div>'
        f'</article>'
    )
    return _html_response(body)


# ---------------------------------------------------------------------------
# Stats dashboard
# ---------------------------------------------------------------------------


@get("/ui/stats", opt={"no_auth": True})
async def ui_stats(db_path: Path) -> Response:
    """Render cost/token dashboard as HTML fragment."""
    from html import escape

    from siftd.api.conversations import list_conversations
    from siftd.api.stats import get_stats
    from siftd.output.common import fmt_tokens, fmt_workspace

    parts: list[str] = ['<article class="stats-dashboard">']
    parts.append("<h2>Stats</h2>")

    # Overall stats from get_stats()
    try:
        stats = get_stats(db_path=db_path)
    except Exception:
        return _html_response('<p class="empty">No data available</p>')

    # Summary cards
    parts.append('<div class="stats-grid">')
    parts.append(
        f'<div class="stat-card">'
        f'<div class="stat-value">{stats.counts.conversations:,}</div>'
        f'<div class="stat-label">Conversations</div></div>'
    )
    parts.append(
        f'<div class="stat-card">'
        f'<div class="stat-value">{stats.counts.responses:,}</div>'
        f'<div class="stat-label">Responses</div></div>'
    )
    parts.append(
        f'<div class="stat-card">'
        f'<div class="stat-value">{stats.token_coverage.pct_with_tokens:.0f}%</div>'
        f'<div class="stat-label">Token coverage</div></div>'
    )
    if stats.activity_window[0]:
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">{escape(stats.activity_window[0][:10])}</div>'
            f'<div class="stat-label">First activity</div></div>'
        )
    if stats.activity_window[1]:
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">{escape(stats.activity_window[1][:10])}</div>'
            f'<div class="stat-label">Last activity</div></div>'
        )
    parts.append("</div>")

    # Cost/tokens by recent conversations (last 100)
    rows = list_conversations(db_path=db_path, limit=100)
    if rows:
        total_tokens = sum(r.total_tokens for r in rows)
        total_cost = sum(r.cost or 0 for r in rows)

        parts.append('<div class="stats-grid">')
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">{fmt_tokens(total_tokens)}</div>'
            f'<div class="stat-label">Tokens (last {len(rows)})</div></div>'
        )
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">${total_cost:.2f}</div>'
            f'<div class="stat-label">Cost (last {len(rows)})</div></div>'
        )
        if total_cost > 0:
            avg_cost = total_cost / len(rows)
            parts.append(
                f'<div class="stat-card">'
                f'<div class="stat-value">${avg_cost:.4f}</div>'
                f'<div class="stat-label">Avg cost/conversation</div></div>'
            )
        parts.append("</div>")

        # By model
        model_tokens: dict[str, int] = {}
        model_cost: dict[str, float] = {}
        model_count: dict[str, int] = {}
        for r in rows:
            m = r.model or "unknown"
            model_tokens[m] = model_tokens.get(m, 0) + r.total_tokens
            model_cost[m] = model_cost.get(m, 0) + (r.cost or 0)
            model_count[m] = model_count.get(m, 0) + 1

        parts.append("<h3>By model</h3>")
        parts.append('<table class="conversation-list">')
        parts.append(
            "<thead><tr>"
            "<th>Model</th><th>Conversations</th>"
            "<th>Tokens</th><th>Cost</th>"
            "</tr></thead><tbody>"
        )
        for m in sorted(model_tokens, key=lambda k: model_cost[k], reverse=True):
            parts.append(
                f"<tr>"
                f'<td class="model">{escape(m)}</td>'
                f'<td class="metric">{model_count[m]}</td>'
                f'<td class="metric">{fmt_tokens(model_tokens[m])}</td>'
                f'<td class="metric">${model_cost[m]:.4f}</td>'
                f"</tr>"
            )
        parts.append("</tbody></table>")

        # By workspace
        ws_tokens: dict[str, int] = {}
        ws_cost: dict[str, float] = {}
        ws_count: dict[str, int] = {}
        for r in rows:
            w = fmt_workspace(r.workspace_path)
            ws_tokens[w] = ws_tokens.get(w, 0) + r.total_tokens
            ws_cost[w] = ws_cost.get(w, 0) + (r.cost or 0)
            ws_count[w] = ws_count.get(w, 0) + 1

        parts.append("<h3>By workspace</h3>")
        parts.append('<table class="conversation-list">')
        parts.append(
            "<thead><tr>"
            "<th>Workspace</th><th>Conversations</th>"
            "<th>Tokens</th><th>Cost</th>"
            "</tr></thead><tbody>"
        )
        for w in sorted(ws_tokens, key=lambda k: ws_cost[k], reverse=True):
            parts.append(
                f"<tr>"
                f'<td class="workspace">{escape(w)}</td>'
                f'<td class="metric">{ws_count[w]}</td>'
                f'<td class="metric">{fmt_tokens(ws_tokens[w])}</td>'
                f'<td class="metric">${ws_cost[w]:.4f}</td>'
                f"</tr>"
            )
        parts.append("</tbody></table>")

    # Top tools
    if stats.top_tools:
        parts.append("<h3>Top tools</h3>")
        parts.append('<table class="conversation-list">')
        parts.append(
            "<thead><tr><th>Tool</th><th>Calls</th></tr></thead><tbody>"
        )
        for t in stats.top_tools[:15]:
            parts.append(
                f'<tr><td class="tool-name">{escape(t.name)}</td>'
                f'<td class="metric">{t.usage_count:,}</td></tr>'
            )
        parts.append("</tbody></table>")

    parts.append("</article>")
    return _html_response("\n".join(parts))
