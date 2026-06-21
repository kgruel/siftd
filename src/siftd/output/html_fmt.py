"""HTML fragment output format — renders conversations as htmx-swappable fragments.

Returns HTML strings (not full pages). A page shell provides the chrome;
these fragments are swap targets for htmx requests or standalone embeds.

DomainStyles map 1:1 to CSS classes — the same semantic vocabulary
(identifier, temporal, metric, tool_name, ...) used in terminal rendering
becomes the class namespace here.
"""

from __future__ import annotations

from collections import Counter
from html import escape
from typing import TYPE_CHECKING, Any

from siftd.output._id_format import short_id

if TYPE_CHECKING:
    from painted import Fidelity

FORMATTER_INTERFACE_VERSION = 1
name = "html"
media_type = "text/html"


def _hx_detail(
    detail_base: str,
    value: str,
    shell_base: str = "",
    *,
    key: str = "id",
    mode: str | None = None,
    event: str | None = None,
) -> str:
    """Build htmx attributes that mount a surface into ``#main``, or "" if no base.

    Rows mount into ``#main`` — the Swiss shell's single swap target (the old
    two-pane ``#detail`` is gone, so anything still targeting it is a dead click).
    ``key`` is the query param: ``id`` for a conversation folio, ``tag`` for a
    tag-filtered Find. ``value`` is URL-encoded with ``quote`` (not html-escape)
    so a value carrying ``:``/``&``/spaces yields a valid query string — quote's
    output is also attribute-safe, so the whole attribute stays html-safe. (For
    ULIDs quote and escape are identical, so existing ``?id=`` callers are
    unaffected.)

    A folio jump from a search hit carries ``mode="trace"`` (the entry-point
    default: you came from a match, so show the agent's actual event flow) and,
    when the matched chunk knows it, ``event=<ULID>`` — the route marks that
    event ``is-target`` and scrolls it into view, so the jump lands *on the
    match*, not the folio top. Both ride the push-url too, so a reload
    deep-links to the same spot.
    """
    from urllib.parse import quote as _q

    if not detail_base:
        return ""
    qv = _q(value)
    suffix = ""
    if mode:
        suffix += f"&mode={_q(mode)}"
    if event:
        suffix += f"&event={_q(event)}"
    push = f' hx-push-url="{escape(shell_base)}?{key}={qv}{suffix}"' if shell_base else ""
    return (
        f' hx-get="{escape(detail_base)}?{key}={qv}{suffix}"'
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


def render_search(result: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render a :class:`SearchView` as HTML fragments.

    The positional argument is a ``SearchView`` (a bare list of render-dicts is
    tolerated and wrapped as a chunks view); the view shape and the thread
    ``tier1``/``tier2`` split ride the SearchView. Context keys:

        query: str — the search query
        mode: str — resolved search engine that ran: "fts", "semantic", or "hybrid"
        detail_base: str — URL prefix for detail links
        caveats: list[Finding] — threaded from dispatch; appended as an
            ``<aside class="caveats">`` fragment after the results section.
    """
    from siftd.domain.search_types import as_search_view
    from siftd.output.common import truncate_text

    sv = as_search_view(result, view=context.get("view", "chunks"))
    results = sv.results
    query = context.get("query", "")
    view = sv.view
    engine = context.get("mode")
    engine_tag = f' <span class="engine">[{escape(engine)}]</span>' if engine else ""
    detail_base = context.get("detail_base", "")
    shell_base = context.get("shell_base", "")
    caveats = context.get("caveats") or []

    parts: list[str] = []

    if view == "conversations":
        parts.append('<section class="search-results conversations">')
        parts.append(f"<h2>Conversations for: {escape(query)}{engine_tag}</h2>")
        if not results:
            parts.append('<p class="empty">No matches.</p>')
        else:
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
            parts.append("</tbody></table>")
        parts.append("</section>")

    elif view == "thread":
        tier1 = sv.tier1 or []
        tier2 = sv.tier2 or []
        parts.append('<section class="search-results thread">')
        parts.append(f"<h2>Results for: {escape(query)}{engine_tag}</h2>")
        if not tier1 and not tier2:
            parts.append('<p class="empty">No matches.</p>')

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
                    f'{_hx_detail(detail_base, conv_id, shell_base, mode="trace", event=r.get("event_id"))}>'
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
            turn_index = r.get("turn_index")
            event_id = r.get("event_id")

            # The hit splits into two siblings: __main carries the folio nav (the
            # deliberate jump); .hit-context holds the inline unfold control. They
            # are siblings — not nested — so an unfold click never bubbles to the
            # folio-navigable block (no stopPropagation / JS needed, CSP-clean).
            # The jump opens the trace anchored at the matched event (search →
            # trace; lands on the match, not the folio top).
            parts.append('<article class="search-hit">')
            parts.append(
                f'<div class="search-hit__main"'
                f'{_hx_detail(detail_base, conv_id, shell_base, mode="trace", event=event_id)}>'
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
            parts.append("</div>")  # .search-hit__main

            # Unfold context in place — only when the chunk knows its turn anchor
            # (turn_index None means enrichment couldn't place it; no anchor → no
            # unfold, the hit still navigates to the folio).
            if conv_id and turn_index is not None:
                parts.append(
                    f'<div class="hit-context">'
                    f'{_unfold_trigger(conv_id, int(turn_index), event_id)}</div>'
                )
            parts.append("</article>")

        parts.append("</section>")

    if caveats:
        parts.append('<aside class="caveats">')
        for c in caveats:
            parts.append(f'<p class="caveat">{escape(c.message)}</p>')
        parts.append("</aside>")

    return "\n".join(parts)


# Stepped context rings for the search "unfold" view: window half-widths (turn
# offsets) the inline context widens through before deferring to the full folio.
SEARCH_CONTEXT_RINGS: tuple[int, ...] = (2, 5, 10)


def _unfold_button(label: str, attrs: str, *, classes: str = "") -> str:
    """One shape for every context control — the single place the unfold
    buttons' (CSP-relevant) attribute surface lives. ``attrs`` is a pre-built
    htmx attribute string with a leading space (from :func:`_ctx_attrs` for a
    same-region ring step, or :func:`_hx_detail` for the folio jump)."""
    cls = "hit-unfold" + (f" {classes}" if classes else "")
    return f'<button class="{cls}" type="button"{attrs}>{escape(label)}</button>'


def _ctx_attrs(conv_id: str, at: int, w: int, event: str | None = None) -> str:
    """htmx attrs for a same-region context step: fetch ring ``w`` and swap it
    into the closest ``.hit-context`` (so hits unfold independently, no id
    plumbing). ``w=0`` is the collapsed trigger. ``event`` (the matched chunk's
    ULID) rides every ring URL so it survives the re-render and the last ring's
    'open in folio' jump stays event-precise."""
    evt = f"&event={escape(event)}" if event else ""
    return (
        f' hx-get="/find/context?id={escape(conv_id)}&at={at}&w={w}{evt}"'
        f' hx-target="closest .hit-context" hx-swap="innerHTML"'
    )


def _unfold_trigger(conv_id: str, at: int, event: str | None = None) -> str:
    """The collapsed state of a chunk's context region — a single control that
    fetches the first ring. Shared by the initial hit render and the 'collapse'
    action (which restores exactly this)."""
    return _unfold_button(
        "unfold context", _ctx_attrs(conv_id, at, SEARCH_CONTEXT_RINGS[0], event)
    )


def render_search_context(detail: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render one search hit's unfolded context slice — the inline 'unfold' view.

    The seed is a matched chunk; this renders a window of surrounding exchanges
    (``get_conversation`` anchored at the chunk's turn, the matched one flagged
    ``is-anchor``) plus the stepped controls: widen to the next ring, or — at the
    last ring — defer to the full folio (the deliberate jump), and always
    collapse. State rides the control URLs (the current ``w``), so no session is
    needed; ``w <= 0`` (or a missing/short read) renders the collapsed trigger.

    Context keys: ``conv_id``, ``at`` (anchor turn_index), ``w`` (current
    window half-width), ``anchor_pos`` (matched exchange's index in the window),
    ``event`` (the matched chunk's ULID — rides the ring URLs so the last ring's
    'open in folio' jump lands event-precise).
    """
    conv_id = context.get("conv_id", "")
    at = int(context.get("at", 0))
    w = int(context.get("w", 0))
    anchor_pos = context.get("anchor_pos")
    event = context.get("event") or None

    if w <= 0 or detail is None:
        return _unfold_trigger(conv_id, at, event)

    turns = getattr(detail, "turns", []) or []
    # The unfold IS the trace: it answers "what did the agent actually do here",
    # so it inlines tool I/O in sequence rather than the folio's prose-only body.
    # The route fetches it with a tools/thinking-visible fidelity to match.
    body, _rail, _n, _tc, _seq = _render_turn_blocks(
        turns, fidelity, id_prefix=None, anchor_pos=anchor_pos, mode="trace"
    )
    if not body:
        # Same collapsed-trigger fallback as the w<=0 path above — must thread
        # `event` identically, else a re-unfold from here loses event-precision.
        return _unfold_trigger(conv_id, at, event)

    parts: list[str] = ['<div class="hit-context__slice">', *body, "</div>"]
    parts.append('<div class="hit-context__controls">')
    next_w = next((r for r in SEARCH_CONTEXT_RINGS if r > w), None)
    if next_w is not None:
        parts.append(_unfold_button("more context", _ctx_attrs(conv_id, at, next_w, event)))
    else:
        # Last ring → the deliberate jump into the full folio. Reuse _hx_detail
        # so the folio-jump contract (target #main, push-url, quote()-encoded id)
        # lives in exactly one place. mode=trace + the matched event so the jump
        # lands on the match, consistent with the hit's own folio nav.
        parts.append(
            _unfold_button(
                "open in folio",
                _hx_detail("/folio", conv_id, "/", mode="trace", event=event),
                classes="hit-unfold--folio",
            )
        )
    parts.append(
        _unfold_button(
            "collapse", _ctx_attrs(conv_id, at, 0, event), classes="hit-unfold--collapse"
        )
    )
    parts.append("</div>")
    return "".join(parts)


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


def _folio_mode_toggle(conv_id: str, mode: str) -> str:
    """Segmented control switching the folio body between the reading view
    (prose; tool I/O in the ledger) and the trace view (tool I/O inlined in
    sequence — the agent's actual event flow).

    Stateless: the active mode rides the re-fetch URL and the *whole* folio
    re-renders into ``#main``. The re-fetch (not a client-side toggle) is
    load-bearing — trace mode needs ``get_conversation`` to fetch tool
    input/result, which it only does at a tools-visible fidelity, so the route
    must re-resolve the fidelity from the URL ``mode``.
    """
    from urllib.parse import quote as _q

    qv = _q(conv_id)
    btns = [
        f'<button type="button" class="folio-mode__btn'
        f'{" is-active" if val == mode else ""}"'
        f' hx-get="/folio?id={qv}&mode={val}"'
        f' hx-target="#main" hx-swap="innerHTML">{label}</button>'
        for val, label in (("reading", "Reading"), ("trace", "Trace"))
    ]
    return (
        '<div class="folio-mode" role="group" aria-label="View mode">'
        f'{"".join(btns)}</div>'
    )


def _render_turn_blocks(
    turns: list[Any],
    fidelity: Fidelity,
    *,
    id_prefix: str | None = None,
    anchor_pos: int | None = None,
    mode: str = "reading",
    target_event_id: str | None = None,
) -> tuple[list[str], list[str], int, Counter[str], list[dict]]:
    """Render exchanges as user/assistant ``.turn`` blocks — shared by the folio
    body and the search context slice (the unfold view).

    Each exchange (one ``Turn``) renders up to two ``.turn`` divs: the user
    prompt and the assistant narrative. ``id_prefix`` (e.g. ``"t"``) emits
    ``id="t-{n}"`` anchors + a parallel turn-rail (the folio needs both for its
    ``:target`` highlight and scroll-spy); leaving it ``None`` emits neither (the
    inline context slice has no rail and needs no anchors). ``anchor_pos`` is the
    *exchange* index to flag with ``is-anchor`` — the matched turn in an unfold.

    ``mode`` selects the assistant-body emitter (the only axis that differs
    between the two body shapes — header/framing stay shared):

    - ``"reading"`` (default): :class:`_FolioEmitter` — prose + thinking only;
      tool I/O is dropped from the body and surfaced as the per-turn ``__tools``
      chip + the folio ledger. The reading view.
    - ``"trace"``: :class:`HtmlEmitter` — tool I/O inlined *in sequence* (each
      tool a collapsed ``<details>``), the agent's actual event flow. The
      per-turn chip is suppressed (the tools are inline now). The caller MUST
      fetch with a tools/thinking-visible ``fidelity`` (``walk_narrative`` keys
      ``tool_content`` vs ``tool_summary`` off ``fidelity.shows("tools")``, and
      ``get_conversation`` only populates tool input/result when it does), so
      ``mode`` and ``fidelity`` are resolved together at the route.

    Returns ``(body, rail, n, tool_counter, tool_seq)``: the body divs, the rail
    items (empty unless ``id_prefix``), the rail-item count (= the folio's
    "Turns N"), the tool-call ``Counter`` (the reading-mode aggregate ledger),
    and ``tool_seq`` — the chronological Activity registry (trace mode only;
    each entry {id, name, target, status, turn} anchors a body ``.tool-call``).
    """
    from siftd.output.common import fmt_timestamp
    from siftd.output.narrative import HtmlEmitter, walk_narrative

    trace = mode == "trace"

    body: list[str] = []
    rail: list[str] = []
    tool_counter: Counter[str] = Counter()
    tool_seq: list[dict] = []
    n = 0

    for i, turn in enumerate(turns):
        amark = " is-anchor" if anchor_pos is not None and i == anchor_pos else ""
        t_time = fmt_timestamp(getattr(turn, "timestamp", None), time_only=True) or ""
        prompt_text = getattr(turn, "prompt_text", None)
        narrative = getattr(turn, "narrative", []) or []
        summaries = getattr(turn, "tool_call_summaries", []) or []

        if prompt_text:
            n += 1
            idattr = ""
            if id_prefix:
                anchor = f"{id_prefix}-{n}"
                idattr = f' id="{anchor}"'
                rail.append(_folio_rail_item(n, "user", "User", t_time, anchor))
            # A prompt is a single event: anchor the whole user div by its
            # prompt_id (a ULID) so the search → folio jump can scroll/highlight
            # it. The assistant body's events are anchored inside, by the emitter.
            pid = getattr(turn, "prompt_id", None)
            evt_attrs = f' data-event-id="{escape(pid)}"' if pid else ""
            evt_cls = " is-target" if pid and pid == target_event_id else ""
            body.append(
                f'<div class="turn{amark}{evt_cls}" data-role="user"{idattr}{evt_attrs}>'
                f'<header class="turn__head"><span class="turn__role">User</span>'
                f'<span class="turn__time">{escape(t_time)}</span></header>'
                f'<div class="turn__text">{_md_to_html(prompt_text)}</div>'
                f"</div>"
            )

        if narrative:
            n += 1
            tool_parts: list[str] = []
            for s in summaries:
                tool_counter[s.tool_name] += s.count
                lbl = escape(s.tool_name)
                if s.count > 1:
                    lbl += f"&times;{s.count}"
                tool_parts.append(lbl)
            tools_html = (
                f'<span class="turn__tools">{" &middot; ".join(tool_parts)}</span>'
                if tool_parts and not trace else ""
            )
            idattr = ""
            if id_prefix:
                anchor = f"{id_prefix}-{n}"
                idattr = f' id="{anchor}"'
                rail.append(_folio_rail_item(n, "assistant", "Assistant", t_time, anchor))
            # Only the folio (id_prefix set) collects the Activity registry +
            # emits evt-N ids: the inline search-context slice has no aside and
            # can coexist with other slices, so it must not mint colliding ids.
            collect = trace and id_prefix is not None
            emitter = (
                HtmlEmitter(
                    target_event_id,
                    tool_seq=tool_seq if collect else None,
                    turn_no=n,
                )
                if trace else _FolioEmitter()
            )
            walk_narrative(narrative, emitter, fidelity=fidelity, tool_chars=0)
            body.append(
                f'<div class="turn{amark}" data-role="assistant"{idattr}>'
                f'<header class="turn__head"><span class="turn__role">Assistant</span>'
                f'<span class="turn__time">{escape(t_time)}</span>{tools_html}</header>'
                f'<div class="turn__text">{emitter.to_html()}</div>'
                f"</div>"
            )

    return body, rail, n, tool_counter, tool_seq


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
    from siftd.output.common import fmt_tokens

    turns = getattr(detail, "turns", []) or []
    conv_id = getattr(detail, "id", "") or ""
    short = short_id(conv_id) if conv_id else ""

    # Reading (default) vs trace body. The route resolves mode→fidelity (trace
    # needs tool I/O fetched), so by here the fidelity already matches the mode;
    # _render_turn_blocks only picks the emitter.
    mode = context.get("mode", "reading")
    if mode not in ("reading", "trace"):
        mode = "reading"

    # The search → "open in folio" jump passes the matched event (a ULID): the
    # body marks that element is-target and the article root carries
    # data-scroll-to, so enhance.js scrolls it into view after the htmx swap
    # (event-precise landing). Validated at the route; escaped on emit. The
    # target is a TRACE-mode affordance — response anchors only exist in trace,
    # and the entry-point rule always opens search jumps in trace — so a
    # reading-mode folio ignores it (no unscrollable hint on a hand-crafted URL).
    target_event_id = (context.get("target_event_id") or None) if mode == "trace" else None

    # Turn blocks (body + rail) are shared with the search context slice; the
    # folio passes id_prefix="t" for its :target anchors + scroll-spy rail.
    body, rail, n, tool_counter, tool_seq = _render_turn_blocks(
        turns, fidelity, id_prefix="t", mode=mode, target_event_id=target_event_id
    )

    total_tokens = (
        getattr(detail, "total_input_tokens", 0) + getattr(detail, "total_output_tokens", 0)
    )
    # Cost is the rollup's canonical per-conversation value, fetched at depth>=3.
    # None means no priced usage — render an em dash, never a fabricated $0.
    cost = getattr(detail, "cost", None)
    cost_str = f"${cost:.4f}" if cost is not None else "&mdash;"

    # The folio aside is the conversation's tool record, shown two ways:
    #   trace   → Activity: the chronological run (.tool-seq), each row a link to
    #             the matching inline .tool-call[id] (enhance.js mirrors scroll).
    #   reading → Tool ledger: the frequency aggregate (no inline tools to anchor).
    if mode == "trace":
        seq_rows: list[str] = []
        for it in tool_seq:
            err = " is-error" if it["status"] and it["status"] != "success" else ""
            turn_lbl = f'{it["turn"]:02d}' if it["turn"] else ""
            seq_rows.append(
                f'<a class="tool-seq__row{err}" href="#{it["id"]}">'
                f'<span class="tool-seq__n">{turn_lbl}</span>'
                f'<span class="tool-seq__name">{escape(it["name"])}</span>'
                f'<span class="tool-seq__target">{escape(it["target"])}</span></a>'
            )
        if not seq_rows:
            seq_rows.append(
                '<span class="tool-seq__row tool-seq__empty">'
                '<span class="tool-seq__name">no tool calls</span></span>'
            )
        aside_label, aside_count = "Activity", len(tool_seq)
        aside_body = f'<ol class="tool-seq">{"".join(seq_rows)}</ol>'
    else:
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
        aside_label, aside_count = "Tool ledger", sum(tool_counter.values())
        aside_body = f'<ul class="ledger">{"".join(ledger_rows)}</ul>'

    # The count is rail items (each User / Assistant message is a turn), so the
    # "Turns N" header matches the rail length — not the exchange count, which
    # would under-report by ~half (one exchange renders two turns).
    turn_count = n
    kick = context.get("kick") or (f"{escape(short)} · folio" if short else "folio")
    view = context.get("view", "transcript")
    title = context.get("title", "Transcript")
    live_poll_url = context.get("live_poll_url", "")

    # Curation: tags + export, hosted in the command bar's actions group. The
    # /tag route swaps the same stable #tags-<id> section render_tag_section
    # returns. Suppressed on a live folio (no ingest yet → nothing to curate).
    curation = ""
    if conv_id and not live_poll_url:
        tags = getattr(detail, "tags", None) or []
        tag_html = _render_tag_section(
            conv_id, tags, context.get("interactive_tags", False),
            tag_action_url=context.get("tag_action_url", ""),
            tag_suggest_url=context.get("tag_suggest_url", ""),
        )
        export_html = _render_export_links(context.get("export_base_url", ""), conv_id)
        curation = f'{tag_html}{export_html}'

    live_attrs = (
        f' hx-get="{escape(live_poll_url)}" hx-trigger="every 2s"'
        f' hx-swap="outerHTML"'
        if live_poll_url else ""
    )
    folio_cls = "folio folio--live" if live_poll_url else "folio"
    scroll_attr = f' data-scroll-to="{escape(target_event_id)}"' if target_event_id else ""
    # The reading↔trace toggle re-fetches the folio (so the route re-resolves
    # fidelity). Suppressed on a live folio: it isn't in the DB yet, so the
    # /folio re-fetch would 404 — same gate as curation.
    mode_toggle = (
        _folio_mode_toggle(conv_id, mode) if conv_id and not live_poll_url else ""
    )
    # Manuscript is the folio's permanent reading style (a pure-CSS treatment of
    # the .turn DOM; no variant picker). The Reading/Trace toggle stays — trace
    # is the same manuscript styling with the tool I/O inlined.
    bar = ""
    if mode_toggle or curation:
        bar = (
            '<div class="folio__bar">'
            f'<div class="folio__bargroup">{mode_toggle}</div>'
            + (
                f'<div class="folio__bargroup folio__bargroup--actions">{curation}</div>'
                if curation else ""
            )
            + '</div>'
        )
    parts: list[str] = [
        f'<article class="{folio_cls}" data-view="{escape(view)}"'
        f' data-title="{escape(title)}" data-mode="{escape(mode)}"'
        f' data-count="{turn_count}" data-kick="{kick}"{scroll_attr}{live_attrs}>',
        bar,
        '<nav class="folio__nav" aria-label="Turns">',
        '<div class="folio__navhead"><span class="micro">Turns</span>'
        f'<span class="folio__navmeta">{turn_count}</span></div>',
        f'<div class="turns">{"".join(rail)}</div>',
        "</nav>",
        '<div class="folio__body">',
        "".join(body) or '<p class="empty">This conversation has no turns.</p>',
        "</div>",
        f'<aside class="folio__ledger" aria-label="{aside_label}">',
        f'<div class="folio__navhead"><span class="micro">{aside_label}</span>'
        f'<span class="folio__navmeta">{aside_count}</span></div>',
        aside_body,
        "</aside>",
        # Foot is its own grid area: tokens + cost only now (tags/export moved to
        # the command bar). Wide layouts pin it under the ledger column; narrow
        # ones reflow it into the footer band.
        '<footer class="folio__foot">',
        '<div class="folio__stats">',
        '<div class="ledger__stat"><span class="micro">Tokens</span>'
        f'<span class="ledger__statn">{escape(fmt_tokens(total_tokens))}</span></div>',
        '<div class="ledger__stat"><span class="micro">Cost</span>'
        f'<span class="ledger__statn">{cost_str}</span></div>',
        "</div>",
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
    live_section = ""

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
        live_section = (
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
        row_cost_str = f"${row_cost:,.4f}" if row_cost is not None else "&mdash;"
        is_sub = parent_id is not None or flagged
        # Spawn/start clock-time rides the gutter for every entry (it orders
        # siblings and gives the daybook its diary rhythm).
        e = _iso_epoch(getattr(c, "started_at", None))
        when = datetime.fromtimestamp(e).strftime("%H:%M") if e is not None else ""
        if is_sub:
            # Children inherit the parent's workspace, so repeating its name on
            # every sibling is pure noise. Identify a child by what differs: its
            # agent type (when captured from the sidecar); the spawn time lives
            # in the gutter alongside every other entry.
            atype = (getattr(c, "agent_type", None) or "").strip()
            if ":" in atype:  # feature-dev:code-reviewer -> code-reviewer
                atype = atype.rsplit(":", 1)[-1]
            name_html = escape(atype) if atype else "sub-agent"
        else:
            name_html = escape(ws_name)
        # entry--sub / row--sub: the new daybook class plus the legacy hook the
        # disclosure CSS + enhance.js toggle still key off.
        cls = "entry entry--sub row--sub" if is_sub else "entry"
        attrs = _hx_detail(detail_base, c.id, shell_base)
        if parent_id is not None:
            attrs += f' data-parent="{escape(parent_id)}" hidden'
        if group_id is not None:
            gutter_mark = '<span class="entry__n" aria-hidden="true"></span>'
            toggle = (
                f'<button class="entry__toggle row__toggle" type="button"'
                f' data-group="{escape(group_id)}" aria-expanded="false"'
                f' aria-label="Toggle sub-agents"></button>'
            )
        elif is_sub:
            gutter_mark = '<span class="entry__caret" aria-hidden="true">&#8627;</span>'
            toggle = ""
        else:
            gutter_mark = '<span class="entry__n" aria-hidden="true"></span>'
            toggle = ""
        chip = (
            f'<span class="entry__chip">{agents} agent{"" if agents == 1 else "s"}</span>'
            if agents else ""
        )
        tok = fmt_tokens(getattr(c, "total_tokens", 0) or 0)
        return (
            f'<li class="{cls}"{attrs}>'
            f'<div class="entry__gutter">{gutter_mark}'
            f'<span class="entry__time">{escape(when)}</span></div>'
            f'<div class="entry__body">'
            f'<div class="entry__title">{toggle}'
            f'<span class="entry__name">{name_html}</span>{chip}</div>'
            f'<div class="entry__meta">{escape(model)}</div></div>'
            f'<div class="entry__figures">'
            f'<span class="figure"><span class="figure__n figure__n--dim">'
            f'{getattr(c, "prompt_count", 0)}</span><span class="figure__k">turns</span></span>'
            f'<span class="figure"><span class="figure__n">{escape(tok)}</span>'
            f'<span class="figure__k">tok</span></span>'
            f'<span class="figure"><span class="figure__n">{row_cost_str}</span>'
            f'<span class="figure__k">cost</span></span>'
            f"</div></li>"
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
            aria = "undated"
            dateline = (
                '<div class="dateline"><span class="dateline__day">&mdash;</span></div>'
            )
            hist = ""
        else:
            d = datetime.strptime(key, "%Y-%m-%d")
            aria = d.strftime("%a %d %b")
            day_num = d.strftime("%d").lstrip("0") or "0"
            dateline = (
                f'<div class="dateline"><span class="dateline__day">{day_num}</span>'
                f'<span class="dateline__cal"><span class="dateline__wk">{d.strftime("%a")}</span>'
                f'<span class="dateline__mo">{d.strftime("%b")}</span></span></div>'
            )
            hours = []
            for c in convs:
                e = _iso_epoch(c.started_at)
                if e is not None:
                    hours.append(datetime.fromtimestamp(e).hour)
            hist = _hour_hist(hours)
        tok = sum(getattr(c, "total_tokens", 0) or 0 for c in in_day)
        priced = [c.cost for c in in_day if getattr(c, "cost", None) is not None]
        cost_str = f"${sum(priced):,.2f}" if priced else "&mdash;"
        totals = (
            '<div class="leaf__totals">'
            f'<span class="total"><span class="total__k">Sessions</span>'
            f'<span class="total__n">{len(convs)}</span></span>'
            + (
                f'<span class="total"><span class="total__k">Sub-agents</span>'
                f'<span class="total__n">{len(day_kids)}</span></span>'
                if day_kids else ""
            )
            + f'<span class="total"><span class="total__k">Tokens</span>'
            f'<span class="total__n">{fmt_tokens(tok)}</span></span>'
            f'<span class="total"><span class="total__k">Cost</span>'
            f'<span class="total__n">{cost_str}</span></span>'
            "</div>"
        )

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
            f'<section class="leaf" aria-label="{escape(aria)}">'
            f'<div class="leaf__head">{dateline}'
            f'<div class="leaf__aside">{hist}{totals}</div></div>'
            f'<ul class="entries">{"".join(rows)}</ul></section>'
        )

    ingested = (
        "".join(day_parts)
        if day_parts
        else '<p class="empty">No ingested sessions yet.</p>'
    )
    # The daybook is the column: the live zone (and, in a later slice, the
    # Oculus) enter at the crown, the dated leaves descend below it.
    parts.append(f'<div class="daybook">{live_section}{ingested}</div>')

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


def _idx_pin_button(name: str, pinned: bool, pin_action_url: str) -> str:
    """The pin/unpin toggle shared by index entries (the star before the name)."""
    if not pin_action_url:
        return ""
    import json as _json

    vals = _json.dumps({"action": "unpin" if pinned else "pin", "tag": name})
    star = "★" if pinned else "☆"
    pin_cls = "pin pin--on" if pinned else "pin"
    verb = "unpin" if pinned else "pin"
    pressed = "true" if pinned else "false"
    return (
        f'<button class="{pin_cls}" type="button"'
        f' hx-post="{escape(pin_action_url)}" hx-vals="{escape(vals)}"'
        f' hx-target="#main" hx-swap="innerHTML"'
        f' aria-pressed="{pressed}" aria-label="{verb}"'
        f' title="{verb.title()} {escape(name)}">{star}</button>'
    )


def _idx_entry(
    t: Any, *, display: str, list_base: str, shell_base: str, pin_action_url: str
) -> str:
    """One tag as an ``.idx-entry``: pin · name (drills to Find) · dots · locator.

    ``display`` is the visible label (a leaf inside its namespace group); the
    pin + drill always use the full ``t.name``.
    """
    name = t.name
    pinned = bool(getattr(t, "pinned", False))
    weight, unit = _tag_weight(t)
    drill = _hx_detail(list_base, name, shell_base, key="tag")
    li_cls = "idx-entry is-pinned" if pinned else "idx-entry"
    return (
        f'<li class="{li_cls}">{_idx_pin_button(name, pinned, pin_action_url)}'
        f'<a class="idx-name"{drill}>{escape(display)}</a>'
        f'<span class="idx-dots" aria-hidden="true"></span>'
        f'<span class="idx-loc"><b class="idx-loc__n">{weight:,}</b>'
        f'<i>{escape(unit)}</i></span></li>'
    )


def _marked_entry(t: Any, *, list_base: str, shell_base: str) -> str:
    """A pinned tag in the Marked section — full name, fixed star, locator."""
    name = t.name
    weight, unit = _tag_weight(t)
    drill = _hx_detail(list_base, name, shell_base, key="tag")
    return (
        '<li class="marked__entry">'
        '<span class="marked__star" aria-hidden="true">★</span>'
        f'<a class="idx-name"{drill}>{escape(name)}</a>'
        '<span class="idx-dots" aria-hidden="true"></span>'
        f'<span class="idx-loc"><b class="idx-loc__n">{weight:,}</b>'
        f'<i>{escape(unit)}</i></span></li>'
    )


def render_tags(tags: list, **context: Any) -> str:
    """Render the Swiss 'Tags' view as an index: a Marked section over two books
    — the hand-applied Subject index and the auto-assigned Machine vocabulary —
    each a namespace tree.

    Composition over data: every number comes from owner-scoped
    ``api.tags.list_tags``. 'pinned' is the only stored state; the tree is
    synthesized by splitting flat names on ``:`` (sibling magnitudes normalise
    within each namespace). The Subject/Machine split rides ``TagInfo.auto`` —
    machine vocabulary (shell:* tool families, siftd:derivative) is counted on
    its own grain (calls) and kept out of the subject headline, where it would
    swamp the hand-applied conversation tags. Entries pin/unpin in place and
    drill into Find pre-filtered by the tag.

    Context keys: ``list_base`` (drill target, e.g. ``/find``), ``shell_base``
    (deep-link push prefix), ``pin_action_url`` (pin/unpin POST),
    ``total_conversations`` (optional — the apparatus corpus figure).
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

    if not tags:
        return (
            '<article class="index" data-view="tags" data-title="Tags"'
            ' data-count="0" data-kick="">'
            '<p class="empty">No tags yet.</p></article>'
        )

    def _book(tag_list: list) -> tuple[int, str]:
        """Group a tag list into ``.idx-group`` namespace sections; return
        ``(n_headings, html)``. Namespaces alphabetical, the ungrouped bucket
        last; leaf labels inside a namespace, full names when ungrouped."""
        groups: OrderedDict[str, list] = OrderedDict()
        for t in tag_list:
            ns, _leaf = _tag_namespace(t.name)
            groups.setdefault(ns, []).append(t)
        ordered_ns = sorted(k for k in groups if k) + ([""] if "" in groups else [])
        secs: list[str] = []
        for ns in ordered_ns:
            members = sorted(groups[ns], key=lambda t: _tag_weight(t)[0], reverse=True)
            entries = [
                _idx_entry(t, display=(_tag_namespace(t.name)[1] if ns else t.name), **row_kw)
                for t in members
            ]
            head_label = escape(ns) if ns else "ungrouped"
            secs.append(
                f'<section class="idx-group"><div class="idx-head">{head_label}'
                f'<span class="idx-head__count">{len(members)}</span></div>'
                f'<ul class="idx-entries">{"".join(entries)}</ul></section>'
            )
        return len(ordered_ns), "".join(secs)

    subject = [t for t in tags if not getattr(t, "auto", False)]
    machine = [t for t in tags if getattr(t, "auto", False)]
    marked = [t for t in tags if getattr(t, "pinned", False)]

    n_subj, subj_html = _book(subject)
    n_mach, mach_html = _book(machine)
    total_headings = n_subj + n_mach

    parts: list[str] = []

    # head — kicker + apparatus (the corpus figure only when the route threads it)
    total_conv = context.get("total_conversations")
    drawn = f"Drawn from <b>{total_conv:,}</b> conversations. " if total_conv else ""
    apparatus = (
        f"{drawn}Subject tags are applied by hand and counted by conversation; "
        "machine vocabulary is assigned at ingest and counted on its own grain. "
        "A locator opens Find scoped to that tag."
    )
    parts.append(
        '<section class="index__head"><div class="index__kicker">'
        '<span class="micro">Index</span>'
        f'<span class="index__count">{len(tags):,} entries · {total_headings} headings</span>'
        f'</div><p class="index__apparatus">{apparatus}</p></section>'
    )

    # marked — the pinned tags, principal references (full names)
    if marked:
        rows = "".join(
            _marked_entry(t, list_base=list_base, shell_base=shell_base) for t in marked
        )
        parts.append(
            '<section class="index__marked zone--pinned"><div class="zone__head">'
            '<span class="micro">Marked</span>'
            f'<span class="zone__count">{len(marked)}</span>'
            f'<span class="zone__rule"></span></div><ul class="marked">{rows}</ul></section>'
        )

    # subject index — the hand-applied vocabulary
    if subject:
        parts.append(
            '<section class="index__book"><div class="zone__head">'
            '<span class="micro">Subject index</span>'
            f'<span class="zone__count">{len(subject):,} entries · {n_subj} headings</span>'
            f'<span class="zone__rule"></span></div>'
            f'<div class="index__cols">{subj_html}</div></section>'
        )

    # machine vocabulary — the auto-assigned categories, a quieter concordance
    if machine:
        parts.append(
            '<section class="index__book index__machine"><div class="zone__head">'
            '<span class="micro">Machine vocabulary</span>'
            f'<span class="zone__count">{len(machine):,} entries · auto-applied</span>'
            '<span class="zone__rule"></span></div>'
            '<p class="index__gloss">Categories assigned automatically at ingest — tool '
            'families counted by call, <code>siftd:derivative</code> marking a '
            "sub-agent's own conversation. Kept out of the subject headline, where "
            'their grain would swamp the hand-applied tags.</p>'
            f'<div class="index__cols">{mach_html}</div></section>'
        )

    kick = " · ".join(filter(None, ["pinned" if marked else "", "tree"]))
    return (
        f'<article class="index" data-view="tags" data-title="Tags"'
        f' data-count="{len(tags):,}" data-kick="{kick}">'
        f'{"".join(parts)}</article>'
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


def _fmt_short_date(iso_day: str) -> str:
    """``2026-06-21`` → ``21 Jun`` (the trend bar's data-date / axis label)."""
    from datetime import date

    try:
        d = date.fromisoformat(iso_day)
    except ValueError:
        return iso_day
    return f"{d.day} {d.strftime('%b')}"


def _reck_bars(buckets: list, cls: str, *, dated: bool = False) -> str:
    """Server-emit one chart's bars carrying ``data-tokens``/``data-cost``.

    The bar HEIGHTS are not set here — ``initReck`` in enhance.js scales each
    plot per the active measure (tokens|cost) and marks the peak, so the same
    bars re-draw on a measure toggle without a round-trip. ``dated`` adds a
    ``data-date`` (the daily trend; the rhythm dists key off a static axis).
    Cost is blank when ``None`` so the cost measure reads ``&mdash;``, not $0.
    """
    from siftd.output.common import fmt_tokens

    out: list[str] = []
    for b in buckets:
        cost_attr = "" if b.cost is None else f"{b.cost:.2f}"
        date_attr = ""
        title_lead = f"{b.label} &middot; "
        if dated:
            short = _fmt_short_date(b.label)
            date_attr = f' data-date="{escape(short)}"'
            title_lead = f"{escape(short)} &middot; "
        cost_bit = "" if b.cost is None else f" &middot; ${b.cost:,.2f}"
        title = f"{title_lead}{escape(fmt_tokens(b.tokens))}{cost_bit}"
        out.append(
            f'<div class="{cls}" data-tokens="{b.tokens}" data-cost="{cost_attr}"'
            f'{date_attr} title="{title}"></div>'
        )
    return "".join(out)


def _reck_account(
    groups: list,
    *,
    title: str,
    noun: str,
    label_fn=None,
    limit: int = 8,
    brush_base: str | None = None,
    active: str | None = None,
) -> str:
    """A footed, ranked account (model mix / workspace mix) for the reckoning.

    Rows carry ``data-tokens``/``data-cost`` so ``initReck`` re-sorts them when
    the measure toggles. The overflow beyond ``limit`` collapses into one
    ``account__row--rest`` carrying the summed remainder (so the foot still
    reconciles to the whole), and the foot totals over ALL groups — never just
    the shown head. Cost honesty: a ``None``-cost group renders ``&mdash;``.

    When ``brush_base`` is set, each row's name becomes a click target that
    re-renders the reckoning scoped to that group (``brush_base?model=<name>``,
    whole-fragment ``#main`` swap), so the activity charts focus on it; the
    ``active`` row toggles back to the unscoped view (links to ``brush_base``)
    and is marked ``is-current``.
    """
    from siftd.output.common import fmt_tokens

    def _tok(g: Any) -> int:
        return (g.input_tokens or 0) + (g.output_tokens or 0)

    def _cost_cell(cost: float | None) -> str:
        if cost is None:
            return '<span class="account__cost account__cost--none">&mdash;</span>'
        return f'<span class="account__cost">${cost:,.2f}</span>'

    def _name_cell(raw: str, shown: str) -> tuple[str, str]:
        """Returns (li-class-suffix, name-cell). Brushing makes the name an
        hx anchor; the active group links back to the unscoped view."""
        if not brush_base:
            return "", f'<span class="ledger__name">{escape(shown)}</span>'
        from urllib.parse import quote

        is_active = raw == active
        href = brush_base if is_active else f"{brush_base}?model={quote(raw)}"
        cell = (
            f'<a class="ledger__name" hx-get="{escape(href)}" hx-target="#main"'
            f' hx-swap="innerHTML" hx-push-url="true">{escape(shown)}</a>'
        )
        return (" is-current" if is_active else ""), cell

    head = groups[:limit]
    rest = groups[limit:]
    rows: list[str] = []
    for g in head:
        name = label_fn(g.name) if label_fn else g.name
        tok = _tok(g)
        cost_attr = "" if g.cost is None else f"{g.cost:.2f}"
        active_cls, name_cell = _name_cell(g.name, name)
        rows.append(
            f'<li class="ledger__row{active_cls}" data-tokens="{tok}" data-cost="{cost_attr}">'
            f'<span class="account__rank"></span>'
            f'{name_cell}'
            f'<span class="account__tok">{escape(fmt_tokens(tok))}</span>'
            f'{_cost_cell(g.cost)}</li>'
        )
    if rest:
        rest_tok = sum(_tok(g) for g in rest)
        rest_priced = [g.cost for g in rest if g.cost is not None]
        rest_cost = sum(rest_priced) if rest_priced else None
        cost_attr = "" if rest_cost is None else f"{rest_cost:.2f}"
        rows.append(
            f'<li class="ledger__row account__row--rest" data-tokens="{rest_tok}"'
            f' data-cost="{cost_attr}"><span class="account__rank"></span>'
            f'<span class="ledger__name">{len(rest):,} more {escape(noun)} carried forward</span>'
            f'<span class="account__tok">{escape(fmt_tokens(rest_tok))}</span>'
            f'{_cost_cell(rest_cost)}</li>'
        )
    if not rows:
        rows.append(
            '<li class="ledger__row ledger__empty"><span class="account__rank"></span>'
            '<span class="ledger__name">no usage</span></li>'
        )

    total_tok = sum(_tok(g) for g in groups)
    total_priced = [g.cost for g in groups if g.cost is not None]
    total_cost = sum(total_priced) if total_priced else None

    return (
        '<section class="account">'
        '<div class="account__head">'
        f'<span class="account__title"><span class="micro">{escape(title)}</span>'
        f'<span class="account__hcount">{len(groups):,} {escape(noun)}</span></span>'
        '<span class="account__hk">Tokens</span><span class="account__hk">Cost</span></div>'
        f'<ul class="ledger ledger--account">{"".join(rows)}</ul>'
        '<div class="account__foot">'
        f'<span class="account__flabel">Total &middot; {len(groups):,} {escape(noun)}</span>'
        f'<span class="account__tok">{escape(fmt_tokens(total_tok))}</span>'
        f'{_cost_cell(total_cost)}</div>'
        '</section>'
    )


def _cadence_section(buckets: list) -> str:
    """The reckoning's daily trend, scoped to one workspace and frozen.

    Unlike the Stats reckoning, the cadence has a single measure (activity), so
    bar heights are computed SERVER-side (``--h``) — no measure toggle, no
    enhance.js dependency. The busiest day is marked. Empty span → nothing.
    """
    if not buckets:
        return ""
    peak_tok = max(b.tokens for b in buckets)
    peak = max(buckets, key=lambda b: b.tokens)
    bars: list[str] = []
    for b in buckets:
        h = max(7, round(b.tokens / peak_tok * 100)) if peak_tok else 7
        is_peak = " is-peak" if b is peak else ""
        title = f"{escape(_fmt_short_date(b.label))} &middot; {escape(_fmt_tokens_safe(b.tokens))}"
        bars.append(f'<div class="trend__bar{is_peak}" style="--h:{h}%" title="{title}"></div>')
    span = (
        f"{escape(_fmt_short_date(buckets[0].label))} &ndash; "
        f"{escape(_fmt_short_date(buckets[-1].label))}"
    )
    return (
        '<section class="dossier__cad"><div class="zone__head"><span class="micro">Cadence</span>'
        f'<span class="zone__count">{span} &middot; {len(buckets):,} days &middot; '
        f'busiest {escape(_fmt_short_date(peak.label))}</span>'
        '<span class="zone__rule"></span></div>'
        f'<div class="trend__plot dossier__cadplot">{"".join(bars)}</div>'
        '<div class="chart__unit">activity per day, across the workspace&rsquo;s span</div></section>'
    )


def _fmt_tokens_safe(n: int) -> str:
    from siftd.output.common import fmt_tokens

    return fmt_tokens(n)


def render_dashboard(
    *,
    usage: Any,
    by_model: list,
    by_workspace: list,
    coverage: Any,
    stats: Any,
    distributions: Any = None,
    owner: str | None = None,
    scope_model: str | None = None,
    brush_base: str = "",
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

    def _pct(p: float) -> str:
        # 1dp, and never round UP to a false 100% — 99.87% is not "complete".
        # Coverage measures token *presence* per response, not completeness of
        # the value (cache-read tokens live off this axis), so an honest 99.9%
        # matters: it must not read as "we have everything".
        import math

        shown = math.floor(p * 10) / 10 if p < 100 else 100.0
        return f"{shown:.1f}%"

    # --- standing: the period of account + the three holdings ---
    start, end = window
    period_bits: list[str] = []
    if start or end:
        s = escape(fmt_timestamp(start) or "?")
        e = escape(fmt_timestamp(end) or "?")
        period_bits.append(
            f'<span class="standing__dates"><b>{s}</b><span>through</span><b>{e}</b></span>'
        )
    gathered = _ago(_iso_epoch(last_ingest)) if last_ingest else None
    if gathered:
        period_bits.append(
            f'<span class="standing__gathered">last gathered {escape(gathered)} ago</span>'
        )
    period = (
        '<div class="standing__period"><span class="micro">Period of account</span>'
        f'{"".join(period_bits)}</div>'
        if period_bits
        else ""
    )

    cost_gathered = (
        f'<span class="standing__gathered">priced on {_pct(cost_pct)} of usage</span>'
        if cost_pct is not None
        else ""
    )
    standing = (
        '<section class="reck__standing">'
        f'{period}'
        '<div class="standing__figs">'
        '<div class="standing__fig"><span class="micro">Conversations</span>'
        f'<span class="standing__fign">{convs:,}</span></div>'
        '<div class="standing__fig"><span class="micro">Tokens</span>'
        f'<span class="standing__fign">{escape(fmt_tokens(in_tok + out_tok))}</span>'
        f'<div class="standing__ratio"><i class="seg-in" style="flex:{in_tok or 1}"></i>'
        f'<i class="seg-out" style="flex:{out_tok or 1}"></i></div>'
        f'<span class="standing__legend"><span><i class="seg-in"></i>{escape(fmt_tokens(in_tok))} in</span>'
        f'<span><i class="seg-out"></i>{escape(fmt_tokens(out_tok))} out</span></span></div>'
        '<div class="standing__fig standing__fig--hero"><span class="micro">Cost</span>'
        f'<span class="standing__fign">{headline_cost}</span>{cost_gathered}</div>'
        '</div></section>'
    )

    # --- input economy: the cache lever (unique to LLM usage) -----------------
    # input_tokens is the TRUE TOTAL (uncached + cache_read + cache_creation),
    # so the cache reads are input we DIDN'T pay full freight for — the single
    # biggest cost lever, and otherwise invisible. Only shown when the corpus
    # actually reports cache tokens (many providers don't); honest absence.
    cread = getattr(usage, "total_cache_read_tokens", 0) or 0
    ccreate = getattr(usage, "total_cache_creation_tokens", 0) or 0
    economy = ""
    if in_tok > 0 and (cread + ccreate) > 0:
        uncached = max(0, in_tok - cread - ccreate)
        import math as _math

        hit = _math.floor((cread / in_tok) * 1000) / 10  # 1dp, never rounds up
        economy = (
            '<div class="reck__sechead"><span class="micro">Input economy</span>'
            '<span class="zone__rule"></span><span class="reck__ctl">'
            f'<span class="trend__peak">{hit:.1f}% of input served from cache</span>'
            '</span></div>'
            '<section class="reck__econ">'
            '<div class="standing__ratio reck__econbar">'
            f'<i class="seg-in" style="flex:{uncached or 1}"></i>'
            f'<i class="seg-cache" style="flex:{cread or 1}"></i>'
            f'<i class="seg-fresh" style="flex:{ccreate or 1}"></i></div>'
            '<span class="standing__legend">'
            f'<span><i class="seg-in"></i>{escape(fmt_tokens(uncached))} uncached</span>'
            f'<span><i class="seg-cache"></i>{escape(fmt_tokens(cread))} cache reads</span>'
            f'<span><i class="seg-fresh"></i>{escape(fmt_tokens(ccreate))} cache writes</span>'
            '</span></section>'
        )

    # --- activity over the period + the hour/weekday rhythm ---
    by_day = getattr(distributions, "by_day", []) or []
    by_hour = getattr(distributions, "by_hour", []) or []
    by_dow = getattr(distributions, "by_dow", []) or []

    axis_ticks = ""
    if by_day:
        first = _fmt_short_date(by_day[0].label)
        last = _fmt_short_date(by_day[-1].label)
        mid = _fmt_short_date(by_day[len(by_day) // 2].label)
        axis_ticks = (
            f'<span>{escape(first)}</span><span>{escape(mid)}</span><span>{escape(last)}</span>'
        )

    # chart-brushing: when scoped to a model, label the activity charts with it
    # and offer a "show all" toggle back to the unscoped view (whole #main swap).
    brushing = bool(scope_model and brush_base)
    activity_label = (
        f'Activity &middot; {escape(scope_model or "")}' if brushing else "Activity over the period"
    )
    clear = (
        f'<a class="reck__clear" hx-get="{escape(brush_base)}" hx-target="#main"'
        ' hx-swap="innerHTML" hx-push-url="true">show all &times;</a>'
        if brushing
        else ""
    )
    trend = (
        f'<div class="reck__sechead"><span class="micro">{activity_label}</span>'
        '<span class="zone__rule"></span><span class="reck__ctl">'
        f'{clear}'
        '<span class="trend__peak" id="trend-peak"></span>'
        '<div class="measure" role="radiogroup" aria-label="Measure">'
        '<input type="radio" name="measure" id="m-tok" checked><label for="m-tok">Tokens</label>'
        '<input type="radio" name="measure" id="m-cost"><label for="m-cost">Cost</label>'
        '</div></span></div>'
        '<section class="trend"><div class="chart">'
        '<div class="chart__y"><span id="trend-ymax"></span><span class="chart__y0">0</span></div>'
        '<div class="chart__main">'
        f'<div class="trend__plot" id="trend-plot">{_reck_bars(by_day, "trend__bar", dated=True)}</div>'
        f'<div class="trend__axis">{axis_ticks}</div></div></div>'
        '<div class="chart__unit" id="trend-unit">tokens per day</div></section>'
    )

    rhythm = (
        '<div class="reck__rhythm">'
        '<section class="dist"><div class="zone__head"><span class="micro">By hour of day</span>'
        '<span class="zone__rule"></span></div><div class="chart">'
        '<div class="chart__y chart__y--sm"><span id="hod-ymax"></span>'
        '<span class="chart__y0">0</span></div><div class="chart__main">'
        f'<div class="dist__plot" id="hod-plot">{_reck_bars(by_hour, "dist__bar")}</div>'
        '<div class="dist__axis"><span>00</span><span>06</span><span>12</span>'
        '<span>18</span><span>23</span></div></div></div>'
        '<div class="chart__unit" id="hod-unit">tokens per hour</div></section>'
        '<section class="dist"><div class="zone__head"><span class="micro">By day of week</span>'
        '<span class="zone__rule"></span></div><div class="chart">'
        '<div class="chart__y chart__y--sm"><span id="dow-ymax"></span>'
        '<span class="chart__y0">0</span></div><div class="chart__main">'
        f'<div class="dist__plot dist__plot--dow" id="dow-plot">{_reck_bars(by_dow, "dist__bar")}</div>'
        '<div class="dist__axis"><span>Mon</span><span>Tue</span><span>Wed</span>'
        '<span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span></div></div></div>'
        '<div class="chart__unit" id="dow-unit">tokens per weekday</div></section></div>'
    )

    # --- breakdown: the two footed accounts (model mix · workspace mix) ---
    books = (
        '<div class="reck__sechead"><span class="micro">Breakdown</span>'
        '<span class="zone__rule"></span><span class="reck__ctl">'
        '<span class="trend__peak">ranked by the measure above</span></span></div>'
        '<div class="reck__books">'
        f'{_reck_account(by_model, title="Model mix", noun="models", brush_base=brush_base or None, active=scope_model)}'
        f'{_reck_account(by_workspace, title="Workspace mix", noun="workspaces", label_fn=fmt_workspace)}'
        '</div>'
    )

    # --- colophon: the corpus counts + the trust footnote ---
    cells: list[str] = []

    def _cell(label: str, value: str) -> None:
        cells.append(
            f'<div class="colophon__cell"><span class="micro">{escape(label)}</span>'
            f'<span class="colophon__celln">{escape(value)}</span></div>'
        )

    if counts is not None:
        _cell("Responses", f"{counts.responses:,}")
        _cell("Prompts", f"{counts.prompts:,}")
        _cell("Tool calls", f"{counts.tool_calls:,}")
        _cell("Models", f"{counts.models:,}")
        _cell("Workspaces", f"{counts.workspaces:,}")
        _cell("Harnesses", f"{counts.harnesses:,}")
        _cell("Tools", f"{counts.tools:,}")
    # Rhythm: how concentrated the work is over the period — derived from the
    # same daily series the trend draws (active = a day with any tokens; streak
    # = the longest unbroken run of active days). Zero new query.
    if by_day:
        active = sum(1 for b in by_day if b.tokens > 0)
        streak = best = 0
        for b in by_day:
            streak = streak + 1 if b.tokens > 0 else 0
            best = max(best, streak)
        _cell("Active days", f"{active:,}/{len(by_day):,}")
        _cell("Longest streak", f"{best:,} day" + ("" if best == 1 else "s"))
    note_bits: list[str] = []
    if tok_pct is not None:
        note_bits.append(
            f"Tokens accounted on <b>{_pct(tok_pct)}</b> of responses, floored "
            "&mdash; cache reads sit off this axis."
        )
    if cost_pct is not None:
        note_bits.append(
            f"Cost priced on <b>{_pct(cost_pct)}</b>; the remainder is set as "
            "<b>&mdash;</b>, never <b>$0</b>."
        )
    note = f'<p class="colophon__note">{" ".join(note_bits)}</p>' if note_bits else ""
    sign_bits: list[str] = []
    if last_ingest:
        sign_bits.append(
            f"<span>Last gathered <b>{escape(fmt_timestamp(last_ingest) or '')}</b></span>"
        )
    sign = f'<div class="colophon__sign">{"".join(sign_bits)}</div>' if sign_bits else ""
    colophon = (
        '<section class="reck__colophon"><div class="zone__head">'
        '<span class="micro">Corpus</span><span class="zone__rule"></span></div>'
        f'<div class="colophon__grid">{"".join(cells)}</div>{note}{sign}</section>'
    )

    return (
        '<article class="reck by-tokens" data-view="stats" data-title="Stats"'
        f' data-count="{convs}" data-kick="{kick}">'
        f'{standing}{economy}{trend}{rhythm}{books}{colophon}</article>'
    )


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
    find_base = context.get("find_base", "")  # /find for the tag chips

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

    cadence_sec = _cadence_section(getattr(detail, "cadence", []) or [])

    tags_sec = ""
    ws_tags = getattr(detail, "tags", []) or []
    if ws_tags:
        chips: list[str] = []
        for name, n in ws_tags:
            drill = _hx_detail(find_base, name, shell_base, key="tag")
            chips.append(
                f'<a class="dossier__tag"{drill}>{escape(name)}'
                f'<span class="dossier__tagn">{n:,}</span></a>'
            )
        tags_sec = (
            '<section class="dossier__tags"><div class="zone__head">'
            '<span class="micro">What it&rsquo;s about</span>'
            f'<span class="zone__count">{len(ws_tags):,} tags</span>'
            '<span class="zone__rule"></span></div>'
            f'<div class="dossier__chips">{"".join(chips)}</div></section>'
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
        f"{bar}{head}{cadence_sec}{tags_sec}{models}{recent}</article>"
    )
