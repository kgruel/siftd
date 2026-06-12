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

_NAV_ITEMS: tuple[tuple[str, str, str, str, str], ...] = (
    # (view_id, number, name, descriptor, mount_url)
    ("sessions", "01", "Sessions", "live · ingested", "/view/sessions"),
    ("search", "02", "Search", "query · facets", "/find"),
    ("transcript", "03", "Transcript", "folio · full", "/folio"),
    ("tags", "04", "Tags", "pinned · tree", "/view/tags"),
    ("workspaces", "05", "Workspaces", "explorer", "/view/workspaces"),
    ("stats", "06", "Stats", "tokens · cost", "/dashboard"),
)
_VIEW_TITLES = {vid: name for vid, _n, name, _d, _u in _NAV_ITEMS}


def _stub(view: str, title: str, hint: str = "coming in a later slice") -> str:
    """A neutral placeholder for a view not yet authored in this slice.

    Carries the same ``data-view/title/kick`` head metadata as a real fragment
    so ``enhance.js`` updates the chrome head + active nav identically.
    """
    from html import escape

    return (
        f'<div class="stub" data-view="{escape(view)}" data-title="{escape(title)}"'
        f' data-kick="{escape(hint)}">'
        f'<span class="stub__mark"></span>'
        f'<p>{escape(title)}</p>'
        f'<p class="stub__hint">{escape(hint)}</p></div>'
    )


def _siftd_version() -> str:
    try:
        from importlib.metadata import version

        return "v" + version("siftd")
    except Exception:
        return ""


def _shell_footer(db_path: Path, *, with_counts: bool) -> dict:
    """Rail-foot summary. Counts are corpus-wide, so they're only computed when
    auth is disabled — the shell route is ``no_auth`` and would otherwise leak
    global totals to an unauthenticated load."""
    foot: dict = {"version": _siftd_version()}
    if not with_counts:
        return foot
    try:
        from siftd.api.stats import get_stats

        stats = get_stats(db_path=db_path)
        foot["conversations"] = f"{stats.counts.conversations:,}"
    except Exception:
        pass
    try:
        if db_path.exists():
            foot["on_disk"] = f"{db_path.stat().st_size / 1_000_000:.1f} MB"
    except Exception:
        pass
    return foot


def _page_shell(
    *,
    conv_id: str | None = None,
    search_q: str | None = None,
    follow_sid: str | None = None,
    footer: dict | None = None,
) -> str:
    """Build the Swiss page shell (left rail + surface) with deep-link state.

    The rail mounts one of six views into ``#main`` via htmx. Only Transcript
    (the folio) is live in this slice; the others mount a ``.stub``. Deep links
    remap to a view: ``?id=`` → Transcript, ``?q=`` → Search, ``?follow=`` →
    Sessions (the latter two land on their stub until those slices ship).
    """
    from html import escape as esc
    from urllib.parse import quote as urlquote

    if conv_id:
        active, main_url = "transcript", f"/folio?id={urlquote(conv_id)}"
    elif search_q:
        active, main_url = "search", f"/find?q={urlquote(search_q)}"
    elif follow_sid:
        active, main_url = "sessions", "/view/sessions"
    else:
        active, main_url = "transcript", "/folio"

    nav_parts: list[str] = []
    for vid, num, name, ds, url in _NAV_ITEMS:
        cur = ' aria-current="page"' if vid == active else ""
        # Only the live view pushes a clean URL; stubs leave the address bar
        # untouched (their deep-link contract returns with their slice).
        push_attr = ' hx-push-url="/"' if vid == "transcript" else ""
        nav_parts.append(
            f'<a data-view="{vid}"{cur} hx-get="{esc(url)}" hx-target="#main"'
            f' hx-swap="innerHTML"{push_attr}>'
            f'<span class="n">{num}</span>'
            f'<span><span class="nm">{esc(name)}</span>'
            f'<span class="ds">{esc(ds)}</span></span></a>'
        )
    nav = "".join(nav_parts)

    foot = footer or {}
    foot_rows = ""
    for label, value in (
        ("Conversations", foot.get("conversations")),
        ("On disk", foot.get("on_disk")),
    ):
        if value:
            foot_rows += (
                f'<div class="row2"><span>{esc(label)}</span>'
                f"<b>{esc(str(value))}</b></div>"
            )
    version = foot.get("version") or ""
    ver_html = f'<div class="ver">{esc(version)}</div>' if version else ""

    init_title = _VIEW_TITLES.get(active, "siftd")

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>siftd</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="/static/vendor/htmx.min.js"></script>
<link href="/static/vendor/prism/prism-tomorrow.min.css" rel="stylesheet">
<link rel="stylesheet" href="/static/siftd.css">
</head>
<body data-theme="swiss" data-tone="light">
<div class="chrome chrome--swiss">
  <aside class="sw-rail">
    <div class="sw-brand"><b>siftd</b><i></i></div>
    <nav class="sw-nav">{nav}</nav>
    <div class="sw-foot">
      {foot_rows}
      <button class="sw-tone" type="button" data-tone-toggle>Dark</button>
      {ver_html}
    </div>
  </aside>
  <div class="sw-surface">
    <header class="sw-head">
      <h1 id="sw-title">{esc(init_title)}</h1>
      <span class="ct" id="sw-count"></span>
      <span class="kick" id="sw-kick"></span>
    </header>
    <main class="content" id="main" hx-get="{esc(main_url)}" hx-trigger="load" hx-swap="innerHTML"></main>
  </div>
</div>
<script src="/static/vendor/prism/prism-core.min.js"></script>
<script src="/static/vendor/prism/autoloader.min.js" data-autoloader-path="/static/vendor/prism/components/"></script>
<script src="/static/enhance.js"></script>
<script src="/static/auth.js"></script>
</body>
</html>"""


@get("/", opt={"no_auth": True})
async def ui_shell(
    db_path: Path,
    auth_config: dict | None = None,
    id: str | None = Parameter(query="id", default=None),
    q: str | None = Parameter(query="q", default=None),
    follow: str | None = Parameter(query="follow", default=None),
) -> Response:
    """Serve the Swiss page shell — the single full-page HTML response.

    Accepts optional params for deep-linkable URLs:
        ?id=     — open a conversation (Transcript folio)
        ?q=      — pre-populate Search
        ?follow= — follow a live session (Sessions)
    """
    footer = _shell_footer(db_path, with_counts=auth_config is None)
    return Response(
        content=_page_shell(conv_id=id, search_q=q, follow_sid=follow, footer=footer),
        media_type="text/html",
    )


@get("/folio")
async def ui_folio(
    request: Request,
    db_path: Path,
    id: str | None = Parameter(query="id", default=None),
) -> Response:
    """Render the Swiss transcript folio for one conversation.

    With ``?id=`` renders that conversation; without, the most recent one so
    the view is never empty. Owner-scoped via the effective identity.
    """
    from siftd.api.conversations import (
        AmbiguousPrefix,
        get_conversation,
        list_conversations,
    )
    from siftd.output.format_registry import get_format

    owner = _effective_owner(request, None)
    fmt = get_format("html")
    # depth=3 fetches the rollup's canonical cost (+ tags) for the ledger foot.
    # shows() is membership-based, so tools/thinking stay unfetched — the bump
    # only flips the cost/tag gates, not the body's prose-only rendering.
    fidelity = _fidelity(depth=3, chars=0)

    conv_id = id
    if not conv_id:
        latest = list_conversations(
            fidelity=_fidelity(depth=1), db_path=db_path, n=1, owner=owner,
        )
        if not latest:
            return _html_response(_stub("transcript", "Transcript", "no conversations yet"))
        conv_id = latest[0].id

    try:
        detail = get_conversation(conv_id, fidelity=fidelity, db_path=db_path, owner=owner)
    except AmbiguousPrefix as exc:
        return _html_response(
            _stub("transcript", "Transcript", f"ambiguous id — {exc.total} matches")
        )
    if detail is None:
        return _html_response(_stub("transcript", "Transcript", f"not found: {conv_id[:12]}"))
    return _html_response(fmt.render_folio(
        detail, fidelity,
        interactive_tags=True,
        tag_action_url="/tag",
        tag_suggest_url="/tags/suggest",
        export_base_url="/export",
    ))


@get("/dashboard")
async def ui_dashboard(request: Request, db_path: Path) -> Response:
    """Render the Swiss 'Stats' dashboard — aggregate token/cost over the corpus.

    Owner-scoped: every read takes the effective identity, so a tenant sees only
    their own totals. This is the highest-IDOR-risk view in the set — the folio
    owner-checks one conversation inside ``get_conversation``, but the dashboard
    *sums across all of them*, so the ``owner=`` on each aggregate read is the
    only thing scoping it (``owner=None`` would be a cross-tenant total).

    The rollup (``usage_by_conv_model``) is guaranteed present: ``open_database``
    migrates any served DB to the current schema (v11+) on open, or raises if it
    cannot. The pre-rollup (v8) degrade path that lived here was scaffolding for
    the 0.9.0 migration and dissolved once that migration landed.
    """
    from siftd.api.stats import (
        get_cost_coverage,
        get_stats,
        get_usage_by_model,
        get_usage_by_workspace,
        get_usage_summary,
    )
    from siftd.output.html_fmt import render_dashboard

    owner = _effective_owner(request, None)
    return _html_response(
        render_dashboard(
            usage=get_usage_summary(db_path=db_path, owner=owner),
            by_model=get_usage_by_model(db_path=db_path, owner=owner),
            by_workspace=get_usage_by_workspace(db_path=db_path, owner=owner),
            coverage=get_cost_coverage(db_path=db_path, owner=owner),
            stats=get_stats(db_path=db_path, owner=owner),
            owner=owner,
        )
    )


@get("/view/{name:str}")
async def ui_view_stub(
    name: str,
    q: str | None = Parameter(query="q", default=None),
) -> Response:
    """Placeholder for a view not yet authored in this slice (Swiss Phase B)."""
    title = _VIEW_TITLES.get(name, name.replace("-", " ").title())
    hint = "coming in a later slice"
    if name == "search" and q:
        hint = f'search "{q[:40]}" — coming in a later slice'
    return _html_response(_stub(name, title, hint))


@get("/find")
async def ui_find(
    q: str | None = Parameter(query="q", default=None),
) -> Response:
    """The Swiss 'Find' view: one surface unifying metadata facets + content search.

    A host fragment, not a renderer — it composes the two existing reads it has
    no new logic over: ``/meta`` (the control strip: search box + filter
    dropdowns) loads into ``#filters``, ``/query`` (the conversation list) loads
    into ``#list``. Every control in the strip targets ``#list`` and includes
    ``#filters``, so keyword + facets resolve as one ``list_conversations`` call.

    A deep-linked ``?q=`` pre-fills the box and the initial list (FTS5-sanitized
    in ``ui_query``). Content search here is keyword/FTS only; semantic ranking
    (CLI ``search``) is a deliberate follow-up that swaps in behind this same box.
    """
    from urllib.parse import quote as urlquote

    term = (q or "").strip()
    qs = f"?search={urlquote(term)}" if term else ""
    return _html_response(
        '<div class="find" data-view="search" data-title="Search"'
        ' data-kick="query · facets">'
        f'<div class="find__filters" id="filters" role="search"'
        f' hx-get="/meta{qs}" hx-trigger="load"></div>'
        f'<div class="find__list" id="list"'
        f' hx-get="/query{qs}" hx-trigger="load"></div>'
        "</div>"
    )


@get("/auth/config", opt={"no_auth": True}, sync_to_thread=False)
def ui_auth_config(auth_config: dict | None) -> dict:
    """Advertise the PUBLIC OIDC params the browser needs for auth-code+PKCE login.

    The browser is a client like the CLI, but — unlike the CLI — it cannot read
    local ``[auth]`` config, so the server hands it the (non-secret) issuer +
    public client_id + scopes here. The browser discovers the authorize/token
    endpoints itself from ``issuer/.well-known/openid-configuration`` and runs the
    flow client-side; the server stays a pure token *validator* and never sees
    this acquisition. ``serve.auth`` is unchanged.

    Returns ``{"enabled": false}`` unless the server is in issuer (JWKS) mode AND
    ``serve.auth.browser_client_id`` is set — i.e. static_token / introspection
    deployments fall back to the manual token-paste UI.
    """
    cfg = auth_config or {}
    issuer = cfg.get("issuer")
    client_id = cfg.get("browser_client_id")
    if not issuer or not client_id:
        return {"enabled": False}
    scopes = cfg.get("browser_scopes") or ["openid", "profile", "email", "offline_access"]
    if isinstance(scopes, str):  # tolerate a space-delimited string
        scopes = scopes.split()
    return {
        "enabled": True,
        "issuer": issuer.rstrip("/"),
        "client_id": client_id,
        "scope": " ".join(scopes),
    }


# ---------------------------------------------------------------------------
# Fragment endpoints — htmx swap targets
# ---------------------------------------------------------------------------


@get("/meta")
async def ui_meta(
    request: Request,
    db_path: Path,
    search: str | None = Parameter(query="search", default=None),
) -> Response:
    """Return the find control strip: a content-search box + filter dropdowns.

    ``search`` pre-fills the box for deep-linked ``?q=`` loads. Every control
    targets ``#list`` and includes ``#filters``, so the box and the dropdowns
    compose into one query — keyword + metadata facets in a single request.
    """
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

    # Content search: untrusted text, debounced; ui_query sanitizes it for FTS5.
    search_box = (
        '<input type="search" name="search" placeholder="Search content…"'
        f' value="{escape(search or "")}"'
        ' hx-get="/query" hx-target="#list"'
        ' hx-trigger="keyup changed delay:350ms, search"'
        ' hx-include="#filters" class="filter-input filter-input--q">'
    )

    parts = [
        search_box,
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
) -> Response:
    """List conversations as an HTML fragment (rows link to the folio).

    List-only: the old ``?id=`` detail mode is gone — the folio is the single
    detail surface, and rows mount it into ``#main``.
    """
    from siftd.api.conversations import list_conversations
    from siftd.api.dispatch import Operation, dispatch
    from siftd.output.format_registry import get_format

    # Normalize empty strings to None (htmx sends "" for blank inputs)
    workspace = workspace or None
    model = model or None
    search = search or None
    since = since or None
    before = before or None
    owner = _effective_owner(request, owner or None)
    tag = [t for t in (tag or []) if t] or None

    # FTS5 safety: the find box is untrusted text fed straight into a MATCH
    # clause. Tokenize+quote it so bare punctuation (", :, *, (, or the word
    # AND) can't raise an fts5 syntax error → a 500 in the user's face.
    # Punctuation-only input sanitizes to None → the search filter is dropped.
    # The api keeps its raw-FTS contract; sanitization lives at the serve edge.
    if search:
        from siftd.api import sanitize_fts5_query

        search = sanitize_fts5_query(search).fts_query

    fmt = get_format("html")
    ctx = {"detail_base": "/folio", "shell_base": "/"}

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
    ctx = {"detail_base": "/folio", "shell_base": "/", "query": q, "mode": mode}

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
# Tag operations
# ---------------------------------------------------------------------------


@post("/tag")
async def ui_tag(request: Request, db_path: Path) -> Response:
    """Apply or remove a tag, return updated tag section fragment."""
    from siftd.serve.auth import require_write

    require_write(request)

    from siftd.api import record_audit_event
    from siftd.api.tags import modify_conversation_tag
    from siftd.output.html_fmt import render_tag_section
    from siftd.serve.routes import _actor_identity, _client_ip

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
    record_audit_event(
        db_path=db_path,
        actor=_actor_identity(request),
        action=f"tag.{action}",
        target_type="conversation",
        target=conv_id,
        detail=tag_name,
        source_ip=_client_ip(request),
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
