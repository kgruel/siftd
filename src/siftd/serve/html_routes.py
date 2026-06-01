"""HTML route handlers for htmx-driven web UI.

Returns HTML fragments for htmx swaps. The page shell (GET /) serves the
single full-page response; everything else is a fragment.

Content negotiation: routes check for HX-Request header or Accept: text/html.
JSON API routes in routes.py remain untouched.
"""

from __future__ import annotations

from pathlib import Path

from litestar import Request, get, post
from litestar.params import Parameter
from litestar.response import Response

from siftd.output._id_format import short_id
from siftd.serve.routes import _effective_owner


def _html_response(content: str) -> Response:
    return Response(content=content, media_type="text/html")


def _hx_detail(detail_base: str, conv_id: str, shell_base: str = "") -> str:
    """Build htmx attributes for a row that navigates to conversation detail."""
    from html import escape
    if not detail_base:
        return ""
    push = f' hx-push-url="{escape(shell_base)}?id={escape(conv_id)}"' if shell_base else ""
    return (
        f' hx-get="{escape(detail_base)}?id={escape(conv_id)}"'
        f' hx-target="#detail" hx-swap="innerHTML"'
        f'{push}'
    )


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
    from urllib.parse import quote as urlquote

    # Search bar: pre-populate if search_q provided
    search_val = f' value="{esc(search_q)}"' if search_q else ""

    # List pane: search results, peek sessions, or conversation list
    if search_q:
        list_url = f"/search?q={urlquote(search_q)}"
    elif follow_sid:
        list_url = "/peek"
    else:
        list_url = "/query"

    # Detail pane: auto-load conversation, follow session, or empty
    if follow_sid:
        detail_attr = f' hx-get="/follow?sid={esc(follow_sid)}" hx-trigger="load" hx-swap="innerHTML"'
        detail_content = '<p class="empty">Loading session...</p>'
    elif conv_id:
        detail_attr = f' hx-get="/query?id={esc(conv_id)}" hx-trigger="load" hx-swap="innerHTML"'
        detail_content = '<p class="empty">Loading...</p>'
    else:
        detail_attr = ""
        detail_content = '<div class="empty-state"><div class="empty-icon">&#x2139;</div><p>Select a conversation from the list</p><p class="empty-hint">or search with the bar above</p></div>'

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>siftd</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<link href="https://unpkg.com/prismjs@1.30.0/themes/prism-tomorrow.min.css" rel="stylesheet">
<link rel="stylesheet" href="/static/siftd.css">
</head>
<body>
<nav>
  <span class="brand">siftd</span>
  <input type="search" name="q" placeholder="Search..."{search_val}
    hx-get="/search" hx-target="#list" hx-trigger="keyup changed delay:300ms"
    hx-include="this">
  <a href="/" hx-get="/query" hx-target="#list" hx-push-url="/"
    hx-on::before-request="document.querySelectorAll('#filters select,#filters input').forEach(e=>e.value='')">Recent</a>
  <a href="#" hx-get="/peek" hx-target="#list">Live</a>
  <a href="#" hx-get="/stats" hx-target="#detail" hx-swap="innerHTML">Stats</a>
  <button class="density-toggle" onclick="document.body.classList.toggle('compact')" title="Toggle compact mode">Compact</button>
</nav>
<main>
  <div id="list-pane">
    <div id="filters" hx-get="/meta" hx-trigger="load" hx-swap="innerHTML">
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
<script src="https://unpkg.com/prismjs@1.30.0/components/prism-core.min.js"></script>
<script src="https://unpkg.com/prismjs@1.30.0/plugins/autoloader/prism-autoloader.min.js"></script>
<script>
document.body.addEventListener('htmx:afterSettle', function() {{
  if (window.Prism) Prism.highlightAll();
}});
</script>
<script>
(function() {{
  var token = sessionStorage.getItem('siftd_token');
  if (token) {{
    document.body.setAttribute('hx-headers',
      JSON.stringify({{"Authorization": "Bearer " + token}}));
    htmx.process(document.body);
    var btn = document.createElement('button');
    btn.className = 'nav-auth-btn';
    btn.textContent = 'Sign out';
    btn.onclick = function() {{
      sessionStorage.removeItem('siftd_token');
      location.reload();
    }};
    document.querySelector('nav').appendChild(btn);
  }}

  document.body.addEventListener('htmx:responseError', function(e) {{
    if (e.detail.xhr.status !== 401) return;
    if (document.getElementById('siftd-login')) return;
    document.getElementById('list').innerHTML =
      '<div id="siftd-login">' +
      '<div class="login-icon">&#x1f512;</div>' +
      '<h3>Sign in to siftd</h3>' +
      '<p>Enter a bearer token to authenticate.</p>' +
      '<form onsubmit="return siftdLogin(this)">' +
      '<input type="password" name="token" placeholder="Bearer token" autofocus ' +
      'class="login-input">' +
      '<button type="submit" class="login-btn">Sign in</button>' +
      '</form></div>';
    document.getElementById('detail').innerHTML = '';
  }});

  window.siftdLogin = function(form) {{
    sessionStorage.setItem('siftd_token', form.token.value);
    location.reload();
    return false;
  }};
}})();
</script>
</body>
</html>"""


@get("/", opt={"no_auth": True})
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


@get("/meta")
async def ui_meta(request: Request, db_path: Path) -> Response:
    """Return filter dropdowns populated from the database."""
    from html import escape

    from siftd.api.stats import get_stats, list_workspaces
    from siftd.api.tags import list_tags

    owner = _effective_owner(request, None)

    stats = None
    try:
        stats = get_stats(db_path=db_path, owner=owner)
    except Exception:
        pass

    ws_rows: list = []
    try:
        ws_rows = list_workspaces(db_path=db_path, n=200, owner=owner)
    except Exception:
        pass

    tag_names: list[str] = []
    try:
        tag_names = [t.name for t in list_tags(db_path=db_path, owner=owner)]
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
            f' hx-get="/query" hx-target="#list" hx-trigger="change"'
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
            f' hx-get="/query" hx-target="#list" hx-trigger="change"'
            f' hx-include="#filters">'
        )

    parts = [
        _select("workspace", "workspaces", ws_opts),
        _select("model", "models", model_opts),
        _select("tag", "tags", tag_opts),
        (
            '<input type="text" name="owner" placeholder="Owner"'
            ' hx-get="/query" hx-target="#list" hx-trigger="change"'
            ' hx-include="#filters" class="filter-input">'
        ),
        _date("since", "Since"),
        _date("before", "Before"),
    ]
    return _html_response("".join(parts))


@get("/query")
async def ui_query(
    request: Request,
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    search: str | None = Parameter(query="search", default=None),
    owner: str | None = Parameter(query="owner", default=None),
    n: int = Parameter(query="n", default=50),
    id: str | None = Parameter(query="id", default=None),
    # Fidelity controls (detail view)
    thinking: bool = Parameter(query="thinking", default=False),
    full: bool = Parameter(query="full", default=False),
    brief: bool = Parameter(query="brief", default=False),
) -> Response:
    """List or detail conversations as HTML fragments."""
    from html import escape

    from siftd.api.conversations import AmbiguousPrefix, get_conversation, list_conversations
    from siftd.api.dispatch import Operation, dispatch, execute
    from siftd.output.format_registry import get_format

    # Normalize empty strings to None (htmx sends "" for blank inputs)
    workspace = workspace or None
    model = model or None
    search = search or None
    since = since or None
    before = before or None
    owner = _effective_owner(request, owner or None)
    tag = [t for t in (tag or []) if t] or None

    fmt = get_format("html")
    ctx = {"detail_base": "/query", "shell_base": "/"}

    if id is not None:
        # Build fidelity from query params — same logic as CLI flags
        if full:
            fidelity = _fidelity(depth=3, chars=0, tools=True, thinking=True)
        elif brief:
            fidelity = _fidelity(depth=0, chars=80)
        else:
            fidelity = _fidelity(depth=2, chars=0, thinking=thinking)

        op = Operation(
            path=f"/api/v1/conversations/{id}",
            method="GET",
            fn=get_conversation,
            params={
                "id": id,
                "fidelity": fidelity,
                "db_path": db_path,
                "owner": owner,
            },
            render_method="detail",
            fidelity=fidelity,
            db=db_path,
            render_context={
                **ctx,
                "tool_chars": _tool_chars(fidelity),
                "controls": {"id": id, "thinking": thinking,
                             "full": full, "brief": brief},
                "interactive_tags": True,
                "tag_action_url": "/tag",
                "tag_suggest_url": "/tags/suggest",
                "export_base_url": "/export",
            },
        )
        try:
            detail = execute(op)
        except AmbiguousPrefix as exc:
            return _html_response(
                f'<p class="error">Ambiguous prefix {escape(exc.prefix)!r} — matched {exc.total} conversations.'
                " Use a longer prefix or full ID.</p>"
            )
        if detail is None:
            return _html_response(f'<p class="empty">Not found: {id[:12]}</p>')
        return _html_response(fmt.render_detail(detail, op.fidelity, **op.render_context))

    list_fidelity = _fidelity()
    op = Operation(
        path="/api/v1/conversations",
        method="GET",
        fn=list_conversations,
        params={
            "fidelity": list_fidelity,
            "db_path": db_path,
            "workspace": workspace,
            "model": model,
            "since": since,
            "before": before,
            "search": search,
            "tag": tag,
            "owner": owner,
            "n": n,
        },
        render_method="list",
        fidelity=list_fidelity,
        db=db_path,
        render_context=ctx,
    )
    return _html_response(dispatch(op, fmt=fmt))


@get("/search")
async def ui_search(
    request: Request,
    db_path: Path,
    q: str = Parameter(query="q", default=""),
    mode: str = Parameter(query="mode", default="chunks"),
) -> Response:
    """Search conversations, return HTML fragment.

    Tries semantic search (embeddings) first, falls back to FTS5.
    Modes: chunks (default), conversations (aggregated scores).
    """
    from html import escape

    from siftd.api.dispatch import Operation, execute
    from siftd.output.format_registry import get_format

    owner = _effective_owner(request, None)

    if not q.strip():
        return _html_response('<p class="empty">Type to search...</p>')

    fmt = get_format("html")
    ctx = {"detail_base": "/query", "shell_base": "/", "query": q, "mode": mode}

    # Mode toggle tabs
    def _tab(label: str, m: str) -> str:
        from urllib.parse import quote as urlquote

        active = " active" if m == mode else ""
        return (
            f'<button class="toggle{active}"'
            f' hx-get="/search?q={urlquote(q)}&mode={m}"'
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
        from siftd.api.search import aggregate_by_conversation, search_chunks

        op = Operation(
            path="/api/v1/search",
            method="GET",
            fn=search_chunks,
            params={"q": q, "db_path": db_path, "n": 30, "owner": owner},
            render_method="search",
            fidelity=_fidelity(),
            db=db_path,
            render_context=ctx,
        )
        results = execute(op)
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
            else:
                conv_hits = [
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
            html = fmt.render_search(conv_hits, op.fidelity, **op.render_context)
            return _html_response(mode_bar + html)
    except Exception:
        pass  # Embeddings not installed or DB missing — fall through to FTS

    # FTS5 fallback (always available)
    from siftd.api.conversations import list_conversations

    fts_fidelity = _fidelity()
    op = Operation(
        path="/api/v1/conversations",
        method="GET",
        fn=list_conversations,
        params={"fidelity": fts_fidelity, "db_path": db_path, "search": q, "n": 20, "owner": owner},
        render_method="list",
        fidelity=fts_fidelity,
        db=db_path,
        render_context=ctx,
    )
    rows = execute(op)
    if rows:
        html = fmt.render_list(rows, op.fidelity, **op.render_context)
        return _html_response(mode_bar + html)

    return _html_response(mode_bar + f'<p class="empty">No results for: {escape(q)}</p>')


# ---------------------------------------------------------------------------
# Live session endpoints — peek/follow
# ---------------------------------------------------------------------------


@get("/peek")
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
            f'<tr hx-get="/follow?sid={escape(s.session_id)}"'
            f' hx-target="#detail" hx-swap="innerHTML"'
            f' hx-push-url="/?follow={escape(s.session_id)}">'
            f'<td class="identifier">{escape(short_id(s.session_id))}</td>'
            f'<td class="workspace">{escape(ws)}</td>'
            f'<td class="model">{escape(s.model or "")}</td>'
            f'<td class="metric">{s.exchange_count}</td>'
            f'<td class="adapter">{escape(s.adapter_name or "")}</td>'
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return _html_response("\n".join(parts))


@get("/follow")
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
        detail_base="/query",
        shell_base="/",
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

    poll_url = f"/follow?sid={escape(sid)}&poll=true"
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


@get("/stats")
async def ui_stats(request: Request, db_path: Path) -> Response:
    """Render cost/token dashboard as HTML fragment."""
    from siftd.api.dispatch import Operation, execute
    from siftd.api.stats import (
        get_cost_coverage,
        get_stats,
        get_usage_by_model,
        get_usage_by_workspace,
        get_usage_summary,
    )
    from siftd.output.format_registry import get_format

    owner = _effective_owner(request, None)

    op = Operation(
        path="/api/v1/stats",
        method="GET",
        fn=get_stats,
        params={"db_path": db_path, "owner": owner},
        render_method="stats",
        fidelity=_fidelity(),
        db=db_path,
    )

    try:
        stats = execute(op)
    except Exception:
        return _html_response('<p class="empty">No data available</p>')

    # Gather supplementary data for render context
    usage = None
    cost_coverage = 0
    by_model: list = []
    by_workspace: list = []
    try:
        usage = get_usage_summary(db_path=db_path, owner=owner)
        cc = get_cost_coverage(db_path=db_path, owner=owner)
        cost_coverage = round(cc.pct_covered) if cc else 0
    except Exception:
        pass
    try:
        by_model = get_usage_by_model(db_path=db_path, owner=owner)
    except Exception:
        pass
    try:
        by_workspace = get_usage_by_workspace(db_path=db_path, owner=owner)
    except Exception:
        pass

    fmt = get_format("html")
    html = fmt.render_stats(
        stats, op.fidelity,
        usage=usage,
        cost_coverage_pct=cost_coverage,
        by_model=by_model,
        by_workspace=by_workspace,
    )
    return _html_response(html)


# ---------------------------------------------------------------------------
# Tag operations
# ---------------------------------------------------------------------------


@post("/tag")
async def ui_tag(request: Request, db_path: Path) -> Response:
    """Apply or remove a tag, return updated tag section fragment."""
    from siftd.serve.auth import require_write

    require_write(request)

    from siftd.api.tags import modify_conversation_tag
    from siftd.output.html_fmt import render_tag_section

    owner = _effective_owner(request, None)
    form = await request.form()
    action = str(form.get("action", "apply"))
    conv_id = str(form.get("id", ""))
    tag_name = str(form.get("tag", "")).strip()

    if not conv_id or not tag_name:
        return _html_response('<div class="tag-section">error: missing id or tag</div>')

    tags = modify_conversation_tag(
        conv_id, tag_name, action=action, db_path=db_path, owner=owner,
    )
    return _html_response(render_tag_section(
        conv_id, tags,
        tag_action_url="/tag", tag_suggest_url="/tags/suggest",
    ))


@get("/tags/suggest")
async def ui_tags_suggest(
    request: Request,
    db_path: Path,
    tag: str = Parameter(query="tag", default=""),
) -> Response:
    """Return datalist <option> elements for tag autocomplete."""
    from html import escape

    from siftd.api.tags import list_tags

    owner = _effective_owner(request, None)
    all_tags = list_tags(db_path=db_path, owner=owner)
    prefix = tag.lower()
    options = [
        f'<option value="{escape(t.name)}">'
        for t in all_tags
        if not prefix or t.name.lower().startswith(prefix)
    ]
    return _html_response("".join(options[:20]))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@get("/export")
async def ui_export(
    request: Request,
    db_path: Path,
    id: str = Parameter(query="id", default=""),
    format: str = Parameter(query="format", default="md"),
) -> Response:
    """Export a conversation as a downloadable file."""
    from html import escape

    from siftd.api.conversations import AmbiguousPrefix
    from siftd.api.dispatch import Operation, execute
    from siftd.api.export import export_document

    if not id:
        return _html_response('<p class="empty">No conversation ID specified</p>')

    op = Operation(
        path="/api/v1/export",
        method="GET",
        fn=export_document,
        params={
            "format": format,
            "id": [id],
            "fidelity": _fidelity(depth=3, chars=0, tools=True, thinking=True),
            "db_path": db_path,
            "owner": _effective_owner(request, None),
        },
        render_method="raw",
        fidelity=_fidelity(depth=3, chars=0, tools=True, thinking=True),
        db=db_path,
    )

    try:
        artifact = execute(op)
    except AmbiguousPrefix as exc:
        return _html_response(
            f'<p class="error">Ambiguous prefix {escape(exc.prefix)!r} — matched {exc.total} conversations.'
            " Use a longer prefix or full ID.</p>"
        )
    if artifact.count == 0:
        return _html_response(f'<p class="empty">Not found: {id[:12]}</p>')

    return Response(
        content=artifact.content.encode(),
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
        },
    )
