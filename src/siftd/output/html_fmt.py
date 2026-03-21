"""HTML fragment output format — renders conversations as htmx-swappable fragments.

Returns HTML strings (not full pages). A page shell provides the chrome;
these fragments are swap targets for htmx requests or standalone embeds.

DomainStyles map 1:1 to CSS classes — the same semantic vocabulary
(identifier, temporal, metric, tool_name, ...) used in terminal rendering
becomes the class namespace here.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity

FORMATTER_INTERFACE_VERSION = 1
name = "html"
media_type = "text/html"


def _hx_detail(detail_base: str, conv_id: str, shell_base: str = "") -> str:
    """Build htmx attributes for a detail link, or empty string if no base."""
    if not detail_base:
        return ""
    push = f' hx-push-url="{escape(shell_base)}?id={escape(conv_id)}"' if shell_base else ""
    return (
        f' hx-get="{escape(detail_base)}?id={escape(conv_id)}"'
        f' hx-target="#detail" hx-swap="innerHTML"'
        f'{push}'
    )


# ---------------------------------------------------------------------------
# Render methods — OutputFormat protocol
# ---------------------------------------------------------------------------


def _render_controls(controls: dict, detail_base: str) -> str:
    """Render fidelity toggle buttons for the detail view.

    Each button represents a target fidelity state — clicking it re-fetches
    the conversation with those params. Active buttons show current state.
    """
    conv_id = controls.get("id", "")
    tools = controls.get("tools", False)
    thinking = controls.get("thinking", False)
    full = controls.get("full", False)
    brief = controls.get("brief", False)

    def _qs(**params: str | bool) -> str:
        """Build query string from non-falsy params."""
        parts = []
        for k, v in sorted(params.items()):
            if v and v is not False:
                parts.append(f"{k}={escape(str(v).lower() if isinstance(v, bool) else str(v))}")
        return "&".join(parts)

    def _btn(label: str, qs: str, active: bool) -> str:
        css = "toggle active" if active else "toggle"
        href = f"{escape(detail_base)}?{qs}" if detail_base else f"?{qs}"
        return (
            f'<button class="{css}"'
            f' hx-get="{href}"'
            f' hx-target="#detail" hx-swap="innerHTML">'
            f"{label}</button>"
        )

    buttons = [
        # Tools: toggle, preserve thinking
        _btn("Tools", _qs(id=conv_id, tools=not tools, thinking=thinking), tools),
        # Thinking: toggle, preserve tools
        _btn("Thinking", _qs(id=conv_id, tools=tools, thinking=not thinking), thinking),
        # Brief preset
        _btn("Brief", _qs(id=conv_id, brief=True), brief),
        # Full preset (everything on)
        _btn("Full", _qs(id=conv_id, full=True), full),
    ]

    return '<div class="fidelity-controls">' + "".join(buttons) + "</div>"


def render_detail(turns: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation detail as an HTML fragment.

    Context keys:
        detail: conversation metadata object
        tool_chars: int
        no_header: bool
        controls: dict — fidelity toggle state (id, tools, thinking, full, brief)
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace
    from siftd.output.narrative import HtmlEmitter, walk_narrative

    detail = context.get("detail")
    no_header = context.get("no_header", False)
    tool_chars = context.get("tool_chars", 0)
    controls = context.get("controls")

    parts: list[str] = ['<article class="conversation-detail">']

    if detail and not no_header:
        detail_id = getattr(detail, "id", "") or ""

        # Breadcrumb: workspace > date > ID
        ws = fmt_workspace(getattr(detail, "workspace_path", None))
        ts_date = fmt_timestamp(getattr(detail, "started_at", None))
        crumbs = []
        if ws:
            crumbs.append(f'<span class="workspace">{escape(ws)}</span>')
        if ts_date:
            crumbs.append(f'<span class="temporal">{escape(ts_date.split(" ")[0])}</span>')
        crumbs.append(f'<span class="identifier">{escape(detail_id[:12])}</span>')
        breadcrumb = '<nav class="breadcrumb">' + " ".join(crumbs) + "</nav>"

        parts.append('<header class="conversation-header">')
        parts.append(breadcrumb)

        meta = []
        ts = fmt_timestamp(getattr(detail, "started_at", None))
        if ts:
            meta.append(f'<span class="temporal">{escape(ts)}</span>')
        model = fmt_model(getattr(detail, "model", None))
        if model:
            meta.append(f'<span class="model">{escape(model)}</span>')

        total_tokens = getattr(detail, "total_tokens", None)
        if total_tokens is None:
            total_tokens = (
                getattr(detail, "total_input_tokens", 0)
                + getattr(detail, "total_output_tokens", 0)
            )
        if total_tokens:
            meta.append(
                f'<span class="metric">{escape(fmt_tokens(total_tokens))} tokens</span>'
            )

        tags = getattr(detail, "tags", None)
        if tags:
            for tag in tags:
                meta.append(f'<span class="tag">{escape(tag)}</span>')

        if meta:
            parts.append(f'<div class="meta">{" ".join(meta)}</div>')
        if controls:
            detail_base = context.get("detail_base", "")
            parts.append(_render_controls(controls, detail_base))
        parts.append("</header>")

    for turn in turns:
        parts.append('<section class="turn">')

        ts = fmt_timestamp(getattr(turn, "timestamp", None), time_only=True)

        prompt_text = getattr(turn, "prompt_text", None)
        if prompt_text:
            parts.append('<div class="prompt">')
            parts.append(
                f'<h3><span class="role-label">User</span>{f" <span class=temporal>{escape(ts)}</span>" if ts else ""}</h3>'
            )
            text = prompt_text.strip()
            if fidelity.chars > 0 and len(text) > fidelity.chars:
                text = text[: fidelity.chars] + "..."
            parts.append(f"<p>{escape(text)}</p>")
            parts.append("</div>")

        narrative = getattr(turn, "narrative", [])
        if narrative:
            parts.append('<div class="assistant">')
            parts.append(
                f'<h3><span class="role-label">Assistant</span>{f" <span class=temporal>{escape(ts)}</span>" if ts else ""}</h3>'
            )
            emitter = HtmlEmitter()
            walk_narrative(narrative, emitter, fidelity=fidelity, tool_chars=tool_chars)
            parts.append(emitter.to_html())
            parts.append("</div>")

        parts.append("</section>")

    parts.append("</article>")
    return "\n".join(parts)


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation list as an HTML table fragment.

    Context keys:
        detail_base: str — URL prefix for detail links (e.g., "/ui/query").
            Rows get hx-get="{detail_base}?id=..." when provided,
            otherwise they're static (no htmx navigation).
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace

    if not summaries:
        return '<div class="empty-state"><div class="empty-icon">&#x2205;</div><p>No conversations found</p><p class="empty-hint">Try adjusting your filters</p></div>'

    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")
    depth = fidelity.depth

    parts: list[str] = ['<table class="conversation-list">']
    parts.append("<thead><tr>")
    parts.append('<th class="identifier">ID</th>')
    parts.append('<th class="temporal">Started</th>')
    parts.append('<th class="workspace">Workspace</th>')
    if depth >= 1:
        parts.append('<th class="model">Model</th>')
        parts.append('<th class="metric">Turns</th>')
        parts.append('<th class="metric">Tokens</th>')
        parts.append('<th class="metric">Cost</th>')
    if depth >= 3:
        parts.append('<th class="tag">Tags</th>')
    parts.append("</tr></thead>")

    parts.append("<tbody>")
    for c in summaries:
        cid = c.id[:12] if c.id else ""
        parts.append(f"<tr{_hx_detail(detail_base, c.id, shell_base)}>")
        parts.append(f'<td class="identifier">{escape(cid)}</td>')
        parts.append(f'<td class="temporal">{escape(fmt_timestamp(c.started_at))}</td>')
        parts.append(f'<td class="workspace">{escape(fmt_workspace(c.workspace_path))}</td>')
        if depth >= 1:
            model = fmt_model(c.model) if c.model else ""
            parts.append(f'<td class="model">{escape(model)}</td>')
            parts.append(f'<td class="metric">{c.prompt_count}p/{c.response_count}r</td>')
            parts.append(f'<td class="metric">{escape(fmt_tokens(c.total_tokens))}</td>')
            cost = f"${c.cost:.4f}" if c.cost else "$0.0000"
            parts.append(f'<td class="metric">{escape(cost)}</td>')
        if depth >= 3:
            tags = ", ".join(c.tags) if c.tags else ""
            parts.append(f'<td class="tag">{escape(tags)}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")

    return "\n".join(parts)


def render_search(results: list, fidelity: Fidelity, **context: Any) -> str:
    """Render search results as HTML fragments.

    Context keys:
        query: str — the search query
        mode: str — "chunks", "conversations", or "thread"
        detail_base: str — URL prefix for detail links
    """
    from siftd.output.common import truncate_text

    query = context.get("query", "")
    mode = context.get("mode", "chunks")
    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")

    parts: list[str] = []

    if mode == "conversations":
        parts.append('<section class="search-results conversations">')
        parts.append(f"<h2>Conversations for: {escape(query)}</h2>")
        parts.append('<table class="conversation-list">')
        parts.append(
            "<thead><tr>"
            '<th class="identifier">ID</th>'
            '<th class="metric">Max</th><th class="metric">Mean</th>'
            '<th class="metric">Chunks</th>'
            '<th class="temporal">Started</th><th class="workspace">Workspace</th>'
            "</tr></thead><tbody>"
        )
        for r in results:
            conv_id = r.get("conversation_id", "")
            parts.append(f"<tr{_hx_detail(detail_base, conv_id, shell_base)}>")
            parts.append(f'<td class="identifier">{escape(conv_id[:12])}</td>')
            parts.append(f'<td class="metric">{r.get("max_score", 0.0):.3f}</td>')
            parts.append(f'<td class="metric">{r.get("mean_score", 0.0):.3f}</td>')
            parts.append(f'<td class="metric">{r.get("chunk_count", 0)}</td>')
            parts.append(f'<td class="temporal">{escape(r.get("_started_at", ""))}</td>')
            parts.append(f'<td class="workspace">{escape(r.get("_workspace", ""))}</td>')
            parts.append("</tr>")
        parts.append("</tbody></table></section>")
        return "\n".join(parts)

    if mode == "thread":
        tier1 = context.get("tier1", [])
        tier2 = context.get("tier2", [])
        parts.append('<section class="search-results thread">')
        parts.append(f"<h2>Results for: {escape(query)}</h2>")

        for r in tier1:
            ws = r.get("_workspace", "")
            started = r.get("_started_at", "")
            parts.append('<article class="search-hit expanded">')
            parts.append(
                f'<header><span class="workspace">{escape(ws)}</span>'
                f' <span class="temporal">{escape(started)}</span></header>'
            )
            exchanges = r.get("_exchanges")
            if exchanges:
                for _pid, prompt_text, response_text in exchanges:
                    if prompt_text:
                        parts.append(
                            f'<blockquote class="prompt">{escape(prompt_text.strip())}</blockquote>'
                        )
                    if response_text:
                        parts.append(f'<p class="assistant">{escape(response_text.strip())}</p>')
            else:
                text = r.get("text", "").strip()
                if text:
                    parts.append(f"<p>{escape(text)}</p>")
            parts.append("</article>")

        if tier2:
            parts.append('<div class="search-more">')
            parts.append("<h3>More results</h3>")
            for r in tier2:
                conv_id = r.get("conversation_id", "")
                ws = r.get("_workspace", "")
                started = r.get("_started_at", "")
                score = r.get("score", 0.0)
                snippet = truncate_text(r.get("text", ""), 120).replace("\n", " ")
                parts.append(
                    f'<div class="search-hit compact"'
                    f'{_hx_detail(detail_base, conv_id, shell_base)}>'
                    f'<span class="identifier">{escape(conv_id[:12])}</span>'
                    f' <span class="metric">{score:.3f}</span>'
                    f' <span class="workspace">{escape(ws)}</span>'
                    f' <span class="temporal">{escape(started)}</span>'
                    f' <span class="summary">{escape(snippet)}</span>'
                    f"</div>"
                )
            parts.append("</div>")

        parts.append("</section>")
        return "\n".join(parts)

    # Chunks mode
    parts.append('<section class="search-results chunks">')
    parts.append(f"<h2>Results for: {escape(query)}</h2>")
    for r in results:
        conv_id = r.get("conversation_id", "")
        chunk_type = r.get("chunk_type", "").upper()[:8]
        score = r.get("score", 0.0)
        ws = r.get("_workspace", "")
        started = r.get("_started_at", "")

        parts.append(
            f'<article class="search-hit"'
            f'{_hx_detail(detail_base, conv_id, shell_base)}>'
        )
        parts.append(
            f'<header>'
            f'<span class="identifier">{escape(conv_id[:12])}</span>'
            f' <span class="metric">{score:.3f}</span>'
            f' <span class="adapter">[{escape(chunk_type)}]</span>'
            f' <span class="temporal">{escape(started)}</span>'
            f' <span class="workspace">{escape(ws)}</span>'
            f"</header>"
        )

        chars = fidelity.chars
        if chars == 0 and fidelity.depth < 2:
            chars = 200
        text = r.get("text", "")
        if chars > 0:
            text = truncate_text(text, chars)
        parts.append(f'<div class="excerpt">{escape(text)}</div>')
        parts.append("</article>")

    parts.append("</section>")
    return "\n".join(parts)
