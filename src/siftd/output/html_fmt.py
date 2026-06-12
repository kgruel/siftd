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


def _hx_detail(detail_base: str, conv_id: str, shell_base: str = "") -> str:
    """Build htmx attributes for a detail link, or empty string if no base.

    Rows mount the detail surface into ``#main`` — the Swiss shell's single
    swap target. The old two-pane ``#detail`` container no longer exists, so
    anything still targeting it is a dead click (htmx targetError).
    """
    if not detail_base:
        return ""
    push = f' hx-push-url="{escape(shell_base)}?id={escape(conv_id)}"' if shell_base else ""
    return (
        f' hx-get="{escape(detail_base)}?id={escape(conv_id)}"'
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
        mode: str — "chunks", "conversations", or "thread"
        detail_base: str — URL prefix for detail links
        caveats: list[Finding] — threaded from dispatch; appended as an
            ``<aside class="caveats">`` fragment after the results section.
    """
    from siftd.output.common import truncate_text

    query = context.get("query", "")
    mode = context.get("mode", "chunks")
    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")
    caveats = context.get("caveats") or []

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
            parts.append(f'<td class="identifier">{escape(short_id(conv_id))}</td>')
            parts.append(f'<td class="metric">{r.get("max_score", 0.0):.3f}</td>')
            parts.append(f'<td class="metric">{r.get("mean_score", 0.0):.3f}</td>')
            parts.append(f'<td class="metric">{r.get("chunk_count", 0)}</td>')
            parts.append(f'<td class="temporal">{escape(r.get("_started_at", ""))}</td>')
            parts.append(f'<td class="workspace">{escape(r.get("_workspace", ""))}</td>')
            parts.append("</tr>")
        parts.append("</tbody></table></section>")

    elif mode == "thread":
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
        # Chunks mode
        parts.append('<section class="search-results chunks">')
        parts.append(f"<h2>Results for: {escape(query)}</h2>")
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
    """Render assistant prose as HTML (markdown when mistune is available)."""
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


def _plain_paragraphs(text: str) -> str:
    """User prompts: escaped, paragraph-split, never markdown-interpreted."""
    out: list[str] = []
    for para in text.strip().split("\n\n"):
        stripped = para.strip()
        if stripped:
            out.append(f"<p>{escape(stripped)}</p>")
    return "".join(out) or "<p></p>"


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


def render_folio(detail: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render a conversation as the Swiss 'folio' fragment.

    Three CSS-grid regions, all over data ``get_conversation`` already returns:
      - ``.folio__nav``    turn-rail (anchor links into the body; ``:target``
                           highlights the landed turn — no JS scroll-spy needed)
      - ``.folio__body``   user prompts + assistant prose (markdown); tool I/O is
                           NOT inlined (the ledger owns it)
      - ``.folio__ledger`` a ``Counter`` over ``turn.tool_call_summaries`` —
                           pure render-layer, no new data

    The fragment root carries ``data-view/title/count/kick`` so ``enhance.js``
    updates the chrome head + active nav on an htmx swap without oob coupling.
    The ledger foot shows tokens + cost; cost is the rollup's canonical
    per-conversation value (``ConversationDetail.cost``, fetched at depth>=3),
    rendered as ``&mdash;`` when ``None`` (no priced usage) — never a fabricated
    $0 that would re-introduce the mispricing the rollup work removed. The tool
    total lives in the ledger header (``folio__navmeta``).

    Context keys (all optional — the folio is the single detail surface, so it
    hosts the tag/export affordances the two-pane detail used to carry):
        interactive_tags: bool — tag pills get × remove + an add input
        tag_action_url / tag_suggest_url: routes for the tag form (no hardcoding)
        export_base_url: route for md/json download links
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
                f'<div class="turn__text">{_plain_paragraphs(prompt_text)}</div>'
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
    kick = f"{escape(short)} · folio" if short else "folio"

    # Curation foot: tags + export, hosted in the ledger aside. The /tag route
    # swaps the same stable #tags-<id> section render_tag_section returns.
    curation = ""
    if conv_id:
        tags = getattr(detail, "tags", None) or []
        tag_html = _render_tag_section(
            conv_id, tags, context.get("interactive_tags", False),
            tag_action_url=context.get("tag_action_url", ""),
            tag_suggest_url=context.get("tag_suggest_url", ""),
        )
        export_html = _render_export_links(context.get("export_base_url", ""), conv_id)
        curation = f'<div class="ledger__curation">{tag_html}{export_html}</div>'

    parts: list[str] = [
        f'<article class="folio" data-view="transcript" data-title="Transcript"'
        f' data-count="{turn_count}" data-kick="{kick}">',
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
        '<div class="ledger__foot">',
        '<div class="ledger__stat"><span class="micro">Tokens</span>'
        f'<span class="ledger__statn">{escape(fmt_tokens(total_tokens))}</span></div>',
        '<div class="ledger__stat"><span class="micro">Cost</span>'
        f'<span class="ledger__statn">{cost_str}</span></div>',
        "</div>",
        curation,
        "</aside>",
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
    Rows mount the folio via the same _hx_detail contract as Find.
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
    days: OrderedDict[str, list] = OrderedDict()
    for c in summaries:
        epoch = _iso_epoch(getattr(c, "started_at", None))
        key = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d") if epoch else "unknown"
        days.setdefault(key, []).append(c)

    day_parts: list[str] = []
    for key, convs in days.items():
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
        tok = sum(getattr(c, "total_tokens", 0) or 0 for c in convs)
        priced = [c.cost for c in convs if getattr(c, "cost", None) is not None]
        cost_str = f"${sum(priced):,.2f}" if priced else "&mdash;"
        sub = f"{len(convs)} sessions · {fmt_tokens(tok)} tok · {cost_str}"

        rows: list[str] = []
        for c in convs:
            model = fmt_model(getattr(c, "model", None)) or ""
            ws = getattr(c, "workspace_path", None)
            ws_name = ws.rstrip("/").rsplit("/", 1)[-1] if ws else "?"
            row_cost = getattr(c, "cost", None)
            row_cost_str = f"${row_cost:,.2f}" if row_cost is not None else "&mdash;"
            rows.append(
                f"<li class=\"row\"{_hx_detail(detail_base, c.id, shell_base)}>"
                f'<span class="row__ws">{escape(ws_name)}</span>'
                f'<span class="row__model">{escape(model)}</span>'
                f'<span class="row__turns">{getattr(c, "prompt_count", 0)}</span>'
                f'<span class="row__tok">{escape(fmt_tokens(getattr(c, "total_tokens", 0) or 0))}</span>'
                f'<span class="cost">{row_cost_str}</span>'
                f"</li>"
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
