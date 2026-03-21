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
    hx-on::before-request="document.querySelectorAll('#filters select').forEach(s=>s.value='')"
    style="color:var(--accent);text-decoration:none">Recent</a>
  <a href="#" hx-get="/ui/peek" hx-target="#list"
    style="color:var(--accent);text-decoration:none">Live</a>
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

    parts = [
        _select("workspace", "workspaces", ws_opts),
        _select("model", "models", model_opts),
        _select("tag", "tags", tag_opts),
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
) -> Response:
    """Search conversations, return HTML fragment.

    Tries semantic search (embeddings) first, falls back to FTS5.
    """
    from siftd.output.format_registry import get_format

    if not q.strip():
        return _html_response('<p class="empty">Type to search...</p>')

    fmt = get_format("html")
    ctx = {"detail_base": "/ui/query", "shell_base": "/ui", "query": q}

    # Try semantic search if embeddings are available
    try:
        from siftd.api.search import hybrid_search

        results = hybrid_search(q, db_path=db_path, limit=20, fts5_passthrough=True)
        if results:
            hits = [
                {
                    "conversation_id": r.conversation_id,
                    "score": r.score,
                    "text": r.text,
                    "chunk_type": r.chunk_type,
                    "_workspace": r.workspace_path or "",
                    "_started_at": r.started_at or "",
                }
                for r in results
            ]
            html = fmt.render_search(hits, _fidelity(), **ctx)
            return _html_response(html)
    except Exception:
        pass  # Embeddings not installed or DB missing — fall through to FTS

    # FTS5 fallback (always available)
    from siftd.api.conversations import list_conversations

    rows = list_conversations(db_path=db_path, search=q, limit=20)
    if rows:
        html = fmt.render_list(rows, _fidelity(), **ctx)
        return _html_response(html)

    return _html_response(f'<p class="empty">No results for: {q}</p>')


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
