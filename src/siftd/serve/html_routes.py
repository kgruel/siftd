"""HTML route handlers for htmx-driven web UI.

Returns HTML fragments for htmx swaps. The page shell (GET /) serves the
single full-page response; everything else is a fragment.

Content negotiation: routes check for HX-Request header or Accept: text/html.
JSON API routes in routes.py remain untouched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from litestar import Request, get, post
from litestar.params import Parameter
from litestar.response import Redirect, Response

from siftd.output._id_format import short_id
from siftd.serve.routes import _effective_owner

# An event anchor is an events-table ULID; this charset also covers any adapter
# id we might anchor on. Validating here keeps a malformed/hostile ``?event=``
# out of the rendered ``data-event-id`` / ``data-scroll-to`` and the client-side
# selector entirely (defense in depth — the values are also escaped on emit).
_EVENT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _safe_event_id(value: str | None) -> str | None:
    """Return ``value`` if it is a safe anchor token, else ``None``."""
    if isinstance(value, str) and _EVENT_ID_RE.match(value):
        return value
    return None


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


def _asset_v(rel: str) -> str:
    """Cache-bust query for one of our own static assets, keyed on file mtime.

    The served file is unchanged — litestar's static router ignores the query —
    but the browser refetches when the byte content changes. Computed at render
    time, so a CSS/JS edit busts on the next page load with no server restart
    (the un-versioned ``<link>`` was why a stale cached siftd.css survived an
    edit). Vendored assets (htmx, prism) live in version-pinned dirs and are
    left alone.
    """
    try:
        mtime = int((Path(__file__).parent / "static" / rel).stat().st_mtime)
    except OSError:
        return ""
    return f"?v={mtime}"


# ---------------------------------------------------------------------------
# URL-as-state: the canonical /?view=<v>&<state> grammar
# ---------------------------------------------------------------------------
# The address bar is always a shell URL (``/?view=…``); fragment endpoints
# (``/folio``, ``/dashboard``, …) are internal mount targets the shell loads
# into ``#main`` via htmx. ``_resolve_view`` is the decoder (URL → mount);
# ``_canonical_url`` is the encoder (view+state → URL). A direct/non-htmx GET
# of a fragment redirects to its canonical URL (``_shell_redirect``), so a
# refresh / shared link / typed fragment URL always lands on the shell.

_VALID_VIEWS = frozenset(
    {"sessions", "search", "transcript", "tags", "workspaces", "stats"}
)


def _canonical_url(view: str, **state: str | None) -> str:
    """Encode a view + its state into the canonical shell URL ``/?view=…``.

    Empty/None state values are dropped, so a stateless view encodes to a clean
    ``/?view=<v>``. Ordering is stable (insertion order) for predictable,
    shareable links.
    """
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = [("view", view)]
    params.extend((k, v) for k, v in state.items() if v)
    return "/?" + urlencode(params)


def _is_htmx(request: Request) -> bool:
    """True when this GET is an htmx fragment fetch (the shell mounting ``#main``
    or an in-page swap), false for a direct browser navigation/refresh.

    Defensive: a request without usable headers (a direct ``handler.fn(...)``
    call in a unit test) can't be classified, so it is treated as htmx — i.e. it
    serves the fragment, never redirects. A real browser always sends headers, so
    the navigation/refresh case is never ambiguous in production.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return True
    return headers.get("HX-Request") == "true"


def _shell_redirect(request: Request, view: str, **state: str | None) -> Redirect | None:
    """A 303 to the canonical shell URL for a direct (non-htmx) GET of a fragment.

    Returns ``None`` when the request is an htmx fetch — the caller then serves
    the bare fragment as normal. This is the single point that dissolves the
    bare-fragment footgun: a typed/refreshed/shared fragment URL canonicalizes to
    ``/?view=…``, which the shell re-mounts (so the page is never an unstyled,
    chrome-less fragment).
    """
    if _is_htmx(request):
        return None
    return Redirect(_canonical_url(view, **state), status_code=303)


def _resolve_view(
    *,
    view: str | None,
    conv_id: str | None,
    folio_mode: str | None,
    folio_event: str | None,
    search_q: str | None,
    tag: str | None,
    workspace_id: str | None,
    follow_sid: str | None,
    model: str | None,
    sort: str | None,
    live_enabled: bool,
) -> tuple[str, str]:
    """Decode the shell's query params into ``(active_view, main_url)``.

    An explicit ``?view=`` wins; otherwise the view is inferred from a legacy
    presence-based deep link (``?id=`` → transcript, ``?q=``/``?tag=`` → search,
    ``?ws=`` → workspaces, ``?follow=`` → sessions) so older links and the
    ``_hx_detail`` drill-downs (which still push ``/?id=``/``/?tag=``/``/?ws=``)
    keep round-tripping. ``main_url`` is the fragment the shell's ``#main`` mounts.
    """
    from urllib.parse import quote as urlquote

    v = view if view in _VALID_VIEWS else None
    if v is None:
        if conv_id:
            v = "transcript"
        elif search_q or tag:
            v = "search"
        elif workspace_id:
            v = "workspaces"
        elif follow_sid:
            v = "sessions"
        else:
            v = "transcript"

    if v == "transcript":
        if not conv_id:
            return "transcript", "/folio"
        main = f"/folio?id={urlquote(conv_id)}"
        if folio_mode == "trace":
            main += "&mode=trace"
            # event is a trace-only target (nested under mode=trace), so a
            # reading mount never carries an unrenderable anchor.
            if folio_event:
                main += f"&event={urlquote(folio_event)}"
        return "transcript", main
    if v == "search":
        parts: list[str] = []
        if search_q:
            parts.append(f"q={urlquote(search_q)}")
        if tag:
            parts.append(f"tag={urlquote(tag)}")
        qs = ("?" + "&".join(parts)) if parts else ""
        return "search", f"/find{qs}"
    if v == "tags":
        return "tags", "/view/tags"
    if v == "workspaces":
        if workspace_id:
            return "workspaces", f"/workspace?ws={urlquote(workspace_id)}"
        if sort in _WS_SORTS:
            return "workspaces", f"/view/workspaces?sort={urlquote(sort)}"
        return "workspaces", "/view/workspaces"
    if v == "sessions":
        if follow_sid and live_enabled:
            return "sessions", f"/follow?sid={urlquote(follow_sid)}"
        return "sessions", "/view/sessions"
    if v == "stats":
        if model:
            return "stats", f"/dashboard?model={urlquote(model)}"
        return "stats", "/dashboard"
    return "transcript", "/folio"


def _page_shell(
    *,
    view: str | None = None,
    conv_id: str | None = None,
    folio_mode: str | None = None,
    folio_event: str | None = None,
    search_q: str | None = None,
    tag: str | None = None,
    follow_sid: str | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    sort: str | None = None,
    footer: dict | None = None,
    live_enabled: bool = False,
) -> str:
    """Build the Swiss page shell (left rail + surface) with deep-link state.

    The rail mounts one of six views into ``#main`` via htmx. Deep links remap
    to a view: ``?id=`` → Transcript folio, ``?q=`` → Search, ``?tag=`` → Find
    pre-filtered by that tag (the Tags view's drill-down target), ``?ws=`` → a
    workspace detail (the Workspaces view's drill-down target), ``?follow=`` →
    the live follow tail (Sessions). When live endpoints are disabled (public
    bind — F7), ``?follow=`` degrades to the Sessions view itself rather than
    pointing at an unregistered route.
    """
    from html import escape as esc

    active, main_url = _resolve_view(
        view=view,
        conv_id=conv_id,
        folio_mode=folio_mode,
        folio_event=folio_event,
        search_q=search_q,
        tag=tag,
        workspace_id=workspace_id,
        follow_sid=follow_sid,
        model=model,
        sort=sort,
        live_enabled=live_enabled,
    )

    nav_parts: list[str] = []
    for vid, num, name, ds, url in _NAV_ITEMS:
        cur = ' aria-current="page"' if vid == active else ""
        # Every rail item pushes its canonical shell URL (/?view=<vid>), so the
        # address bar stays canonical and back/forward + refresh land on the
        # right view. State-bearing views (transcript id, search q, …) gain that
        # state from their own drill-downs / controls, not the bare rail nav.
        push_attr = f' hx-push-url="{esc(_canonical_url(vid))}"'
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
    css_v, enhance_v, auth_v = (
        _asset_v("siftd.css"), _asset_v("enhance.js"), _asset_v("auth.js")
    )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- URL-as-state: htmx snapshots #main into its history cache on push and
     restores it on back/forward (see hx-history-elt on #main below). The cache
     lives in localStorage; for siftd's self-hosted, single-tenant deployment
     that is the user's own data on their own device, so the default cache is
     kept. A shared-device / multi-tenant deployment that wants nothing rendered
     persisted to disk should set historyCacheSize=0 AND server-render #main in
     the shell (so refetch-restore isn't blank) — deferred until such a
     deployment is real (F7-style per-deployment policy). -->
<title>siftd</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="/static/vendor/htmx.min.js"></script>
<link href="/static/vendor/prism/prism-tomorrow.min.css" rel="stylesheet">
<link rel="stylesheet" href="/static/siftd.css{css_v}">
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
    <main class="content" id="main" hx-history-elt hx-get="{esc(main_url)}" hx-trigger="load" hx-swap="innerHTML"></main>
  </div>
</div>
<script src="/static/vendor/prism/prism-core.min.js"></script>
<script src="/static/vendor/prism/autoloader.min.js" data-autoloader-path="/static/vendor/prism/components/"></script>
<script src="/static/enhance.js{enhance_v}"></script>
<script src="/static/auth.js{auth_v}"></script>
</body>
</html>"""


@get("/", opt={"no_auth": True}, sync_to_thread=True)
def ui_shell(
    db_path: Path,
    live_enabled: bool,
    auth_config: dict | None = None,
    view: str | None = Parameter(query="view", default=None),
    id: str | None = Parameter(query="id", default=None),
    mode: str = Parameter(query="mode", default="reading"),
    event: str | None = Parameter(query="event", default=None),
    q: str | None = Parameter(query="q", default=None),
    tag: str | None = Parameter(query="tag", default=None),
    ws: str | None = Parameter(query="ws", default=None),
    model: str | None = Parameter(query="model", default=None),
    sort: str | None = Parameter(query="sort", default=None),
    follow: str | None = Parameter(query="follow", default=None),
) -> Response:
    """Serve the Swiss page shell — the single full-page HTML response.

    Accepts optional params for deep-linkable URLs:
        ?id=     — open a conversation (Transcript folio); ?mode=trace + ?event=
                   carry a search → folio jump so a hard reload re-lands on the
                   matched event (event validated to a safe anchor token)
        ?q=      — pre-populate Search
        ?tag=    — Find pre-filtered by a tag (Tags view drill-down)
        ?ws=     — open a workspace detail (Workspaces view drill-down)
        ?follow= — follow a live session (Sessions; degrades to the Sessions
                   view when live endpoints are off)
    """
    footer = _shell_footer(db_path, with_counts=auth_config is None)
    return Response(
        content=_page_shell(
            view=view,
            conv_id=id, folio_mode=mode, folio_event=_safe_event_id(event),
            search_q=q, tag=tag, workspace_id=ws, model=model, sort=sort,
            follow_sid=follow,
            footer=footer, live_enabled=live_enabled,
        ),
        media_type="text/html",
    )


@get("/folio", sync_to_thread=True)
def ui_folio(
    request: Request,
    db_path: Path,
    id: str | None = Parameter(query="id", default=None),
    mode: str = Parameter(query="mode", default="reading"),
    event: str | None = Parameter(query="event", default=None),
) -> Response:
    """Render the Swiss transcript folio for one conversation.

    With ``?id=`` renders that conversation; without, the most recent one so
    the view is never empty. Owner-scoped via the effective identity.

    ``mode`` selects the body shape: ``reading`` (default — prose, tools in the
    ledger) or ``trace`` (tool I/O inlined in sequence, the agent's actual event
    flow). The toggle re-fetches this route so the fidelity is re-resolved from
    the mode — see below.

    ``event`` (the matched chunk's ULID, from a search → folio jump) marks that
    event ``is-target`` and rides out as ``data-scroll-to`` so enhance.js scrolls
    it into view after the swap — the jump lands on the match, not the top.
    Validated to a safe anchor token; a bad value is simply ignored (no scroll).
    """
    mode = mode if mode in ("reading", "trace") else "reading"
    red = _shell_redirect(
        request, "transcript",
        id=id,
        mode="trace" if mode == "trace" else None,
        event=_safe_event_id(event) if mode == "trace" else None,
    )
    if red is not None:
        return red

    from siftd.api.conversations import (
        AmbiguousPrefix,
        get_conversation,
        list_conversations,
    )
    from siftd.output.format_registry import get_format

    owner = _effective_owner(request, None)
    fmt = get_format("html")
    # depth=3 fetches the rollup's canonical cost (+ tags) for the ledger foot.
    # The body mode rides the fidelity's visibility axis: reading needs only
    # text; trace inlines tool I/O + thinking, which get_conversation populates
    # ONLY when they are visible (conversations.py gates input/result + thinking
    # blocks on fidelity.shows). So trace resolves a tools/thinking-visible
    # fidelity here — one fidelity, fetch and render agree.
    trace = mode == "trace"
    fidelity = _fidelity(depth=3, chars=0, tools=trace, thinking=trace)

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
        mode=mode,
        target_event_id=_safe_event_id(event),
        interactive_tags=True,
        tag_action_url="/tag",
        tag_suggest_url="/tags/suggest",
        export_base_url="/export",
    ))


@get("/dashboard", sync_to_thread=True)
def ui_dashboard(
    request: Request,
    db_path: Path,
    model: str | None = Parameter(query="model", default=None),
) -> Response:
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
    red = _shell_redirect(request, "stats", model=model)
    if red is not None:
        return red

    from siftd.api.stats import (
        get_cost_coverage,
        get_input_economy,
        get_stats,
        get_usage_by_model,
        get_usage_by_workspace,
        get_usage_distributions,
        get_usage_summary,
        read_stats_cache,
        write_stats_cache,
    )
    from siftd.output.html_fmt import render_dashboard

    owner = _effective_owner(request, None)

    # The usage_* reads hit the rollup (cheap); get_stats is the full-table
    # sweep (~10s cold on a large DB). Read-through the per-owner stats cache:
    # require_fresh ties it to db mtime so a push-ingest invalidates it, and a
    # miss pays the sweep once, then writes back. Ingest pre-warms owner=None.
    stats = read_stats_cache(db_path=db_path, owner=owner, require_fresh=True)
    if stats is None:
        stats = get_stats(db_path=db_path, owner=owner)
        try:
            write_stats_cache(stats, owner=owner)
        except OSError:
            pass

    by_model = get_usage_by_model(db_path=db_path, owner=owner)
    # Chart-brushing: a ?model= is honoured only when it names a real (canonical)
    # model in this corpus — an unknown/garbage value falls back to the unscoped
    # view rather than rendering an empty-but-scoped chart. The model account
    # stays the GLOBAL ranking (the picker); only the activity charts scope.
    scope_model = model if model and any(g.name == model for g in by_model) else None
    distributions = get_usage_distributions(
        db_path=db_path, owner=owner, model_name=scope_model
    )
    # The input-economy strip follows the brush selection (scoped to the same
    # model), so it re-draws when a Model-mix row is clicked.
    economy = get_input_economy(db_path=db_path, owner=owner, model_name=scope_model)

    return _html_response(
        render_dashboard(
            usage=get_usage_summary(db_path=db_path, owner=owner),
            by_model=by_model,
            by_workspace=get_usage_by_workspace(db_path=db_path, owner=owner),
            coverage=get_cost_coverage(db_path=db_path, owner=owner),
            stats=stats,
            distributions=distributions,
            economy=economy,
            owner=owner,
            scope_model=scope_model,
            brush_base="/dashboard",
            brush_push_base="/?view=stats",
        )
    )


@get("/view/sessions", sync_to_thread=True)
def ui_sessions(
    request: Request,
    db_path: Path,
    live_enabled: bool,
) -> Response:
    """The Swiss 'Sessions' view: a Live zone over a day-grouped Ingested timeline.

    Live zone = the peek scan (server-local session files, no owner concept) —
    rendered only when ``live_enabled`` (the F7 ``allow_live_endpoints``
    policy: off on a public bind, so the host's sessions are never shown to
    remote users; ``/follow`` isn't registered then either). Ingested zone =
    ``list_conversations``, owner-scoped like every DB read.
    """
    red = _shell_redirect(request, "sessions")
    if red is not None:
        return red

    from siftd.api.conversations import list_conversations
    from siftd.output.format_registry import get_format

    live: list = []
    if live_enabled:
        from siftd.api.peek import list_active_sessions

        live = list_active_sessions(include_inactive=False, limit=12)

    owner = _effective_owner(request, None)
    # n=50 counts top-level sessions; each one's sub-agents come along for free
    # and nest under it (so a parent and its children never split across the
    # page boundary).
    summaries = list_conversations(
        fidelity=_fidelity(), db_path=db_path, n=50, owner=owner,
        group_subagents=True,
    )

    fmt = get_format("html")
    return _html_response(fmt.render_sessions(
        live, summaries,
        live_enabled=live_enabled,
        detail_base="/folio",
        shell_base="/",
        follow_base="/follow",
    ))


@get("/view/tags", sync_to_thread=True)
def ui_tags(request: Request, db_path: Path) -> Response:
    """The Swiss 'Tags' view: a pinned zone + most-used zone over a namespace
    tree (flat names split on ``:``). Owner-scoped like every DB read. Rows
    pin/unpin in place and drill into Find pre-filtered by the tag.

    Reads ``list_tags`` live (read-only open). Its six correlated count
    subqueries are heavier than the name-only callers (``/meta``,
    ``/tags/suggest``); if this proves slow on a large corpus it can route
    through the per-owner stats cache like the dashboard, but that is
    measure-gated — not pre-optimized. ``sync_to_thread=True`` keeps the blocking
    read off the event loop meanwhile.
    """
    red = _shell_redirect(request, "tags")
    if red is not None:
        return red

    from siftd.api.tags import list_tags
    from siftd.output.format_registry import get_format

    owner = _effective_owner(request, None)
    tags = list_tags(db_path=db_path, owner=owner)
    fmt = get_format("html")
    return _html_response(fmt.render_tags(
        tags,
        list_base="/find",
        shell_base="/",
        pin_action_url="/tag/pin",
    ))


_WS_SORTS = frozenset({"sessions", "recent", "tokens", "cost"})


@get("/view/workspaces", sync_to_thread=True)
def ui_workspaces(
    request: Request,
    db_path: Path,
    sort: str = Parameter(query="sort", default="sessions"),
) -> Response:
    """The Swiss 'Workspaces' view: a two-tier nav (head cards + body list).

    The head lifts pinned workspaces + a recent strip into ``.cards``; the body
    is the full canonical list under a filter + sort. Rows are ULID-identified
    (so each drills into its own detail) and carry the rollup's tokens + honest
    cost via ``list_workspaces(with_usage=True)``, plus an owner-scoped ``pinned``
    flag. ``?sort=`` ∈ {sessions, recent, tokens, cost} reorders the body (and the
    magnitude bar follows it). The duplicate-workspace caveat is surfaced only
    when unscoped (local), where ``siftd migrate --merge-workspaces`` is runnable;
    it is count-only, so it leaks no path or remote. ``sync_to_thread=True`` keeps
    the blocking read off the event loop.
    """
    red = _shell_redirect(
        request, "workspaces", sort=sort if sort in _WS_SORTS else None
    )
    if red is not None:
        return red

    from siftd.api.migrations import workspace_duplicate_count
    from siftd.api.stats import list_workspaces
    from siftd.output.format_registry import get_format

    sort = sort if sort in _WS_SORTS else "sessions"
    owner = _effective_owner(request, None)
    rows = list_workspaces(db_path=db_path, owner=owner, n=1000, with_usage=True, sort=sort)
    # Local-only: the remediation is a local migration, and suppressing it under
    # an owner scope also avoids advertising cross-tenant corpus shape.
    duplicates = workspace_duplicate_count(db_path) if owner is None else (0, 0)
    fmt = get_format("html")
    return _html_response(fmt.render_workspaces(
        rows,
        detail_base="/workspace",
        shell_base="/",
        pin_action_url="/workspace/pin",
        sort_base="/view/workspaces",
        sort_push_base="/?view=workspaces",
        sort=sort,
        duplicates=duplicates,
    ))


@get("/workspace", sync_to_thread=True)
def ui_workspace_detail(
    request: Request,
    db_path: Path,
    ws: str | None = Parameter(query="ws", default=None),
) -> Response:
    """Render one workspace's detail (the Workspaces master's drill target).

    With ``?ws=`` renders that workspace; without, the most active one so the
    view is never empty (mirrors the folio's latest-conversation fallback).
    Owner-scoped: ``workspace_detail`` returns None for a workspace the owner
    doesn't participate in, which degrades to the not-found stub (no IDOR leak).
    """
    red = _shell_redirect(request, "workspaces", ws=ws)
    if red is not None:
        return red

    from siftd.api.stats import list_workspaces, workspace_detail
    from siftd.output.format_registry import get_format

    owner = _effective_owner(request, None)
    # depth=3 so the recent rows carry the rollup's canonical cost, like the folio.
    fidelity = _fidelity(depth=3, chars=0)

    ws_id = ws
    if not ws_id:
        top = list_workspaces(db_path=db_path, owner=owner, n=1)
        if not top:
            return _html_response(_stub("workspaces", "Workspaces", "no workspaces yet"))
        ws_id = top[0]["id"]

    detail = workspace_detail(ws_id, fidelity=fidelity, db_path=db_path, owner=owner)
    if detail is None:
        return _html_response(_stub("workspaces", "Workspaces", f"not found: {ws_id[:12]}"))
    fmt = get_format("html")
    return _html_response(fmt.render_workspace_detail(
        detail, fidelity,
        detail_base="/folio",
        shell_base="/",
        find_base="/find",
    ))


@get("/view/{name:str}", sync_to_thread=False)
def ui_view_stub(
    request: Request,
    name: str,
    q: str | None = Parameter(query="q", default=None),
) -> Response:
    """Placeholder for a view not yet authored in this slice (Swiss Phase B)."""
    red = _shell_redirect(request, name, q=q)
    if red is not None:
        return red
    title = _VIEW_TITLES.get(name, name.replace("-", " ").title())
    hint = "coming in a later slice"
    if name == "search" and q:
        hint = f'search "{q[:40]}" — coming in a later slice'
    return _html_response(_stub(name, title, hint))


@get("/find", sync_to_thread=False)
def ui_find(
    request: Request,
    q: str | None = Parameter(query="q", default=None),
    tag: str | None = Parameter(query="tag", default=None),
) -> Response:
    """The Swiss 'Find' view: one surface unifying metadata facets + content search.

    A host fragment, not a renderer — it composes the two existing reads it has
    no new logic over: ``/meta`` (the control strip: search box + filter
    dropdowns) loads into ``#filters``, ``/query`` (the results pane) loads into
    ``#list``. Every control in the strip targets ``#list`` and includes
    ``#filters``, so the box and the facets compose into one request: facets
    alone browse via ``list_conversations``; a content query additionally routes
    ``#list`` through the search engine (see below).

    A deep-linked ``?q=`` pre-fills the box and the initial results; a ``?tag=``
    (the Tags view's drill-down) pre-selects that tag in the filter strip and
    pre-filters the list — so a tag click lands on a real Find surface the user
    can refine, not a dead-end table. A content query routes ``#list`` through
    the real search ENGINE (``ui_query`` → ``search_chunks``): ranked excerpt
    hits, hybrid when this server has embeddings and a graceful keyword (fts)
    fallback otherwise — the same ranking the CLI/REST surfaces serve, with the
    engine that ran named in the result header.
    """
    red = _shell_redirect(request, "search", q=q, tag=tag)
    if red is not None:
        return red

    from urllib.parse import quote as urlquote

    parts: list[str] = []
    term = (q or "").strip()
    if term:
        parts.append(f"search={urlquote(term)}")
    tag_v = (tag or "").strip()
    if tag_v:
        parts.append(f"tag={urlquote(tag_v)}")
    qs = ("?" + "&".join(parts)) if parts else ""
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


@get("/meta", sync_to_thread=True)
def ui_meta(
    request: Request,
    db_path: Path,
    search: str | None = Parameter(query="search", default=None),
    tag: str | None = Parameter(query="tag", default=None),
    view: str = Parameter(query="view", default="chunks"),
    mode: str = Parameter(query="mode", default="auto"),
) -> Response:
    """Return the find control strip: a content-search box + search/facet controls.

    ``search`` pre-fills the box for deep-linked ``?q=`` loads; ``tag``
    pre-selects the tag dropdown for the Tags view's ``?tag=`` drill-down, so the
    filter is visible and the user can refine from there. ``view``/``mode``
    pre-select the result-shape and engine toggles (defaults chunks/auto). Every
    control targets ``#list`` and includes ``#filters``, so the box, the search
    toggles, and the metadata facets compose into one query in a single request.

    The strip carries two kinds of control. The *search* toggles modify the
    content query — ``view`` (excerpts / thread / conversations: the same shapes
    the CLI's ``--view`` exposes) and ``mode`` (the engine: auto / hybrid /
    semantic / keyword). The engine toggle is shown only when this server has
    embeddings — without them every engine collapses to keyword, so the knob
    would be a no-op that contradicts the header's truthful ``[fts]`` label. The
    *facet* dropdowns (workspace / model / tag / owner / since / before) filter
    which conversations the query (or the bare browse) draws from.
    """
    red = _shell_redirect(request, "search", q=search, tag=tag)
    if red is not None:
        return red

    from html import escape

    from siftd.api import embeddings_available
    from siftd.api.search import SEARCH_MODES, SEARCH_VIEWS
    from siftd.api.stats import list_models, list_workspaces
    from siftd.api.tags import list_tags
    from siftd.paths import embeddings_db_path

    owner = _effective_owner(request, None)
    has_embed = embeddings_available() and embeddings_db_path().exists()

    model_names: list[str] = []
    try:
        model_names = list_models(db_path=db_path, owner=owner)
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

    def _select(
        name: str, label: str, options: list[tuple[str, str]], *, selected: str = ""
    ) -> str:
        """Build a facet <select> (with an 'All' clear option) from (value, text)."""
        opts = [f'<option value="">All {label}</option>']
        for val, display in options:
            if val:
                sel = " selected" if val == selected else ""
                opts.append(f'<option value="{escape(val)}"{sel}>{escape(display)}</option>')
        return (
            f'<select name="{name}"'
            f' hx-get="/query" hx-target="#list" hx-trigger="change"'
            f' hx-include="#filters">'
            f'{"".join(opts)}</select>'
        )

    def _toggle(
        name: str, title: str, options: list[tuple[str, str]], *, selected: str
    ) -> str:
        """Build a search toggle: a required single-choice <select> (no 'All').

        Unlike a facet, a toggle always carries a value — it modifies how the
        content query renders/runs rather than narrowing the result set — so it
        defaults to ``selected`` and has no clear option.
        """
        opts = []
        for val, display in options:
            sel = " selected" if val == selected else ""
            opts.append(f'<option value="{escape(val)}"{sel}>{escape(display)}</option>')
        return (
            f'<select name="{name}" title="{escape(title)}" class="search-toggle"'
            f' hx-get="/query" hx-target="#list" hx-trigger="change"'
            f' hx-include="#filters">'
            f'{"".join(opts)}</select>'
        )

    from siftd.output.common import fmt_workspace

    ws_opts = [(r["path"], fmt_workspace(r["path"])) for r in ws_rows if r["path"]]
    model_opts = [(m, m) for m in model_names]
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

    # Search toggles: result shape (always) + engine (only when embeddings make
    # the choice real). Values are the canonical CLI/REST tokens; labels read
    # for a human. ``view`` defaults to chunks, ``mode`` to auto.
    view_toggle = _toggle(
        "view", "Result shape",
        [("chunks", "Excerpts"), ("thread", "Thread"), ("conversations", "Conversations")],
        selected=(view if view in SEARCH_VIEWS else "chunks"),
    )
    search_toggles = [view_toggle]
    if has_embed:
        search_toggles.append(
            _toggle(
                "mode", "Search engine",
                [("auto", "Auto"), ("hybrid", "Hybrid"),
                 ("semantic", "Semantic"), ("fts", "Keyword")],
                selected=(mode if mode in SEARCH_MODES else "auto"),
            )
        )

    owner_input = (
        '<input type="text" name="owner" placeholder="Owner"'
        ' hx-get="/query" hx-target="#list" hx-trigger="change"'
        ' hx-include="#filters" class="filter-input">'
    )

    # Two-state strip (Slice 2c), CSS-only via :has — the strip never re-renders,
    # so the search box keeps focus while typing. Builder = nothing engaged
    # (#list shows .find-prompt) OR force-expanded (#sx-expand:checked); collapsed
    # = a search/browse is showing. In collapsed mode CSS hides inactive facet
    # selects and styles the active ones as chips, tucks the secondary filters,
    # and shows the expand chevron. Primary facets (ws/model/tag) chip cleanly off
    # a <select>'s checked option; owner/dates (no such signal) live in the
    # "more filters" disclosure in both modes.
    parts = [
        # Force-expand checkbox: a sibling :has() target, visually removed; its
        # label is the collapsed-mode chevron below.
        '<input type="checkbox" id="sx-expand" class="sx-expand" tabindex="-1"'
        ' aria-hidden="true">',
        search_box,
        '<div class="find__toggles">' + "".join(search_toggles) + "</div>",
        '<div class="find__facets">'
        + _select("workspace", "workspaces", ws_opts)
        + _select("model", "models", model_opts)
        + _select("tag", "tags", tag_opts, selected=(tag or ""))
        + "</div>",
        '<details class="find__more"><summary>more filters</summary>'
        '<div class="find__morebody">'
        + owner_input
        + _date("since", "Since")
        + _date("before", "Before")
        + "</div></details>",
        '<label for="sx-expand" class="find__expand" title="Show all filters">'
        "filters</label>",
    ]
    return _html_response("".join(parts))


def _find_search_fragment(
    db_path: Path,
    term: str,
    fmt: Any,
    ctx: dict,
    *,
    workspace: str | None,
    model: str | None,
    tag: list[str] | None,
    since: str | None,
    before: str | None,
    owner: str | None,
    n: int,
    mode: str,
    view: str,
) -> Response:
    """Render a Find content query through the real search ENGINE + recipe.

    Delegates to ``api.search.search_view`` — the same Operation function the
    CLI and the REST ``/api/v1/search`` route run — so the browser inherits the
    *whole* repertoire, not just chunks: ``search_view`` is
    ``search_chunks`` (engine) ∘ ``process_search_view`` (the post-processing
    recipe), and it owns candidate-pool widening, axis validation, metadata
    enrichment, and the ``chunks``/``thread``/``conversations`` view shapes,
    returning a render-ready ``SearchView``. This function adds only the two
    concerns the shared api fn correctly lacks and that are specific to a
    multi-tenant browser pane:

    1. *Graceful mode resolution.* ``mode`` resolves to hybrid when this server
       has embeddings, else fts. An explicit semantic/hybrid request against a
       no-embed server degrades to fts rather than erroring (the api fn raises
       ``EmbeddingsRequiredError`` for the CLI/REST 4xx; the pane never errors).
    2. *Degrade-to-fts on engine failure* (the "never 500 the pane" promise —
       there is no app-level exception handler). A hybrid/semantic failure
       (index drift after an upgrade, embedding backend down) drops to the
       keyword engine — the same fallback a no-embed server gets, reported
       truthfully as ``[fts]``; a keyword failure renders an empty pane.

    The resolved engine rides the render envelope (``mode=``) so the header can
    truthfully state which engine ran; it is owner-safe (an envelope field,
    never a caveat, which the multi-tenant guard blanks). Engine scoping
    excludes active (live) and derivative (sub-agent) conversations by default —
    matching CLI/REST ``search``, and unlike the bare facet list (the
    no-content-query browse), which still shows them.
    """
    import sqlite3

    from siftd.api import embeddings_available
    from siftd.api.search import (
        EmbeddingsRequiredError,
        resolve_search_mode,
        search_view,
    )
    from siftd.domain.search_types import SearchView
    from siftd.paths import embeddings_db_path

    has_embed = embeddings_available() and embeddings_db_path().exists()
    try:
        engine = resolve_search_mode(mode, has_embeddings=has_embed)
    except (EmbeddingsRequiredError, ValueError):
        # An explicit semantic/hybrid request against a server without
        # embeddings degrades to keyword search rather than erroring the pane.
        engine = "fts"

    def _run(eng: str) -> SearchView:
        # Pass the raw term (the engine tokenizes/sanitizes it for parity with
        # the CLI; ``raw_fts`` defaults False) and let search_view own the FTS
        # contract, the recipe, and the view shape.
        return search_view(
            term,
            db_path=db_path,
            n=n,
            mode=eng,
            view=view,
            workspace=workspace,
            model=model,
            since=since,
            before=before,
            tag=tag,
            owner=owner,
        )

    # sqlite3.DatabaseError (not OperationalError) is the family root — it also
    # covers the corrupt-image errors a malformed main DB / FTS index raises at
    # query time, which a bare OperationalError catch would let escape and 500
    # the pane. The embed-path drift error (IndexCompatError) lives in the embed
    # module (numpy) and is only importable/raisable when this server has
    # embeddings. ValueError covers search_view's axis validation: ui_query
    # clamps view to SEARCH_VIEWS, so the only residual bad combo is a
    # hand-crafted query string, which then degrades to an empty pane (not a 500).
    engine_errors: tuple[type[Exception], ...] = (
        sqlite3.DatabaseError, ValueError, RuntimeError, OSError,
    )
    if has_embed:
        from siftd.api.search import IndexCompatError

        engine_errors = (*engine_errors, IndexCompatError)

    try:
        sv = _run(engine)
    except engine_errors:
        # A hybrid/semantic failure degrades to keyword search (still useful;
        # header then truthfully reads [fts]); a keyword failure → empty pane.
        if engine != "fts":
            engine = "fts"
            try:
                sv = _run("fts")
            except (sqlite3.DatabaseError, ValueError, RuntimeError):
                sv = SearchView(results=[], view=view)
        else:
            sv = SearchView(results=[], view=view)

    fidelity = _fidelity()
    # search_view returns a render-ready SearchView; render_search reads the
    # view shape and the thread tier1/tier2 split off it (not context).
    return _html_response(
        fmt.render_search(sv, fidelity, query=term, mode=engine, **ctx)
    )


@get("/query", sync_to_thread=True)
def ui_query(
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
    mode: str = Parameter(query="mode", default="auto"),
    view: str = Parameter(query="view", default="chunks"),
) -> Response:
    """The Find results fragment — rows/hits mount the folio into ``#main``.

    Two shapes behind the one ``#list`` target:

    - A content query (``search``) runs the real search ENGINE + recipe
      (``search_view`` → hybrid/fts), rendering ranked excerpt hits — so the
      browser gets the same relevance ranking the CLI and REST surfaces do, not
      a recency-ordered keyword *filter*. ``mode`` resolves to hybrid when this
      server has embeddings, else fts (graceful fallback). ``view`` selects the
      result shape (``chunks``/``thread``/``conversations``), the same repertoire
      the CLI's ``--view`` and the REST ``view=`` param expose.
    - No content query → the facet-filtered conversation list (recency order).

    Facets (workspace/model/tag/date/owner) compose into either shape. The old
    ``?id=`` detail mode is gone — the folio is the single detail surface.
    """
    red = _shell_redirect(request, "search", q=search)
    if red is not None:
        return red

    from siftd.output.format_registry import get_format

    # Normalize empty strings to None (htmx sends "" for blank inputs)
    workspace = workspace or None
    model = model or None
    since = since or None
    before = before or None
    owner_filter = owner or None  # the user-supplied owner FACET (before resolution)
    owner = _effective_owner(request, owner or None)
    tag = [t for t in (tag or []) if t] or None
    # Active facets = the user narrowing the corpus (the owner facet is the raw
    # user value, NOT the always-present effective owner). Drives the no-query
    # branch: no term + no facet → the search prompt; no term + a facet → browse.
    has_facets = bool(workspace or model or tag or since or before or owner_filter)

    # Clamp the result shape to the canonical vocabulary BEFORE delegating —
    # mirrors the control strip, which clamps the toggle's selected value. An
    # out-of-vocab view (only reachable by hand-editing the URL) would otherwise
    # make search_view's axis validation raise, degrade to an empty pane, and
    # mask the real hits behind a truthful-looking [fts] "No matches"; clamping
    # to the default returns the chunks results instead. ``mode`` needs no clamp:
    # an invalid engine resolves gracefully to fts and still runs a real search.
    from siftd.api.search import SEARCH_VIEWS

    view = view if view in SEARCH_VIEWS else "chunks"

    fmt = get_format("html")
    ctx = {"detail_base": "/folio", "shell_base": "/"}

    # A content query routes through the engine. The find box is untrusted text;
    # sanitize_fts5_query is used here only to test for a *meaningful* query —
    # punctuation-only input (which sanitizes to empty) drops to the facet list
    # below, matching the no-query browse UX and never raising an fts5 500.
    term = (search or "").strip()
    if term:
        from siftd.api import sanitize_fts5_query

        if sanitize_fts5_query(term).fts_query:
            return _find_search_fragment(
                db_path, term, fmt, ctx,
                workspace=workspace, model=model, tag=tag,
                since=since, before=before, owner=owner, n=n, mode=mode, view=view,
            )

    # No content term: Find is search-first (Slice 2b). With no active facet the
    # bare recency list is dropped for a search prompt; with a facet active the
    # facet-filtered browse stays (the Tags/Workspaces drill-downs land here).
    if not has_facets:
        return _html_response(fmt.render_find_prompt())

    from siftd.api.conversations import list_conversations
    from siftd.api.dispatch import Operation, dispatch

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
            "search": None,
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


@get("/find/context", sync_to_thread=True)
def ui_find_context(
    request: Request,
    db_path: Path,
    id: str | None = Parameter(query="id", default=None),
    at: int = Parameter(query="at", default=0),
    w: int = Parameter(query="w", default=0),
    event: str | None = Parameter(query="event", default=None),
) -> Response:
    """Render one search hit's inline context slice — the chunks-view 'unfold'.

    A search result is a seed (its conversation + matched ``turn_index``); this
    unfolds a window of surrounding exchanges *in place*, so the user reads the
    context without leaving the results. It runs the SAME windowed read the CLI's
    ``--at-turn``/``--around`` drive — ``get_conversation(anchor='at_turn',
    window=±w)`` — the Operation IR's read exposed as progressive disclosure
    rather than up-front flags. The matched exchange is flagged in the slice; the
    last ring defers to the full folio (the deliberate jump).

    ``w`` is the stepped ring (clamped to ``SEARCH_CONTEXT_RINGS``); ``w=0`` is
    the collapsed trigger and skips the DB entirely. Owner-scoped — the read
    resolves the id under the effective identity, so a hit's context can't leak a
    conversation the requester doesn't own (an unowned/ambiguous id, or an
    out-of-range/failed anchor, degrades to the collapsed trigger, never a 500).
    """
    red = _shell_redirect(request, "search")
    if red is not None:
        return red

    from siftd.output.format_registry import get_format
    from siftd.output.html_fmt import SEARCH_CONTEXT_RINGS, SEARCH_PREVIEW_CHARS

    fmt = get_format("html")
    # Clamp untrusted inputs: w to the allowed rings (else collapsed), at >= 0.
    w = w if w in SEARCH_CONTEXT_RINGS else 0
    conv_id = (id or "").strip()
    # The matched event rides the ring URLs so the last ring's 'open in folio'
    # jump stays event-precise; validated to a safe anchor token.
    ctx: dict[str, Any] = {"conv_id": conv_id, "at": at, "w": w, "event": _safe_event_id(event)}

    if w <= 0 or at < 0 or not conv_id:
        return _html_response(fmt.render_search_context(None, _fidelity(), **ctx))

    import sqlite3

    from siftd.api.conversations import (
        AmbiguousPrefix,
        AnchorError,
        get_conversation,
    )

    owner = _effective_owner(request, None)
    # The unfold is a reading preview (render_search_context renders mode="reading"):
    # prose + thinking only, char-capped so each turn is a scannable excerpt.
    # tools=False — tool I/O isn't inlined here (it's the chip/ledger's job), and
    # not fetching it keeps the slice read light. depth=1: no tags/cost in a slice.
    fidelity = _fidelity(depth=1, chars=SEARCH_PREVIEW_CHARS, tools=False, thinking=True)
    try:
        detail = get_conversation(
            conv_id, fidelity=fidelity, db_path=db_path, owner=owner,
            anchor="at_turn", anchor_value=at, window_start=-w, window_end=w,
        )
    except (AmbiguousPrefix, AnchorError, ValueError, OSError, sqlite3.DatabaseError):
        # A bad/ambiguous id, an out-of-range anchor, or a corrupt DB/FTS image
        # (sqlite3.DatabaseError — the family root, NOT an OSError subclass)
        # collapses the region rather than erroring the pane (the never-500
        # promise, mirroring _find_search_fragment's engine catch).
        detail = None

    turns = getattr(detail, "turns", []) or [] if detail is not None else []
    # The anchor exchange sits at offset min(at, w) in the returned window
    # (get_conversation slices turns[max(0, at-w) : at+w+1]); clamp defensively.
    anchor_pos = min(at, w)
    if anchor_pos >= len(turns):
        anchor_pos = len(turns) - 1 if turns else 0
    ctx["anchor_pos"] = anchor_pos
    return _html_response(fmt.render_search_context(detail, fidelity, **ctx))


# ---------------------------------------------------------------------------
# Live session endpoints — peek/follow
# ---------------------------------------------------------------------------


@get("/follow", sync_to_thread=True)
def ui_follow(
    request: Request,
    sid: str = Parameter(query="sid", default=""),
) -> Response:
    """Follow a live session — the folio rendered from a live source.

    Not a separate view: ``folio_detail_from_session`` projects the peek
    detail onto the folio's duck shape and ``render_folio`` does the rest,
    under the Sessions chrome. ``live_poll_url`` makes the fragment
    self-refreshing (whole-folio outerHTML swap, so the rail, ledger and
    token foot all advance together); enhance.js keeps the body pinned to
    the bottom across swaps — it is a tail.
    """
    red = _shell_redirect(request, "sessions", follow=sid or None)
    if red is not None:
        return red

    from html import escape
    from urllib.parse import quote as urlquote

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

    from siftd.output.html_fmt import folio_detail_from_session

    fmt = get_format("html")
    fidelity = _fidelity(depth=2, chars=0, tools=True, thinking=True)

    folio_detail = folio_detail_from_session(detail)
    return _html_response(fmt.render_folio(
        folio_detail, fidelity,
        view="sessions",
        title="Sessions",
        kick=f"follow · {escape(short_id(detail.info.session_id))}",
        live_poll_url=f"/follow?sid={urlquote(sid)}",
    ))


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


@post("/tag/pin")
async def ui_tag_pin(request: Request, db_path: Path) -> Response:
    """Pin or unpin a tag for the effective owner; return the re-rendered Tags view.

    Mirrors ``ui_tag``: write-scoped, owner-scoped, audited. Returns the whole
    view fragment (swapped into ``#main``) rather than a single row so the pinned
    zone reflects the change immediately — a pinned tag jumps to the top, an
    unpinned one drops back into the tree. Async because it ``await``s the form
    body; the pin itself is one tiny INSERT/DELETE.
    """
    from siftd.serve.auth import require_write

    require_write(request)

    from siftd.api import record_audit_event
    from siftd.api.tags import list_tags, set_tag_pin
    from siftd.output.format_registry import get_format
    from siftd.serve.routes import _actor_identity, _client_ip

    owner = _effective_owner(request, None)
    form = await request.form()
    action = "unpin" if str(form.get("action", "pin")) == "unpin" else "pin"
    tag_name = str(form.get("tag", "")).strip()

    if tag_name:
        set_tag_pin(tag_name, pinned=(action == "pin"), db_path=db_path, owner=owner)
        record_audit_event(
            db_path=db_path,
            actor=_actor_identity(request),
            action=f"tag.{action}",
            target_type="tag",
            target=tag_name,
            source_ip=_client_ip(request),
        )

    tags = list_tags(db_path=db_path, owner=owner)
    fmt = get_format("html")
    return _html_response(fmt.render_tags(
        tags, list_base="/find", shell_base="/", pin_action_url="/tag/pin",
    ))


@post("/workspace/pin")
async def ui_workspace_pin(request: Request, db_path: Path) -> Response:
    """Pin or unpin a workspace for the effective owner; return the re-rendered
    Workspaces view. Mirrors ``ui_tag_pin``: write-scoped, owner-scoped, audited,
    whole-fragment swap (so the head's Pinned/Recent zones reflect the change).
    The active ``sort`` rides the form so the re-render preserves the body order.
    """
    from siftd.serve.auth import require_write

    require_write(request)

    from siftd.api import record_audit_event
    from siftd.api.migrations import workspace_duplicate_count
    from siftd.api.stats import list_workspaces, set_workspace_pin
    from siftd.output.format_registry import get_format
    from siftd.serve.routes import _actor_identity, _client_ip

    owner = _effective_owner(request, None)
    form = await request.form()
    action = "unpin" if str(form.get("action", "pin")) == "unpin" else "pin"
    ws_id = str(form.get("ws", "")).strip()
    sort = str(form.get("sort", "sessions"))
    sort = sort if sort in _WS_SORTS else "sessions"

    if ws_id:
        set_workspace_pin(ws_id, pinned=(action == "pin"), db_path=db_path, owner=owner)
        record_audit_event(
            db_path=db_path,
            actor=_actor_identity(request),
            action=f"workspace.{action}",
            target_type="workspace",
            target=ws_id,
            source_ip=_client_ip(request),
        )

    rows = list_workspaces(db_path=db_path, owner=owner, n=1000, with_usage=True, sort=sort)
    duplicates = workspace_duplicate_count(db_path) if owner is None else (0, 0)
    fmt = get_format("html")
    return _html_response(fmt.render_workspaces(
        rows,
        detail_base="/workspace",
        shell_base="/",
        pin_action_url="/workspace/pin",
        sort_base="/view/workspaces",
        sort_push_base="/?view=workspaces",
        sort=sort,
        duplicates=duplicates,
    ))


@get("/tags/suggest", sync_to_thread=True)
def ui_tags_suggest(
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


@get("/export", sync_to_thread=True)
def ui_export(
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
