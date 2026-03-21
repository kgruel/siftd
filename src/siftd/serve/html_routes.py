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

_PAGE_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>siftd</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<style>
:root {
  --fg: #c9d1d9; --bg: #0d1117; --bg-surface: #161b22;
  --accent: #58a6ff; --muted: #484f58; --success: #3fb950;
  --warning: #d29922; --error: #f85149;
}
*, *::before, *::after { box-sizing: border-box; }
body {
  font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  font-size: 14px; line-height: 1.5;
  color: var(--fg); background: var(--bg);
  margin: 0; padding: 0;
}
nav {
  background: var(--bg-surface); padding: 0.5rem 1rem;
  border-bottom: 1px solid var(--muted);
  display: flex; gap: 1rem; align-items: center;
}
nav .brand { color: var(--accent); font-weight: bold; }
nav input[type="search"] {
  background: var(--bg); color: var(--fg); border: 1px solid var(--muted);
  padding: 0.25rem 0.5rem; border-radius: 4px; flex: 1; max-width: 400px;
  font-family: inherit; font-size: inherit;
}
main { display: flex; height: calc(100vh - 41px); }
#list-pane { width: 50%; overflow-y: auto; border-right: 1px solid var(--muted); }
#detail-pane { width: 50%; overflow-y: auto; padding: 1rem; }

/* Domain styles — mirror DomainStyles semantic vocabulary */
.identifier { color: var(--accent); font-weight: bold; }
.temporal { color: var(--muted); }
.metric { color: var(--muted); }
.workspace { color: var(--fg); }
.model { color: var(--fg); opacity: 0.8; }
.tag { color: var(--success); }
.adapter { color: var(--muted); }
.summary { color: var(--muted); }
.prompt { color: var(--accent); }
.assistant { }
.tool-name { color: var(--accent); }
.tool-input { color: var(--muted); }
.tool-result { color: var(--fg); }
.tool-error { color: var(--error); }
.thinking { color: var(--muted); font-style: italic; }

/* Table */
table.conversation-list {
  width: 100%; border-collapse: collapse;
}
table.conversation-list th {
  text-align: left; padding: 0.5rem; border-bottom: 1px solid var(--muted);
  color: var(--muted); font-weight: normal; font-size: 0.85em;
  position: sticky; top: 0; background: var(--bg-surface);
}
table.conversation-list td { padding: 0.4rem 0.5rem; }
table.conversation-list tr { cursor: pointer; }
table.conversation-list tr:hover { background: var(--bg-surface); }
table.conversation-list tr.htmx-request { opacity: 0.6; }

/* Fidelity controls */
.fidelity-controls {
  display: flex; gap: 0.25rem; margin-top: 0.5rem;
}
.fidelity-controls .toggle {
  background: var(--bg); color: var(--muted); border: 1px solid var(--muted);
  padding: 0.15rem 0.5rem; border-radius: 3px; cursor: pointer;
  font-family: inherit; font-size: 0.8em;
  transition: all 0.1s ease;
}
.fidelity-controls .toggle:hover { color: var(--fg); border-color: var(--fg); }
.fidelity-controls .toggle.active {
  color: var(--accent); border-color: var(--accent); background: var(--bg-surface);
}
.fidelity-controls .toggle.htmx-request { opacity: 0.5; }

/* Detail */
.conversation-detail { }
.conversation-header { margin-bottom: 1rem; }
.conversation-header .meta { display: flex; gap: 0.75rem; flex-wrap: wrap; }
.turn { margin-bottom: 1.5rem; border-bottom: 1px solid var(--bg-surface); padding-bottom: 1rem; }
.turn h3 { margin: 0.5rem 0; font-size: 0.9em; }
.narrative-text { margin: 0.5rem 0; white-space: pre-wrap; }

/* Tools */
.tool-call { margin: 0.5rem 0; padding: 0.5rem; border-left: 2px solid var(--muted); }
.tool-call .tool-name { font-weight: bold; font-size: 0.85em; margin-bottom: 0.15rem; }
.tool-call .tool-headline {
  display: block; color: var(--fg); font-size: 0.85em;
  background: var(--bg); padding: 0.15rem 0.3rem; border-radius: 2px;
}
.tool-call .tool-meta { color: var(--muted); font-size: 0.8em; margin-left: 0.5rem; }
.tool-call pre { margin: 0.25rem 0; font-size: 0.8em; max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
.tool-call .tool-removed { color: var(--error); opacity: 0.8; }
.tool-call .tool-removed::before { content: "- "; }
.tool-call .tool-added { color: var(--success); }
.tool-call .tool-added::before { content: "+ "; }
.tool-call .tool-overflow { color: var(--muted); font-size: 0.8em; font-style: italic; }
.tool-call .tool-error { color: var(--error); }
.tool-call .tool-tasks { list-style: none; padding-left: 0.5rem; margin: 0.25rem 0; font-size: 0.85em; }
.tool-call .task-done::before { content: "\\2713 "; color: var(--success); }
.tool-call .task-pending::before { content: "\\25CB "; color: var(--muted); }
.tool-summary { color: var(--muted); font-size: 0.85em; margin: 0.25rem 0; }

/* Thinking */
details.thinking { border-left: 2px solid var(--muted); padding-left: 0.5rem; margin: 0.5rem 0; }
details.thinking pre { font-size: 0.85em; white-space: pre-wrap; }
.thinking.placeholder { color: var(--muted); font-style: italic; }

/* Search */
.search-results h2 { font-size: 1.1em; margin: 0 0 1rem; }
.search-hit { margin-bottom: 1rem; padding: 0.5rem; }
.search-hit:hover { background: var(--bg-surface); cursor: pointer; }
.search-hit header { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.search-hit .excerpt { margin-top: 0.25rem; white-space: pre-wrap; font-size: 0.9em; }

.empty { color: var(--muted); padding: 2rem; text-align: center; }
</style>
</head>
<body>
<nav>
  <span class="brand">siftd</span>
  <input type="search" name="q" placeholder="Search..."
    hx-get="/ui/search" hx-target="#list" hx-trigger="keyup changed delay:300ms"
    hx-include="this">
  <a href="#" hx-get="/ui/query" hx-target="#list"
    style="color:var(--accent);text-decoration:none">Recent</a>
</nav>
<main>
  <div id="list-pane">
    <div id="list" hx-get="/ui/query" hx-trigger="load" hx-swap="innerHTML">
    </div>
  </div>
  <div id="detail-pane">
    <div id="detail">
      <p class="empty">Select a conversation</p>
    </div>
  </div>
</main>
</body>
</html>"""


@get("/ui", opt={"no_auth": True})
async def ui_shell() -> Response:
    """Serve the page shell — the single full-page HTML response."""
    return Response(content=_PAGE_SHELL, media_type="text/html")


# ---------------------------------------------------------------------------
# Fragment endpoints — htmx swap targets
# ---------------------------------------------------------------------------


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
    html = fmt.render_list(rows, _fidelity(), detail_base="/ui/query")
    return _html_response(html)


@get("/ui/search", opt={"no_auth": True})
async def ui_search(
    db_path: Path,
    q: str = Parameter(query="q", default=""),
) -> Response:
    """Search conversations, return HTML fragment."""
    from siftd.output.format_registry import get_format

    if not q.strip():
        return _html_response('<p class="empty">Type to search...</p>')

    fmt = get_format("html")

    # Try FTS first (always available), fall back gracefully
    from siftd.api.conversations import list_conversations

    rows = list_conversations(db_path=db_path, search=q, limit=20)
    if rows:
        html = fmt.render_list(rows, _fidelity(), detail_base="/ui/query")
        return _html_response(html)

    return _html_response(f'<p class="empty">No results for: {q}</p>')
