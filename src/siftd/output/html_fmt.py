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

from siftd.output._id_format import short_id

if TYPE_CHECKING:
    from painted import Fidelity

FORMATTER_INTERFACE_VERSION = 1
name = "html"
media_type = "text/html"


def _hx_detail(detail_base: str, value: str, shell_base: str = "", *, key: str = "id") -> str:
    """Build htmx attributes that mount a surface into ``#main``, or "" if no base.

    Rows mount into ``#main`` — the Swiss shell's single swap target (the old
    two-pane ``#detail`` is gone, so anything still targeting it is a dead click).
    ``key`` is the query param: ``id`` for a conversation folio, ``tag`` for a
    tag-filtered Find. ``value`` is URL-encoded with ``quote`` (not html-escape)
    so a value carrying ``:``/``&``/spaces yields a valid query string — quote's
    output is also attribute-safe, so the whole attribute stays html-safe. (For
    ULIDs quote and escape are identical, so existing ``?id=`` callers are
    unaffected.)
    """
    from urllib.parse import quote as _q

    if not detail_base:
        return ""
    qv = _q(value)
    push = f' hx-push-url="{escape(shell_base)}?{key}={qv}"' if shell_base else ""
    return (
        f' hx-get="{escape(detail_base)}?{key}={qv}"'
        f' hx-target="#main" hx-swap="innerHTML"'
        f'{push}'
    )


# ---------------------------------------------------------------------------
# Render methods — OutputFormat protocol
# ---------------------------------------------------------------------------


def _render_export_links(export_base: str, conv_id: str) -> str:
    """Render md/json download links for one conversation."""
    if not export_base:
        return ""
    return (
        f'<div class="export-actions">'
        f'<a href="{escape(export_base)}?id={escape(conv_id)}&format=md"'
        f' class="export-link" download>.md</a>'
        f'<a href="{escape(export_base)}?id={escape(conv_id)}&format=json"'
        f' class="export-link" download>.json</a>'
        f'</div>'
    )


def _render_tag_section(
    conv_id: str,
    tags: list[str],
    interactive: bool = False,
    *,
    tag_action_url: str = "",
    tag_suggest_url: str = "",
) -> str:
    """Render the tag section for a conversation detail header.

    When interactive=True, tags get × remove buttons and a + add input.
    The section has a stable ID for htmx fragment swaps.
    Route URLs are passed via parameters — formatters must not hardcode routes.
    """
    section_id = f"tags-{short_id(conv_id)}"
    parts = [f'<div class="tag-section" id="{escape(section_id)}">']

    for tag in tags:
        if interactive and tag_action_url:
            import json as _json
            vals = _json.dumps({"action": "remove", "id": conv_id, "tag": tag})
            parts.append(
                f'<span class="tag interactive">{escape(tag)}'
                f'<button class="tag-remove"'
                f' hx-post="{escape(tag_action_url)}"'
                f' hx-vals="{escape(vals)}"'
                f' hx-target="#{escape(section_id)}" hx-swap="outerHTML"'
                f' title="Remove tag">\xd7</button>'
                f'</span>'
            )
        else:
            parts.append(f'<span class="tag">{escape(tag)}</span>')

    if interactive and tag_action_url:
        input_id = f"tag-input-{short_id(conv_id)}"
        list_id = f"tag-suggest-{short_id(conv_id)}"
        parts.append(
            f'<form class="tag-add" hx-post="{escape(tag_action_url)}"'
            f' hx-target="#{escape(section_id)}"'
            f' hx-swap="outerHTML">'
            f'<input type="hidden" name="action" value="apply">'
            f'<input type="hidden" name="id" value="{escape(conv_id)}">'
            f'<input type="text" name="tag" id="{escape(input_id)}"'
            f' list="{escape(list_id)}" class="tag-input"'
            f' placeholder="add tag\u2026"'
            f' autocomplete="off"'
            f' hx-get="{escape(tag_suggest_url)}"'
            # `input` (not keyup[key!='Enter']): fires only on value change, so
            # Enter never refires the suggest fetch — and htmx event filters
            # compile via new Function, which the CSP's missing 'unsafe-eval'
            # blocks (see tests/architecture/test_csp_fitness.py).
            f' hx-trigger="focus, input changed delay:200ms"'
            f' hx-target="#{escape(list_id)}" hx-swap="innerHTML"'
            f' hx-include="this">'
            f'<datalist id="{escape(list_id)}"></datalist>'
            f'</form>'
        )

    parts.append('</div>')
    return "\n".join(parts)


def render_tag_section(
    conv_id: str,
    tags: list[str],
    *,
    tag_action_url: str = "",
    tag_suggest_url: str = "",
) -> str:
    """Public entry point for rendering an interactive tag section fragment.

    Used by the tag mutation route to return the updated tag section.
    """
    return _render_tag_section(
        conv_id, tags, interactive=True,
        tag_action_url=tag_action_url, tag_suggest_url=tag_suggest_url,
    )


def render_detail(result: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation detail as an HTML fragment.

    Args:
        result: ConversationDetail object, or raw turns list (backward compat).

    Context keys:
        turns: override which turns to render (default: result.turns)
        tool_chars: int
        no_header: bool
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens
    from siftd.output.narrative import HtmlEmitter, walk_narrative

    if hasattr(result, "turns"):
        detail = result
        turns = context.get("turns", detail.turns)
    else:
        turns = result
        detail = context.get("detail")
    no_header = context.get("no_header", False)
    tool_chars = context.get("tool_chars", 0)

    parts: list[str] = ['<article class="conversation-detail">']

    if detail and not no_header:
        detail_id = getattr(detail, "id", "") or ""

        parts.append('<header class="conversation-header">')

        # Sticky info bar — date · model · tokens · id
        # Matches list table column vocabulary
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
        meta.append(f'<span class="identifier">{escape(detail_id)}</span>')

        parts.append(f'<div class="detail-info-bar">{" ".join(meta)}</div>')

        # Row 3: tags
        tags = getattr(detail, "tags", None) or []
        interactive = context.get("interactive_tags", False)
        parts.append(_render_tag_section(
            detail_id, tags, interactive,
            tag_action_url=context.get("tag_action_url", ""),
            tag_suggest_url=context.get("tag_suggest_url", ""),
        ))

        parts.append("</header>")

    total_turns = len(turns)
    for i, turn in enumerate(turns):
        turn_id = f"turn-{i}"
        ts = fmt_timestamp(getattr(turn, "timestamp", None), time_only=True)
        summary_ts = f' <span class="temporal">{escape(ts)}</span>' if ts else ""

        # Nav buttons — anchored to the turn wrapper
        nav = '<span class="turn-nav">'
        if i > 0:
            nav += f'<a href="#turn-{i - 1}" class="turn-nav-btn" title="Previous turn">\u2191</a>'
        if i < total_turns - 1:
            nav += f'<a href="#turn-{i + 1}" class="turn-nav-btn" title="Next turn">\u2193</a>'
        nav += "</span>"

        parts.append(f'<section class="turn" id="{turn_id}">')

        prompt_text = getattr(turn, "prompt_text", None)
        if prompt_text:
            preview_text = prompt_text.strip().replace("\n", " ")[:80]
            if len(prompt_text.strip()) > 80:
                preview_text += "\u2026"
            preview = f' <span class="turn-preview">{escape(preview_text)}</span>'

            parts.append('<details class="turn-block prompt-block" open>')
            parts.append(
                f'<summary><span class="role-label">User</span>'
                f'{summary_ts}{preview}{nav}</summary>'
            )
            parts.append('<div class="prompt">')
            text = prompt_text.strip()
            if fidelity.chars > 0 and len(text) > fidelity.chars:
                text = text[: fidelity.chars] + "..."
            parts.append(f"<p>{escape(text)}</p>")
            parts.append("</div>")
            parts.append("</details>")

        narrative = getattr(turn, "narrative", [])
        if narrative:
            # If no prompt, nav goes on the response summary
            response_nav = nav if not prompt_text else ""
            parts.append('<details class="turn-block response-block" open>')
            parts.append(
                f'<summary><span class="role-label">Assistant</span>'
                f'{summary_ts}{response_nav}</summary>'
            )
            parts.append('<div class="assistant">')
            emitter = HtmlEmitter()
            walk_narrative(narrative, emitter, fidelity=fidelity, tool_chars=tool_chars)
            parts.append(emitter.to_html())
            parts.append("</div>")
            parts.append("</details>")

        elif getattr(turn, "response_text", None):
            response_nav = nav if not prompt_text else ""
            parts.append('<details class="turn-block response-block" open>')
            parts.append(
                f'<summary><span class="role-label">Assistant</span>'
                f'{summary_ts}{response_nav}</summary>'
            )
            parts.append('<div class="assistant">')
            text = turn.response_text.strip()
            if fidelity.chars > 0 and len(text) > fidelity.chars:
                text = text[: fidelity.chars] + "..."
            parts.append(f"<p>{escape(text)}</p>")
            parts.append("</div>")
            parts.append("</details>")

        parts.append("</section>")

    parts.append("</article>")
    return "\n".join(parts)


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation list as an HTML table fragment.

    Context keys:
        detail_base: str — URL prefix for detail links (e.g., "/query").
            Rows get hx-get="{detail_base}?id=..." when provided,
            otherwise they're static (no htmx navigation).

    Cost rendering: `None` → "?" (class "metric missing"), `0` → "$0.0000"
    (truly free), otherwise the dollar amount. The caveat layer explains
    *why* a cost is unknown; the renderer is only responsible for not
    lying about it.
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace

    if not summaries:
        # Reuse the canonical .empty primitive — the bespoke .empty-state/
        # .empty-icon/.empty-hint classes have no skin rule and rendered naked.
        return '<p class="empty">No conversations found. Try adjusting your filters.</p>'

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
    if depth >= 3:
        parts.append('<th class="metric">Cost</th>')
        parts.append('<th class="tag">Tags</th>')
    parts.append("</tr></thead>")

    parts.append("<tbody>")
    for c in summaries:
        cid = short_id(c.id) if c.id else ""
        parts.append(f"<tr{_hx_detail(detail_base, c.id, shell_base)}>")
        parts.append(f'<td class="identifier">{escape(cid)}</td>')
        parts.append(f'<td class="temporal">{escape(fmt_timestamp(c.started_at))}</td>')
        parts.append(f'<td class="workspace">{escape(fmt_workspace(c.workspace_path))}</td>')
        if depth >= 1:
            model = fmt_model(c.model) if c.model else ""
            parts.append(f'<td class="model">{escape(model)}</td>')
            parts.append(f'<td class="metric">{c.prompt_count}</td>')
            parts.append(f'<td class="metric">{escape(fmt_tokens(c.total_tokens))}</td>')
        if depth >= 3:
            if c.cost is None:
                parts.append('<td class="metric missing">?</td>')
            else:
                parts.append(f'<td class="metric">${c.cost:.4f}</td>')
            tags = ", ".join(c.tags) if c.tags else ""
            parts.append(f'<td class="tag">{escape(tags)}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")

    return "\n".join(parts)


def render_search(results: list, fidelity: Fidelity, **context: Any) -> str:
    """Render search results as HTML fragments.

    Context keys:
        query: str — the search query
        mode: str — resolved search engine that ran: "fts", "semantic", or "hybrid"
        view: str — render shape: "chunks" (default), "conversations", or "thread"
        detail_base: str — URL prefix for detail links
        caveats: list[Finding] — threaded from dispatch; appended as an
            ``<aside class="caveats">`` fragment after the results section.
    """
    from siftd.output.common import truncate_text

    query = context.get("query", "")
    view = context.get("view", "chunks")
    engine = context.get("mode")
    engine_tag = f' <span class="engine">[{escape(engine)}]</span>' if engine else ""
    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")
    caveats = context.get("caveats") or []

    parts: list[str] = []

    if view == "conversations":
        parts.append('<section class="search-results conversations">')
        parts.append(f"<h2>Conversations for: {escape(query)}{engine_tag}</h2>")
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
            parts.append(f'<td class="identifier">{escape(short_id(conv_id))}</td>')
            parts.append(f'<td class="metric">{r.get("max_score", 0.0):.3f}</td>')
            parts.append(f'<td class="metric">{r.get("mean_score", 0.0):.3f}</td>')
            parts.append(f'<td class="metric">{r.get("chunk_count", 0)}</td>')
            parts.append(f'<td class="temporal">{escape(r.get("_started_at", ""))}</td>')
            parts.append(f'<td class="workspace">{escape(r.get("_workspace", ""))}</td>')
            parts.append("</tr>")
        parts.append("</tbody></table></section>")

    elif view == "thread":
        tier1 = context.get("tier1", [])
        tier2 = context.get("tier2", [])
        parts.append('<section class="search-results thread">')
        parts.append(f"<h2>Results for: {escape(query)}{engine_tag}</h2>")

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
                    f'<span class="identifier">{escape(short_id(conv_id))}</span>'
                    f' <span class="metric">{score:.3f}</span>'
                    f' <span class="workspace">{escape(ws)}</span>'
                    f' <span class="temporal">{escape(started)}</span>'
                    f' <span class="summary">{escape(snippet)}</span>'
                    f"</div>"
                )
            parts.append("</div>")

        parts.append("</section>")

    else:
        # Chunks view
        parts.append('<section class="search-results chunks">')
        parts.append(f"<h2>Results for: {escape(query)}{engine_tag}</h2>")
        if not results:
            parts.append('<p class="empty">No matches.</p>')
        for r in results:
            conv_id = r.get("conversation_id", "")
            display_label = r["display_label"]
            score = r.get("score", 0.0)
            ws = r.get("_workspace", "")
            started = r.get("_started_at", "")

            parts.append(
                f'<article class="search-hit"'
                f'{_hx_detail(detail_base, conv_id, shell_base)}>'
            )
            parts.append(
                f'<header>'
                f'<span class="identifier">{escape(short_id(conv_id))}</span>'
                f' <span class="metric">{score:.3f}</span>'
                f' <span class="adapter">[{escape(display_label)}]</span>'
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

    if caveats:
        parts.append('<aside class="caveats">')
        for c in caveats:
            parts.append(f'<p class="caveat">{escape(c.message)}</p>')
        parts.append("</aside>")

    return "\n".join(parts)


def render_stats(stats: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render stats dashboard as an HTML fragment.

    Args:
        stats: DatabaseStats object from get_stats().

    Context keys:
        usage: UsageSummary — aggregate token/cost totals
        cost_coverage_pct: int — percentage of conversations with cost data
        by_model: list[GroupUsage] — token/cost breakdown by model
        by_workspace: list[GroupUsage] — token/cost breakdown by workspace
    """
    from siftd.output.common import fmt_tokens, fmt_workspace

    usage = context.get("usage")
    cost_coverage = context.get("cost_coverage_pct", 0)
    by_model = context.get("by_model", [])
    by_workspace = context.get("by_workspace", [])

    parts: list[str] = ['<article class="stats-dashboard">']
    parts.append("<h2>Stats</h2>")

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

    # Aggregate token/cost totals
    if usage is not None:
        total_tokens = usage.total_input_tokens + usage.total_output_tokens
        parts.append('<div class="stats-grid">')
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">{fmt_tokens(total_tokens)}</div>'
            f'<div class="stat-label">Total tokens</div></div>'
        )
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">{fmt_tokens(usage.total_input_tokens)}</div>'
            f'<div class="stat-label">Input tokens</div></div>'
        )
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">{fmt_tokens(usage.total_output_tokens)}</div>'
            f'<div class="stat-label">Output tokens</div></div>'
        )
        parts.append(
            f'<div class="stat-card">'
            f'<div class="stat-value">${usage.total_cost:.2f}</div>'
            f'<div class="stat-label">Cost tracked ({cost_coverage}% coverage)</div></div>'
        )
        parts.append("</div>")

    # By model
    if by_model:
        parts.append("<h3>By model</h3>")
        parts.append('<table class="conversation-list">')
        parts.append(
            "<thead><tr>"
            "<th>Model</th><th>Conversations</th>"
            "<th>Input</th><th>Output</th><th>Total</th>"
            "</tr></thead><tbody>"
        )
        for g in by_model:
            tok = g.input_tokens + g.output_tokens
            parts.append(
                f"<tr>"
                f'<td class="model">{escape(g.name)}</td>'
                f'<td class="metric">{g.conversations:,}</td>'
                f'<td class="metric">{fmt_tokens(g.input_tokens)}</td>'
                f'<td class="metric">{fmt_tokens(g.output_tokens)}</td>'
                f'<td class="metric">{fmt_tokens(tok)}</td>'
                f"</tr>"
            )
        parts.append("</tbody></table>")

    # By workspace
    if by_workspace:
        parts.append("<h3>By workspace</h3>")
        parts.append('<table class="conversation-list">')
        parts.append(
            "<thead><tr>"
            "<th>Workspace</th><th>Conversations</th>"
            "<th>Tokens</th><th>Cost</th>"
            "</tr></thead><tbody>"
        )
        for g in by_workspace:
            tok = g.input_tokens + g.output_tokens
            parts.append(
                f"<tr>"
                f'<td class="workspace">{escape(fmt_workspace(g.name))}</td>'
                f'<td class="metric">{g.conversations:,}</td>'
                f'<td class="metric">{fmt_tokens(tok)}</td>'
                f'<td class="metric">${g.cost:.4f}</td>'
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
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Swiss "folio" — conversation transcript as a three-region grid
# ---------------------------------------------------------------------------


def _md_to_html(content: str) -> str:
    """Render message content as HTML (markdown via mistune, escape=True).

    Used for both user prompts and assistant prose: a spawned sub-agent's
    "user" turn is an orchestration-authored markdown document (headers, lists,
    fenced code), so user turns get the same markdown treatment as the
    assistant narrative. escape=True keeps it XSS-safe — raw HTML and
    javascript: links are neutralized — which is the real control here since
    the serve CSP allows 'unsafe-inline' scripts. Do not swap in a renderer
    that passes raw HTML through.
    """
    try:
        import mistune

        md = mistune.create_markdown(escape=True)
        rendered = md(content)
        # mistune's type is str | list[...]; a string input yields a string.
        return rendered if isinstance(rendered, str) else "\n".join(map(str, rendered))
    except ImportError:
        out: list[str] = []
        for para in content.split("\n\n"):
            stripped = para.strip()
            if stripped:
                out.append(f"<p>{escape(stripped)}</p>")
        return "".join(out)


class _FolioEmitter:
    """Narrative emitter for the folio body: prose + code only.

    Tool I/O is deliberately NOT inlined here — the folio surfaces tools in the
    right-hand ledger and the per-turn ``.turn__tools`` chip, so the body stays
    readable prose. The tool_* methods are intentional no-ops; the ledger is
    built separately from ``turn.tool_call_summaries``.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []

    def text(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self.parts.append(_md_to_html(content))

    def thinking(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self.parts.append(
            f'<details class="turn-think"><summary>thinking</summary>'
            f"<pre>{escape(content)}</pre></details>"
        )

    def thinking_placeholder(self, *, event_id: str | None = None) -> None:
        del event_id

    def tool_summary(self, tools: list) -> None:
        del tools

    def tool_content(
        self, name: str, count: int, raw_input: str | None,
        raw_result: str | None, status: str | None, *,
        event_id: str | None = None, tool_call_id: str | None = None,
    ) -> None:
        del name, count, raw_input, raw_result, status, event_id, tool_call_id

    def tool_output(self, block_type: str, content: str, *, event_id: str | None = None) -> None:
        del block_type, content, event_id

    def to_html(self) -> str:
        return "\n".join(self.parts)


def _folio_rail_item(n: int, role: str, label: str, time: str, anchor: str) -> str:
    return (
        f'<a class="turn-item" data-role="{role}" href="#{anchor}">'
        f'<span class="turn-item__n">{n:02d}</span>'
        f'<span class="turn-item__role">{label}</span>'
        f'<span class="turn-item__t">{escape(time)}</span></a>'
    )


def folio_detail_from_session(detail: Any) -> Any:
    """Project a peek ``SessionDetail`` onto the folio's duck shape.

    Follow is not a separate view — it is the folio rendered from a live
    source. ``PeekExchange`` already carries what a folio turn reads
    (``prompt_text``/``narrative``/``timestamp``); this maps the remaining
    gap: ``tool_calls`` tuples become ``tool_call_summaries`` objects, token
    sums fold up, and cost stays ``None`` (no priced usage pre-ingest — the
    foot renders an em dash, same rule as the DB path).
    """
    from types import SimpleNamespace

    turns: list[Any] = []
    input_tokens = 0
    output_tokens = 0
    for ex in getattr(detail, "exchanges", []) or []:
        turns.append(SimpleNamespace(
            timestamp=ex.timestamp,
            prompt_text=ex.prompt_text,
            narrative=ex.narrative,
            tool_call_summaries=[
                SimpleNamespace(tool_name=name, count=count)
                for name, count in (ex.tool_calls or [])
            ],
        ))
        input_tokens += ex.input_tokens or 0
        output_tokens += ex.output_tokens or 0
    return SimpleNamespace(
        id=detail.info.session_id,
        turns=turns,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        cost=None,
        tags=[],
    )


def render_folio(detail: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render a conversation as the Swiss 'folio' fragment.

    Three CSS-grid regions, all over data ``get_conversation`` already returns:
      - ``.folio__nav``    turn-rail (anchor links into the body; ``:target``
                           highlights the landed turn — no JS scroll-spy needed)
      - ``.folio__body``   user prompts + assistant prose (markdown); tool I/O is
                           NOT inlined (the ledger owns it)
      - ``.folio__ledger`` a ``Counter`` over ``turn.tool_call_summaries`` —
                           pure render-layer, no new data

    A fourth region, ``.folio__foot`` (tokens · cost · tags · export), sits
    under the ledger column when wide; on a narrow container the grid reflows
    to a body-on-top layout with a nav · ledger · foot footer band (container
    query — pure CSS, no markup difference between the layouts).

    The fragment root carries ``data-view/title/count/kick`` so ``enhance.js``
    updates the chrome head + active nav on an htmx swap without oob coupling.
    The foot shows tokens + cost; cost is the rollup's canonical
    per-conversation value (``ConversationDetail.cost``, fetched at depth>=3),
    rendered as ``&mdash;`` when ``None`` (no priced usage) — never a fabricated
    $0 that would re-introduce the mispricing the rollup work removed. The tool
    total lives in the ledger header (``folio__navmeta``).

    Context keys (all optional — the folio is the single detail surface, so it
    hosts the tag/export affordances the two-pane detail used to carry):
        interactive_tags: bool — tag pills get × remove + an add input
        tag_action_url / tag_suggest_url: routes for the tag form (no hardcoding)
        export_base_url: route for md/json download links
        view / title / kick: chrome overrides — follow renders the folio under
            the Sessions view, not Transcript
        live_poll_url: marks the folio live — the article root self-refreshes
            (htmx outerHTML swap) from this URL, and curation is suppressed
            (tags/export need ingest; a live session has neither)
    """
    from collections import Counter

    from siftd.output.common import fmt_timestamp, fmt_tokens
    from siftd.output.narrative import walk_narrative

    turns = getattr(detail, "turns", []) or []
    conv_id = getattr(detail, "id", "") or ""
    short = short_id(conv_id) if conv_id else ""

    rail: list[str] = []
    body: list[str] = []
    tool_counter: Counter[str] = Counter()
    n = 0

    for turn in turns:
        t_time = fmt_timestamp(getattr(turn, "timestamp", None), time_only=True) or ""
        prompt_text = getattr(turn, "prompt_text", None)
        narrative = getattr(turn, "narrative", []) or []
        summaries = getattr(turn, "tool_call_summaries", []) or []

        if prompt_text:
            n += 1
            anchor = f"t-{n}"
            rail.append(_folio_rail_item(n, "user", "User", t_time, anchor))
            body.append(
                f'<div class="turn" data-role="user" id="{anchor}">'
                f'<header class="turn__head"><span class="turn__role">User</span>'
                f'<span class="turn__time">{escape(t_time)}</span></header>'
                f'<div class="turn__text">{_md_to_html(prompt_text)}</div>'
                f"</div>"
            )

        if narrative:
            n += 1
            anchor = f"t-{n}"
            tool_parts: list[str] = []
            for s in summaries:
                tool_counter[s.tool_name] += s.count
                lbl = escape(s.tool_name)
                if s.count > 1:
                    lbl += f"&times;{s.count}"
                tool_parts.append(lbl)
            tools_html = (
                f'<span class="turn__tools">{" &middot; ".join(tool_parts)}</span>'
                if tool_parts else ""
            )
            emitter = _FolioEmitter()
            walk_narrative(narrative, emitter, fidelity=fidelity, tool_chars=0)
            rail.append(_folio_rail_item(n, "assistant", "Assistant", t_time, anchor))
            body.append(
                f'<div class="turn" data-role="assistant" id="{anchor}">'
                f'<header class="turn__head"><span class="turn__role">Assistant</span>'
                f'<span class="turn__time">{escape(t_time)}</span>{tools_html}</header>'
                f'<div class="turn__text">{emitter.to_html()}</div>'
                f"</div>"
            )

    total_tokens = (
        getattr(detail, "total_input_tokens", 0) + getattr(detail, "total_output_tokens", 0)
    )
    total_tools = sum(tool_counter.values())
    # Cost is the rollup's canonical per-conversation value, fetched at depth>=3.
    # None means no priced usage — render an em dash, never a fabricated $0.
    cost = getattr(detail, "cost", None)
    cost_str = f"${cost:.4f}" if cost is not None else "&mdash;"

    ledger_rows: list[str] = []
    for name, cnt in tool_counter.most_common():
        ledger_rows.append(
            f'<li class="ledger__row"><span class="ledger__name">{escape(name)}</span>'
            f'<span class="ledger__bar" data-n="{cnt}"></span>'
            f'<span class="ledger__n">{cnt}</span></li>'
        )
    if not ledger_rows:
        ledger_rows.append(
            '<li class="ledger__row ledger__empty">'
            '<span class="ledger__name">no tool calls</span></li>'
        )

    # The count is rail items (each User / Assistant message is a turn), so the
    # "Turns N" header matches the rail length — not the exchange count, which
    # would under-report by ~half (one exchange renders two turns).
    turn_count = n
    kick = context.get("kick") or (f"{escape(short)} · folio" if short else "folio")
    view = context.get("view", "transcript")
    title = context.get("title", "Transcript")
    live_poll_url = context.get("live_poll_url", "")

    # Curation: tags + export, hosted in the folio foot. The /tag route
    # swaps the same stable #tags-<id> section render_tag_section returns.
    curation = ""
    if conv_id and not live_poll_url:
        tags = getattr(detail, "tags", None) or []
        tag_html = _render_tag_section(
            conv_id, tags, context.get("interactive_tags", False),
            tag_action_url=context.get("tag_action_url", ""),
            tag_suggest_url=context.get("tag_suggest_url", ""),
        )
        export_html = _render_export_links(context.get("export_base_url", ""), conv_id)
        curation = f'<div class="ledger__curation">{tag_html}{export_html}</div>'

    live_attrs = (
        f' hx-get="{escape(live_poll_url)}" hx-trigger="every 2s"'
        f' hx-swap="outerHTML"'
        if live_poll_url else ""
    )
    folio_cls = "folio folio--live" if live_poll_url else "folio"
    parts: list[str] = [
        f'<article class="{folio_cls}" data-view="{escape(view)}"'
        f' data-title="{escape(title)}"'
        f' data-count="{turn_count}" data-kick="{kick}"{live_attrs}>',
        '<nav class="folio__nav" aria-label="Turns">',
        '<div class="folio__navhead"><span class="micro">Turns</span>'
        f'<span class="folio__navmeta">{turn_count}</span></div>',
        f'<div class="turns">{"".join(rail)}</div>',
        "</nav>",
        '<div class="folio__body">',
        "".join(body) or '<p class="empty">This conversation has no turns.</p>',
        "</div>",
        '<aside class="folio__ledger" aria-label="Tool ledger">',
        '<div class="folio__navhead"><span class="micro">Tool ledger</span>'
        f'<span class="folio__navmeta">{total_tools}</span></div>',
        f'<ul class="ledger">{"".join(ledger_rows)}</ul>',
        "</aside>",
        # Foot is its own grid area, not part of the ledger aside: wide layouts
        # pin it under the ledger column; narrow ones reflow it into the footer
        # band (nav · ledger · foot) so the conversation stays front and center.
        '<footer class="folio__foot">',
        '<div class="folio__stats">',
        '<div class="ledger__stat"><span class="micro">Tokens</span>'
        f'<span class="ledger__statn">{escape(fmt_tokens(total_tokens))}</span></div>',
        '<div class="ledger__stat"><span class="micro">Cost</span>'
        f'<span class="ledger__statn">{cost_str}</span></div>',
        "</div>",
        curation,
        "</footer>",
        "</article>",
    ]
    return "".join(parts)


def _ago(epoch: float | None) -> str:
    """Humanize seconds-since-epoch as a compact age ('2m', '1h 14m', '3d')."""
    import time as _time

    if not epoch:
        return ""
    delta = max(0, int(_time.time() - epoch))
    if delta < 60:
        return f"{delta}s"
    minutes, hours, days = delta // 60, delta // 3600, delta // 86400
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h {minutes % 60:02d}m"
    return f"{minutes}m"


def _iso_epoch(ts: str | None) -> float | None:
    from datetime import datetime

    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _hour_hist(hours: list[int]) -> str:
    """24 hour-of-day buckets as .hist spans; enhance.js scales heights from
    data-n (CSSOM writes only — no inline style attributes under the CSP)."""
    buckets = [0] * 24
    for h in hours:
        if 0 <= h < 24:
            buckets[h] += 1
    spans = "".join(f'<span data-n="{n}"></span>' for n in buckets)
    return f'<span class="hist" aria-hidden="true">{spans}</span>'


def render_sessions(live: list, summaries: list, **context: Any) -> str:
    """Render the Swiss 'Sessions' view: a Live zone over an Ingested timeline.

    Live zone — peek scan results (server-local session files). Rendered only
    when ``context['live_enabled']`` (the serve layer threads the F7
    ``allow_live_endpoints`` policy through; on a public bind the /follow
    route isn't even registered, so the cards must not render either).
    Cards show scan-level fields only: workspace [branch] · model · adapter ·
    exchanges · started/active age. No tokens, no cost — the scan's token
    convention is harness-reported (cache-exclusive for Anthropic), and a live
    number that jumps ~40× after ingest inside the same view would
    re-introduce the undercount class the v10 rollup work removed.

    Ingested zone — ConversationSummary rows grouped by start day, newest
    first. Day heads carry totals (sessions · tokens · cost) and an
    hour-of-day histogram; all derived render-side from started_at /
    total_tokens / cost — no new data surface. Cost honesty as everywhere:
    sum of priced rows, ``&mdash;`` when nothing in the day is priced.
    Rows mount the folio via the same _hx_detail contract as Find. Sub-agent
    conversations (external_id ``<root>::agent::<id>``) nest under their parent
    session, collapsed by default and expanded from the parent's disclosure
    button (enhance.js); their tokens/cost still fold into the day totals.
    """
    from collections import OrderedDict
    from datetime import datetime

    from siftd.output.common import fmt_model, fmt_tokens

    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")
    follow_base = context.get("follow_base", "")
    live_enabled = context.get("live_enabled", False)

    parts: list[str] = []

    # --- live zone ---------------------------------------------------------
    if live_enabled:
        cards: list[str] = []
        for s in live:
            ws = getattr(s, "workspace_name", None) or "?"
            branch = getattr(s, "branch", None)
            ws_label = f"{ws} [{branch}]" if branch else ws
            model = fmt_model(getattr(s, "model", None)) or ""
            adapter = getattr(s, "adapter_name", None) or ""
            meta_bits = [b for b in (model, adapter) if b]
            started = _iso_epoch(getattr(s, "started_at", None))
            age = _ago(started)
            active = _ago(getattr(s, "last_activity", None))
            when = f"started {age} ago" if age else (f"active {active} ago" if active else "")
            sid = getattr(s, "session_id", "")
            hx = ""
            if follow_base and sid:
                from urllib.parse import quote as _q

                hx = (
                    f' hx-get="{escape(follow_base)}?sid={_q(sid)}"'
                    f' hx-target="#main" hx-swap="innerHTML"'
                    f' hx-push-url="{escape(shell_base)}?follow={_q(sid)}"'
                )
            cards.append(
                f'<li class="card card--live"{hx}>'
                f'<span class="pulse" aria-hidden="true"></span>'
                f'<div class="card__ws">{escape(ws_label)}</div>'
                f'<div class="card__meta">{escape(" · ".join(meta_bits))}'
                f'{(" · " + escape(when)) if when and meta_bits else escape(when)}</div>'
                f'<div class="card__nums">'
                f'<span class="stat"><span class="stat__n">{getattr(s, "exchange_count", 0)}</span>'
                f'<span class="micro">exchanges</span></span>'
                f"</div></li>"
            )
        live_body = (
            f'<ul class="cards">{"".join(cards)}</ul>'
            if cards
            else '<p class="zone__empty">no live sessions on this host</p>'
        )
        parts.append(
            '<section class="zone zone--live" aria-label="Live sessions">'
            '<div class="zone__head"><span class="micro">Live</span>'
            f'<span class="zone__count">{len(cards)} active</span>'
            '<span class="zone__rule"></span></div>'
            f"{live_body}</section>"
        )

    # --- ingested timeline ---------------------------------------------------
    # Sub-agent conversations carry their root session in external_id
    # ("claude_code::<uuid>::agent::<id>" -> root "claude_code::<uuid>"), so
    # list_conversations derives parent_external_id render-side already. Nest
    # them under their parent row instead of scattering them through the
    # timeline. Pure render-layer: no schema column, no extra query.
    present = {
        c.external_id for c in summaries if getattr(c, "external_id", None)
    }
    children: dict[str, list] = {}
    roots: list = []
    for c in summaries:
        parent_ext = getattr(c, "parent_external_id", None)
        if parent_ext and parent_ext in present:
            children.setdefault(parent_ext, []).append(c)
        else:
            # Top-level session, or an orphan sub-agent whose parent fell outside
            # this page (n=50) — render at top level, flagged as a sub-agent.
            roots.append(c)

    def _session_row(c, *, agents: int = 0, group_id=None, parent_id=None, flagged=False) -> str:
        # group_id  -> expandable parent: disclosure button + agent-count chip.
        # parent_id -> nested sub-agent: hidden by default, data-parent link
        #              (enhance.js toggles it from the parent's button).
        # flagged   -> orphan sub-agent (parent off the n=50 page): sub styling,
        #              but visible, since it has no parent row to hang under.
        model = fmt_model(getattr(c, "model", None)) or ""
        ws = getattr(c, "workspace_path", None)
        ws_name = ws.rstrip("/").rsplit("/", 1)[-1] if ws else "?"
        row_cost = getattr(c, "cost", None)
        row_cost_str = f"${row_cost:,.2f}" if row_cost is not None else "&mdash;"
        is_sub = parent_id is not None or flagged
        if is_sub:
            # Children inherit the parent's workspace, so repeating its name on
            # every sibling is pure noise. Identify a child by what differs: its
            # agent type (when captured from the sidecar) and its spawn
            # clock-time (always present — distinguishes siblings, adds order).
            atype = (getattr(c, "agent_type", None) or "").strip()
            if ":" in atype:  # feature-dev:code-reviewer -> code-reviewer
                atype = atype.rsplit(":", 1)[-1]
            e = _iso_epoch(getattr(c, "started_at", None))
            when = datetime.fromtimestamp(e).strftime("%H:%M") if e is not None else ""
            time_html = f'<span class="row__when">{when}</span>' if when else ""
            label = escape(atype)
            name_html = f"{label} {time_html}".strip() if label else (time_html or "sub-agent")
        else:
            name_html = escape(ws_name)
        cls = "row row--sub" if is_sub else "row"
        attrs = _hx_detail(detail_base, c.id, shell_base)
        if parent_id is not None:
            attrs += f' data-parent="{escape(parent_id)}" hidden'
        if group_id is not None:
            disc = (
                f'<button class="row__toggle" type="button" data-group="{escape(group_id)}"'
                f' aria-expanded="false" aria-label="Toggle sub-agents"></button>'
            )
        elif is_sub:
            disc = '<span class="row__caret" aria-hidden="true">&#8627;</span>'
        else:
            disc = '<span class="row__caret row__caret--none" aria-hidden="true"></span>'
        chip = (
            f'<span class="row__agents">{agents} agent{"" if agents == 1 else "s"}</span>'
            if agents else ""
        )
        return (
            f'<li class="{cls}"{attrs}>'
            f'<span class="row__ws">{disc}'
            f'<span class="row__name">{name_html}</span>{chip}</span>'
            f'<span class="row__model">{escape(model)}</span>'
            f'<span class="row__turns">{getattr(c, "prompt_count", 0)}</span>'
            f'<span class="row__tok">{escape(fmt_tokens(getattr(c, "total_tokens", 0) or 0))}</span>'
            f'<span class="cost">{row_cost_str}</span>'
            f"</li>"
        )

    days: OrderedDict[str, list] = OrderedDict()
    for c in roots:
        epoch = _iso_epoch(getattr(c, "started_at", None))
        key = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d") if epoch else "unknown"
        days.setdefault(key, []).append(c)

    day_parts: list[str] = []
    for key, convs in days.items():
        # Fold each root's nested sub-agents back into the day totals, so token
        # and cost numbers still account for all work done that day.
        day_kids: list = []
        for c in convs:
            ext = getattr(c, "external_id", None)
            if ext:
                day_kids.extend(children.get(ext, []))
        in_day = convs + day_kids

        if key == "unknown":
            label = "undated"
            hist = ""
        else:
            label = datetime.strptime(key, "%Y-%m-%d").strftime("%a %d %b")
            hours = []
            for c in convs:
                e = _iso_epoch(c.started_at)
                if e is not None:
                    hours.append(datetime.fromtimestamp(e).hour)
            hist = _hour_hist(hours)
        tok = sum(getattr(c, "total_tokens", 0) or 0 for c in in_day)
        priced = [c.cost for c in in_day if getattr(c, "cost", None) is not None]
        cost_str = f"${sum(priced):,.2f}" if priced else "&mdash;"
        sub = f"{len(convs)} sessions"
        if day_kids:
            sub += f" &middot; {len(day_kids)} sub-agents"
        sub += f" &middot; {fmt_tokens(tok)} tok &middot; {cost_str}"

        rows: list[str] = []
        for c in convs:
            ext = getattr(c, "external_id", None)
            kids = children.get(ext, []) if ext else []
            if kids:
                rows.append(_session_row(c, agents=len(kids), group_id=c.id))
                for kid in kids:
                    rows.append(_session_row(kid, parent_id=c.id))
            else:
                # childless top-level session, or an orphan sub-agent (flagged)
                rows.append(
                    _session_row(c, flagged=bool(getattr(c, "parent_external_id", None)))
                )
        day_parts.append(
            f'<div class="day"><div class="day__head">'
            f'<span class="day__date">{escape(label)}</span>'
            f'<span class="day__sub">{sub}</span>{hist}</div>'
            f'<ul class="rows">{"".join(rows)}</ul></div>'
        )

    ingested = (
        "".join(day_parts)
        if day_parts
        else '<p class="empty">No ingested sessions yet.</p>'
    )
    parts.append(
        '<section class="zone zone--ingested" aria-label="Ingested sessions">'
        '<div class="zone__head"><span class="micro">Ingested</span>'
        f'<span class="zone__count">{len(summaries)} sessions</span>'
        '<span class="zone__rule"></span></div></section>'
        f"{ingested}"
    )

    kick = "live · ingested" if live_enabled else "ingested"
    return (
        f'<section class="sessions" data-view="sessions" data-title="Sessions"'
        f' data-count="{len(summaries)}" data-kick="{kick}">'
        f'{"".join(parts)}</section>'
    )


# ---------------------------------------------------------------------------
# Tags view — pinned zone + most-used zone over a namespace tree
# ---------------------------------------------------------------------------

# Order matters: the first kind with the largest count wins ties, so a tag used
# equally on conversations and tool_calls reads as a conversation tag.
_TAG_COUNT_KINDS: tuple[tuple[str, str], ...] = (
    ("conversation_count", "conv"),
    ("tool_call_count", "calls"),
    ("prompt_count", "prompts"),
    ("response_count", "resp"),
    ("exchange_count", "exch"),
    ("workspace_count", "ws"),
)


def _tag_namespace(name: str) -> tuple[str, str]:
    """Split a flat tag name into ``(namespace, leaf)`` on the first ``:``.

    The corpus uses a ``namespace:leaf`` naming convention (``shell:test``,
    ``siftd:derivative``) but stores names flat — the tree is synthesized here at
    render time, not in storage. Names with no ``:`` fall in the ``''``
    (ungrouped) namespace.
    """
    if ":" in name:
        ns, leaf = name.split(":", 1)
        return ns, leaf
    return "", name


def _tag_weight(t: Any) -> tuple[int, str]:
    """A tag's dominant usage count + its unit.

    ``list_tags`` keeps six per-grain counts separate (a tag on conversations vs
    on tool_calls). There is no single 'total' that isn't either a double-count
    or a grain-mix lie, so the row shows the DOMINANT count with its true unit —
    ``312 conv`` for a conversation tag, ``198 calls`` for a shell tool tag. The
    bar (relative magnitude within its ledger) is sized by the same number, and
    'most used' ranks by it.
    """
    best_n, best_u = 0, "conv"
    for attr, unit in _TAG_COUNT_KINDS:
        n = getattr(t, attr, 0) or 0
        if n > best_n:
            best_n, best_u = n, unit
    return best_n, best_u


def _tag_row(
    t: Any, *, display: str, list_base: str, shell_base: str, pin_action_url: str
) -> str:
    """One tag as a ``.ledger__row``: pin toggle · name (drills to Find) · bar · count.

    The drill mounts Find pre-filtered by this tag into ``#main`` (so the chrome
    + filter strip come along and the user can refine); the pin toggle posts and
    swaps the whole view back so the pinned zone updates in place. Both use the
    full tag ``name``; ``display`` is only the visible label (a leaf in the tree).
    """
    import json as _json

    name = t.name
    pinned = bool(getattr(t, "pinned", False))
    weight, unit = _tag_weight(t)

    # Drill into Find pre-filtered by this tag — reuses the shared #main-mount
    # primitive (same contract as the folio rows), keyed on ``tag``.
    drill = _hx_detail(list_base, name, shell_base, key="tag")

    pin_btn = ""
    if pin_action_url:
        vals = _json.dumps({"action": "unpin" if pinned else "pin", "tag": name})
        star = "★" if pinned else "☆"
        pin_cls = "pin pin--on" if pinned else "pin"
        verb = "Unpin" if pinned else "Pin"
        pressed = "true" if pinned else "false"
        pin_btn = (
            f'<button class="{pin_cls}" type="button"'
            f' hx-post="{escape(pin_action_url)}" hx-vals="{escape(vals)}"'
            f' hx-target="#main" hx-swap="innerHTML"'
            f' aria-pressed="{pressed}" title="{verb} {escape(name)}">{star}</button>'
        )

    return (
        f'<li class="ledger__row">'
        f"{pin_btn}"
        f'<a class="ledger__name"{drill}>{escape(display)}</a>'
        f'<span class="ledger__bar" data-n="{weight}"></span>'
        f'<span class="ledger__n">{weight}'
        f'<span class="ledger__unit"> {escape(unit)}</span></span>'
        f"</li>"
    )


def render_tags(tags: list, **context: Any) -> str:
    """Render the Swiss 'Tags' view: a pinned zone + most-used zone over a
    namespace tree.

    Composition over data: every number comes from owner-scoped
    ``api.tags.list_tags``. 'pinned' is the only stored state; the 'tree' is
    synthesized by splitting flat names on ``:`` (sibling magnitudes normalise
    within each namespace ledger, which is the meaningful comparison). Rows
    pin/unpin in place and drill into Find pre-filtered by the tag.

    Context keys: ``list_base`` (drill target, e.g. ``/find``), ``shell_base``
    (deep-link push prefix), ``pin_action_url`` (pin/unpin POST).
    """
    from collections import OrderedDict

    list_base = context.get("list_base", "")
    shell_base = context.get("shell_base", "")
    pin_action_url = context.get("pin_action_url", "")
    row_kw = {
        "list_base": list_base,
        "shell_base": shell_base,
        "pin_action_url": pin_action_url,
    }

    def _ledger(rows: list[str]) -> str:
        return f'<ul class="ledger ledger--tags">{"".join(rows)}</ul>'

    def _zone(label: str, count_txt: str, body: str, *, mod: str = "") -> str:
        zcls = f"zone {mod}" if mod else "zone"
        return (
            f'<section class="{zcls}"><div class="zone__head">'
            f'<span class="micro">{escape(label)}</span>'
            f'<span class="zone__count">{escape(count_txt)}</span>'
            f'<span class="zone__rule"></span></div>{body}</section>'
        )

    if not tags:
        return (
            '<section class="tags" data-view="tags" data-title="Tags"'
            ' data-count="0" data-kick="">'
            '<p class="empty">No tags yet.</p></section>'
        )

    parts: list[str] = []

    # pinned zone (full names) — only when something is pinned
    pinned = [t for t in tags if getattr(t, "pinned", False)]
    if pinned:
        rows = [_tag_row(t, display=t.name, **row_kw) for t in pinned]
        parts.append(_zone("Pinned", str(len(pinned)), _ledger(rows), mod="zone--pinned"))

    # most-used zone — top non-pinned tags by dominant count (skip zero-count).
    # "Most used" is a curation headline, so auto-applied vocabulary (shell:*
    # categories + siftd:derivative, flagged ``auto`` by list_tags) is demoted:
    # its tool-call grain counts in the thousands would structurally swamp the
    # tens of hand-applied conversation tags. The auto names still appear in the
    # namespace tree below — demoted from the headline, not lost.
    unpinned = [t for t in tags if not getattr(t, "pinned", False)]
    curated = [t for t in unpinned if not getattr(t, "auto", False)]
    top = [t for t in sorted(curated, key=lambda t: _tag_weight(t)[0], reverse=True)[:8]
           if _tag_weight(t)[0] > 0]
    if top:
        rows = [_tag_row(t, display=t.name, **row_kw) for t in top]
        parts.append(_zone("Most used", str(len(top)), _ledger(rows)))

    # namespace tree — every tag grouped by ':' prefix; namespaces alphabetical,
    # the ungrouped bucket last. Leaf labels in groups, full names ungrouped.
    groups: OrderedDict[str, list] = OrderedDict()
    for t in tags:
        ns, _leaf = _tag_namespace(t.name)
        groups.setdefault(ns, []).append(t)
    ordered_ns = sorted(k for k in groups if k) + ([""] if "" in groups else [])
    for ns in ordered_ns:
        members = sorted(groups[ns], key=lambda t: _tag_weight(t)[0], reverse=True)
        rows = [
            _tag_row(t, display=(_tag_namespace(t.name)[1] if ns else t.name), **row_kw)
            for t in members
        ]
        label = f"{ns}:" if ns else "ungrouped"
        parts.append(_zone(label, f"{len(members)} tags", _ledger(rows)))

    # kick tracks what actually rendered (mirrors render_sessions): "pinned"
    # only appears when a Pinned zone does.
    kick = " · ".join(filter(None, ["pinned" if pinned else "", "tree"]))
    return (
        f'<section class="tags" data-view="tags" data-title="Tags"'
        f' data-count="{len(tags):,}" data-kick="{kick}">'
        f'{"".join(parts)}</section>'
    )


def _dash_usage_rows(groups: list, *, label_fn=None, limit: int = 10) -> str:
    """Render a usage breakdown (model or workspace) as ``.ledger`` rows.

    The bar is sized by total tokens (always exact) via ``data-n`` — drawLedgers
    normalises per-list. Cost is the honest tail: ``&mdash;`` when ``None`` (no
    priced usage), never a fabricated $0. Empty input renders one muted row.
    """
    from siftd.output.common import fmt_tokens

    if not groups:
        return (
            '<li class="ledger__row ledger__empty">'
            '<span class="ledger__name">no usage</span></li>'
        )
    rows: list[str] = []
    for g in groups[:limit]:
        name = label_fn(g.name) if label_fn else g.name
        tok = (g.input_tokens or 0) + (g.output_tokens or 0)
        # 2dp for a money column — 4dp is per-conversation folio precision, far
        # too noisy on aggregates ($886.85, not $886.8454).
        cost_str = "&mdash;" if g.cost is None else f"${g.cost:,.2f}"
        rows.append(
            f'<li class="ledger__row"><span class="ledger__name">{escape(name)}</span>'
            f'<span class="ledger__bar" data-n="{tok}"></span>'
            f'<span class="ledger__n">{escape(fmt_tokens(tok))}</span>'
            f'<span class="ledger__cost">{cost_str}</span></li>'
        )
    return "".join(rows)


def render_dashboard(
    *,
    usage: Any,
    by_model: list,
    by_workspace: list,
    coverage: Any,
    stats: Any,
    owner: str | None = None,
) -> str:
    """Render the Swiss 'Stats' dashboard fragment.

    Aggregate companion to the folio. Composes the four api reads the route
    gathers — no new api type (the route is the only consumer; it dissolves into
    "call the reads, pass them here"). Regions:
      - ``.dash__head``   headline: conversations, tokens, cost (the rollup payoff)
      - ``.dash__cols``   two ``.ledger`` panels — model mix + workspace mix,
                          token-sized bars, honest per-row cost
      - ``.dash__meta``   trust footnotes: token + cost coverage, corpus counts,
                          activity window, last ingest

    Cost honesty carries up from the folio: a per-row ``None`` renders ``&mdash;``,
    and the headline shows ``&mdash;`` when there is no priced usage at all rather
    than a fabricated ``$0.00``. The fragment root carries ``data-view/title/
    count/kick`` for enhance.js chrome sync, exactly like the folio.
    """
    from siftd.output.common import fmt_timestamp, fmt_tokens, fmt_workspace

    convs = getattr(usage, "total_conversations", 0)
    in_tok = getattr(usage, "total_input_tokens", 0)
    out_tok = getattr(usage, "total_output_tokens", 0)
    total_cost = getattr(usage, "total_cost", 0.0) or 0.0

    # "No priced usage" → em dash, same rule as a per-row None. A genuine summed
    # $0 with priced rows present still shows $0.00 (distinct from unknown).
    priced = total_cost > 0 or any(getattr(m, "cost", None) is not None for m in by_model)
    headline_cost = f"${total_cost:,.2f}" if priced else "&mdash;"

    kick = f"{escape(owner)} &middot; usage" if owner else "usage"

    counts = getattr(stats, "counts", None)
    tcov = getattr(stats, "token_coverage", None)
    tok_pct = getattr(tcov, "pct_with_tokens", None) if tcov else None
    cost_pct = getattr(coverage, "pct_covered", None) if coverage else None
    window = getattr(stats, "activity_window", (None, None)) or (None, None)
    last_ingest = getattr(stats, "last_ingest_at", None)

    def _meta(k: str, v: str) -> str:
        return (
            f'<div class="dash__metarow"><span class="dash__metak">{escape(k)}</span>'
            f'<span class="dash__metav">{v}</span></div>'
        )

    def _pct(p: float) -> str:
        # 1dp, and never round UP to a false 100% — 99.87% is not "complete".
        # Coverage measures token *presence* per response, not completeness of
        # the value (cache-read tokens live off this axis), so an honest 99.9%
        # matters: it must not read as "we have everything".
        import math

        shown = math.floor(p * 10) / 10 if p < 100 else 100.0
        return f"{shown:.1f}%"

    meta_rows: list[str] = []
    if tok_pct is not None:
        meta_rows.append(_meta("Token coverage", _pct(tok_pct)))
    if cost_pct is not None:
        meta_rows.append(_meta("Cost coverage", _pct(cost_pct)))
    if counts is not None:
        meta_rows.append(_meta("Responses", f"{counts.responses:,}"))
        meta_rows.append(_meta("Tool calls", f"{counts.tool_calls:,}"))
        meta_rows.append(_meta("Models", f"{counts.models:,}"))
        meta_rows.append(_meta("Workspaces", f"{counts.workspaces:,}"))
    start, end = window
    if start or end:
        # fmt_timestamp emits ISO-ish text (no markup); the &ndash; is a literal
        # entity, so the value is assembled pre-escaped rather than run through escape().
        span = f"{escape(fmt_timestamp(start) or '?')} &ndash; {escape(fmt_timestamp(end) or '?')}"
        meta_rows.append(_meta("Activity", span))
    if last_ingest:
        meta_rows.append(_meta("Last ingest", escape(fmt_timestamp(last_ingest) or "")))

    parts: list[str] = [
        f'<article class="dash" data-view="stats" data-title="Stats"'
        f' data-count="{convs}" data-kick="{kick}">',
        '<section class="dash__head">',
        '<div class="dash__stat"><span class="micro">Conversations</span>'
        f'<span class="dash__statn">{convs:,}</span></div>',
        '<div class="dash__stat"><span class="micro">Tokens</span>'
        f'<span class="dash__statn">{escape(fmt_tokens(in_tok + out_tok))}</span>'
        f'<span class="dash__sub">{escape(fmt_tokens(in_tok))} in &middot; '
        f'{escape(fmt_tokens(out_tok))} out</span></div>',
        '<div class="dash__stat"><span class="micro">Cost</span>'
        f'<span class="dash__statn">{headline_cost}</span></div>',
        '</section>',
        '<div class="dash__cols">',
        '<section class="dash__panel">',
        '<div class="folio__navhead"><span class="micro">Model mix</span>'
        f'<span class="folio__navmeta">{len(by_model)}</span></div>',
        f'<ul class="ledger ledger--usage">{_dash_usage_rows(by_model)}</ul>',
        '</section>',
        '<section class="dash__panel">',
        '<div class="folio__navhead"><span class="micro">Workspace mix</span>'
        f'<span class="folio__navmeta">{len(by_workspace)}</span></div>',
        '<ul class="ledger ledger--usage">'
        f'{_dash_usage_rows(by_workspace, label_fn=fmt_workspace, limit=8)}</ul>',
        '</section>',
        '</div>',
        '<section class="dash__meta">',
        '<div class="folio__navhead"><span class="micro">Corpus</span></div>',
        f'<div class="dash__metagrid">{"".join(meta_rows)}</div>',
        '</section>',
        '</article>',
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Workspaces view — a drillable master ledger + a per-workspace detail
# ---------------------------------------------------------------------------


def _ws_label(path: str | None) -> tuple[str, str]:
    """Split a workspace path into ``(leaf, home-relative parent)`` for display.

    The master ledger shows the leaf as the primary label and the parent dimmed
    beneath it, so two workspaces sharing a basename (the legacy ``painted``
    twin: ``~/Code/painted`` vs ``~/Code/loops/libs/painted``) stay
    distinguishable instead of collapsing to one indistinguishable row.
    """
    if not path or path in ("/", ""):
        return ("(root)", "")
    from pathlib import Path

    p = path.rstrip("/")
    leaf = p.rsplit("/", 1)[-1] or "(root)"
    parent = p[: len(p) - len(leaf)].rstrip("/")
    home = str(Path.home())
    if home and parent.startswith(home):
        parent = "~" + parent[len(home):]
    return (leaf, parent)


def _ws_pin_btn(ws_id: str, *, pinned: bool, pin_action_url: str, sort: str) -> str:
    """A pin/unpin toggle for a workspace. Mirrors ``_tag_row``'s pin: posts to
    the whole-view re-render (``#main`` swap) so a pinned workspace lifts into the
    head and an unpinned one drops back. Carries the active ``sort`` so the swap
    preserves the list ordering. Empty when no ``pin_action_url`` (e.g. JSON).
    """
    if not pin_action_url:
        return ""
    import json as _json

    vals = _json.dumps({"action": "unpin" if pinned else "pin", "ws": ws_id, "sort": sort})
    star = "★" if pinned else "☆"
    cls = "pin pin--on" if pinned else "pin"
    verb = "Unpin" if pinned else "Pin"
    pressed = "true" if pinned else "false"
    return (
        f'<button class="{cls}" type="button"'
        f' hx-post="{escape(pin_action_url)}" hx-vals="{escape(vals)}"'
        f' hx-target="#main" hx-swap="innerHTML"'
        f' aria-pressed="{pressed}" title="{verb} workspace">{star}</button>'
    )


def _workspace_row(
    row: Any, *, detail_base: str, shell_base: str, pin_action_url: str, sort: str
) -> str:
    """One workspace as a drillable ``.ledger--ws`` row.

    pin · leaf + parent/sessions/last-active · [bar] · tokens · honest cost. The
    row carries the workspace ULID, so the drill mounts the per-workspace detail
    keyed on ``ws`` (distinct from the folio's ``id``). Assumes a ``with_usage``
    row (``inp``/``out``/``cost`` present) — the view always opts in.

    The bar encodes the ACTIVE ``sort`` measure (sessions/tokens/cost), so the
    row order always matches the bar — never the order≠bar lie. The recency sort
    has no magnitude, so it drops the bar entirely; the ``.is-ranked`` modifier on
    the ``<ul>`` keeps the column grid aligned in both cases.
    """
    from siftd.output.common import fmt_tokens

    ws_id = row["id"]
    leaf, parent = _ws_label(row["path"])
    tok = (row["inp"] or 0) + (row["out"] or 0)
    cost = row["cost"]
    cost_str = "&mdash;" if cost is None else f"${cost:,.2f}"

    meta_bits: list[str] = []
    if parent:
        meta_bits.append(parent)
    meta_bits.append(f"{row['convs'] or 0:,} sessions")
    last = _ago(_iso_epoch(row["last_activity"]))
    if last:
        meta_bits.append(f"active {last} ago")
    sub = " · ".join(meta_bits)

    pin = _ws_pin_btn(ws_id, pinned=bool(row["pinned"]), pin_action_url=pin_action_url, sort=sort)
    drill = _hx_detail(detail_base, ws_id, shell_base, key="ws")
    bar = ""
    if sort != "recent":
        measure = {"sessions": row["convs"] or 0, "tokens": tok, "cost": cost or 0}.get(sort, tok)
        bar = f'<span class="ledger__bar" data-n="{measure}"></span>'
    return (
        f'<li class="ledger__row">{pin}'
        f'<a class="ledger__name"{drill}>'
        f'<span class="ws__name">{escape(leaf)}</span>'
        f'<span class="ws__sub">{escape(sub)}</span></a>'
        f"{bar}"
        f'<span class="ledger__n">{escape(fmt_tokens(tok))}</span>'
        f'<span class="ledger__cost">{cost_str}</span>'
        f"</li>"
    )


def _workspace_card(
    row: Any, *, detail_base: str, shell_base: str, pin_action_url: str, sort: str
) -> str:
    """One workspace as a head card (Pinned / Recent zones). Reuses the Sessions
    ``.cards`` grid: name, parent · active-ago, and sessions·tokens·cost — no bar
    (cards are shortcuts, not a ranking). The ``.card__link`` is the drill target
    (so the pin button, a sibling, doesn't fire it); the pin sits in the corner.
    """
    from siftd.output.common import fmt_tokens

    ws_id = row["id"]
    leaf, parent = _ws_label(row["path"])
    convs = row["convs"] or 0
    tok = (row["inp"] or 0) + (row["out"] or 0)
    cost = row["cost"]
    cost_str = "&mdash;" if cost is None else f"${cost:,.2f}"
    last = _ago(_iso_epoch(row["last_activity"]))
    meta_bits = [b for b in (parent, (f"active {last} ago" if last else "")) if b]

    pin = _ws_pin_btn(ws_id, pinned=bool(row["pinned"]), pin_action_url=pin_action_url, sort=sort)
    drill = _hx_detail(detail_base, ws_id, shell_base, key="ws")
    return (
        f'<li class="card card--ws">{pin}'
        f'<a class="card__link"{drill}>'
        f'<div class="card__ws">{escape(leaf)}</div>'
        f'<div class="card__meta">{escape(" · ".join(meta_bits))}</div>'
        f'<div class="card__nums">'
        f'<span class="stat"><span class="stat__n">{convs:,}</span>'
        f'<span class="micro">sessions</span></span>'
        f'<span class="stat"><span class="stat__n">{escape(fmt_tokens(tok))}</span>'
        f'<span class="micro">tokens</span></span>'
        f'<span class="stat"><span class="stat__n">{cost_str}</span>'
        f'<span class="micro">cost</span></span>'
        f"</div></a></li>"
    )


_WS_SORT_OPTS: tuple[tuple[str, str], ...] = (
    ("recent", "Recent"),
    ("sessions", "Sessions"),
    ("tokens", "Tokens"),
    ("cost", "Cost"),
)


def _ws_controls(*, sort: str, sort_base: str) -> str:
    """The body controls: a client-side filter box + a server-side sort group.

    The sort options hx-get the view with ``?sort=`` and swap ``#main`` (the same
    whole-fragment pattern as pins); the active one is marked ``is-active``. The
    filter is wired in enhance.js (``wireWorkspaceFilter``) — CSP-safe, hides
    non-matching rows client-side.
    """
    links: list[str] = []
    for key, label in _WS_SORT_OPTS:
        active = " is-active" if key == sort else ""
        hx = (
            f' hx-get="{escape(sort_base)}?sort={key}" hx-target="#main" hx-swap="innerHTML"'
            if sort_base
            else ""
        )
        aria = ' aria-current="true"' if key == sort else ""
        links.append(f'<a class="ws-sort__opt{active}"{hx}{aria}>{escape(label)}</a>')
    sort_ctrl = (
        f'<div class="ws-sort" role="group" aria-label="Sort by">{"".join(links)}</div>'
    )
    filt = (
        '<input class="ws-filter" type="search" placeholder="Filter workspaces…"'
        ' data-ws-filter aria-label="Filter workspaces" autocomplete="off">'
    )
    return f'<div class="ws-controls">{filt}{sort_ctrl}</div>'


def render_workspaces(rows: list, **context: Any) -> str:
    """Render the Swiss 'Workspaces' view: a drillable master ledger.

    Each row is one workspace (ULID identity), ranked by conversation count, with
    a token-sized bar and honest cost (``&mdash;`` when the workspace has no
    priced usage — never a fabricated $0). Rows mount the per-workspace detail
    into ``#main`` via the shared ``_hx_detail`` contract, keyed on ``ws``.

    Legacy duplicate workspaces (sharing a git remote) are surfaced as a caveat
    strip advertising the count + the ``siftd migrate --merge-workspaces``
    remediation, not silently merged: the dedup is a data migration with one
    keeper, not a render-time guess (and read-time collapse would make the drill
    target ambiguous). The strip rides ``context['duplicates']`` as ``(groups,
    extras)``; the view passes it only when unscoped (local), where the
    remediation is runnable.
    """
    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")
    pin_action_url = context.get("pin_action_url", "")
    sort_base = context.get("sort_base", "")
    sort = context.get("sort", "sessions")
    dup_groups, dup_extras = context.get("duplicates", (0, 0))

    row_kw = {
        "detail_base": detail_base,
        "shell_base": shell_base,
        "pin_action_url": pin_action_url,
        "sort": sort,
    }

    def _zone(label: str, count_txt: str, body: str, *, mod: str = "") -> str:
        zcls = f"zone {mod}" if mod else "zone"
        return (
            f'<section class="{zcls}"><div class="zone__head">'
            f'<span class="micro">{escape(label)}</span>'
            f'<span class="zone__count">{escape(count_txt)}</span>'
            f'<span class="zone__rule"></span></div>{body}</section>'
        )

    parts: list[str] = []

    if dup_groups:
        grp = "groups" if dup_groups != 1 else "group"
        rw = "rows" if dup_extras != 1 else "row"
        parts.append(
            '<div class="ws-caveat" role="note">'
            '<span class="ws-caveat__mark" aria-hidden="true"></span>'
            f"<span>{dup_groups} workspace {grp} share a git remote "
            f"({dup_extras} duplicate {rw}) — run "
            "<code>siftd migrate --merge-workspaces</code> to collapse.</span>"
            "</div>"
        )

    pinned = [r for r in rows if r["pinned"]] if rows else []

    if rows:
        # Head — cards as shortcuts OVER the body list (not a partition): pinned,
        # then a Recent strip (most-recently-active, not pinned) ordered by
        # recency regardless of the body's active sort.
        if pinned:
            cards = "".join(_workspace_card(r, **row_kw) for r in pinned)
            parts.append(
                _zone("Pinned", str(len(pinned)), f'<ul class="cards">{cards}</ul>',
                      mod="zone--pinned")
            )
        unpinned = [r for r in rows if not r["pinned"]]
        recent = sorted(unpinned, key=lambda r: r["last_activity"] or "", reverse=True)[:12]
        if recent:
            cards = "".join(_workspace_card(r, **row_kw) for r in recent)
            parts.append(_zone("Recent", str(len(recent)), f'<ul class="cards">{cards}</ul>'))

        # Body — the full canonical list under a filter + sort. ``is-ranked`` adds
        # the bar column for magnitude sorts; recency drops it.
        controls = _ws_controls(sort=sort, sort_base=sort_base)
        ranked = "" if sort == "recent" else " is-ranked"
        listing = "".join(_workspace_row(r, **row_kw) for r in rows)
        parts.append(
            _zone(
                "All workspaces", f"{len(rows):,}",
                controls + f'<ul class="ledger ledger--usage ledger--ws{ranked}">{listing}</ul>',
            )
        )
    else:
        parts.append(
            '<ul class="ledger ledger--usage ledger--ws">'
            '<li class="ledger__row ledger__empty">'
            '<span class="ledger__name">no workspaces yet</span></li></ul>'
        )

    kick = " · ".join(filter(None, ["pinned" if pinned else "", "explorer"]))
    return (
        '<section class="workspaces" data-view="workspaces" data-title="Workspaces"'
        f' data-count="{len(rows)}" data-kick="{kick}">'
        f'{"".join(parts)}</section>'
    )


def render_workspace_detail(detail: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render one workspace's detail — a dashboard scoped to a single workspace.

    Reuses the dashboard's stat-grid head and ``_dash_usage_rows`` ledger (cost
    honesty + the token-bar primitive carry over verbatim), then lists the
    workspace's recent conversations as folio-drilling rows (same ``_hx_detail``
    contract as Sessions/Find). The fragment root carries the
    data-view/title/count/kick chrome contract like every view; ``data-view`` is
    ``workspaces`` so the rail keeps the Workspaces tab lit while a detail shows.
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens

    detail_base = context.get("detail_base", "")  # /folio for recent rows
    shell_base = context.get("shell_base", "")

    leaf, parent = _ws_label(detail.path)
    in_tok = detail.input_tokens or 0
    out_tok = detail.output_tokens or 0
    headline_cost = "&mdash;" if detail.cost is None else f"${detail.cost:,.2f}"
    kick = parent or (detail.git_remote or "workspace")

    bar = (
        '<div class="ws-detail__bar">'
        '<a class="ws-detail__back" hx-get="/view/workspaces" hx-target="#main"'
        ' hx-swap="innerHTML" hx-push-url="/">&larr; Workspaces</a>'
        f'<span class="ws-detail__path">{escape(detail.path or "(root)")}</span>'
        "</div>"
    )

    head = (
        '<section class="dash__head">'
        '<div class="dash__stat"><span class="micro">Sessions</span>'
        f'<span class="dash__statn">{detail.sessions:,}</span></div>'
        '<div class="dash__stat"><span class="micro">Tokens</span>'
        f'<span class="dash__statn">{escape(fmt_tokens(in_tok + out_tok))}</span>'
        f'<span class="dash__sub">{escape(fmt_tokens(in_tok))} in &middot; '
        f'{escape(fmt_tokens(out_tok))} out</span></div>'
        '<div class="dash__stat"><span class="micro">Cost</span>'
        f'<span class="dash__statn">{headline_cost}</span></div>'
        "</section>"
    )

    models = (
        '<section class="dash__panel ws-detail__panel">'
        '<div class="folio__navhead"><span class="micro">Model mix</span>'
        f'<span class="folio__navmeta">{len(detail.model_mix)}</span></div>'
        f'<ul class="ledger ledger--usage">{_dash_usage_rows(detail.model_mix, limit=20)}</ul>'
        "</section>"
    )

    recent_rows: list[str] = []
    for c in detail.recent:
        # All recent share this workspace, so the primary label is the start time
        # (not the workspace name, which would be constant down the column).
        when = fmt_timestamp(getattr(c, "started_at", None)) or c.id[:12]
        model = fmt_model(getattr(c, "model", None)) or ""
        rc = getattr(c, "cost", None)
        rc_str = f"${rc:,.2f}" if rc is not None else "&mdash;"
        recent_rows.append(
            f'<li class="row"{_hx_detail(detail_base, c.id, shell_base)}>'
            f'<span class="row__ws">{escape(when)}</span>'
            f'<span class="row__model">{escape(model)}</span>'
            f'<span class="row__turns">{getattr(c, "prompt_count", 0)}</span>'
            f'<span class="row__tok">{escape(fmt_tokens(getattr(c, "total_tokens", 0) or 0))}</span>'
            f'<span class="cost">{rc_str}</span>'
            f"</li>"
        )
    recent_body = (
        "".join(recent_rows)
        if recent_rows
        else '<li class="row"><span class="row__ws">no conversations</span></li>'
    )
    recent = (
        '<section class="ws-detail__recent">'
        '<div class="zone__head"><span class="micro">Recent</span>'
        f'<span class="zone__count">{len(recent_rows)}</span>'
        '<span class="zone__rule"></span></div>'
        f'<ul class="rows">{recent_body}</ul>'
        "</section>"
    )

    return (
        f'<article class="dash ws-detail" data-view="workspaces" data-title="{escape(leaf)}"'
        f' data-count="{detail.sessions}" data-kick="{escape(kick)}">'
        f"{bar}{head}{models}{recent}</article>"
    )
