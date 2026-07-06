"""Bridge normalized narrative data onto painted rendering primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from siftd.domain.search_types import ROLE_ASSISTANT, ROLE_USER
from siftd.output._id_format import short_id
from siftd.output.common import (
    fmt_count,
    fmt_timestamp,
    fmt_tokens,
    fmt_workspace,
    format_refs_annotation,
    prefers_ascii,
    role_label,
    split_match_segments,
    term_width,
    truncate_text,
)

if TYPE_CHECKING:
    from painted import Block, Fidelity, Line, Style

    from siftd.output.theme import DomainStyles
    from siftd.output.tool_presenters import ToolPresentation


@dataclass(frozen=True)
class _RoleStyles:
    heading: Style
    meta: Style
    prompt: Style
    assistant: Style
    thinking: Style
    tool: Style
    tool_input: Style
    tool_result: Style
    tool_error: Style
    summary_hint: Style


def _painted():
    from painted import Block, Line, Span, Style, current_palette, join_vertical, print_block

    return Block, Line, Span, Style, current_palette, join_vertical, print_block


def _line(*parts: tuple[str, Style]) -> Line:
    # The shared row atom — drops empty segments, identical span construction.
    from siftd.output.row import row_line

    return row_line(parts)


def _blank_block() -> Block:
    Block, _, _, _, _, _, _ = _painted()
    return Block.empty(0, 1)


def _line_block(line: Line) -> Block:
    return line.to_block(line.width) if line.width > 0 else _blank_block()


def _lines_to_block(lines: list[Line]) -> Block:
    Block, _, _, _, _, join_vertical, _ = _painted()
    if not lines:
        return Block.empty(0, 0)
    return join_vertical(*[_line_block(line) for line in lines])


def print_block(block: Block) -> None:
    """Print a painted block, honouring TTY + NO_COLOR for ANSI/plain behavior."""
    from siftd.output.common import should_use_ansi

    _, _, _, _, _, _, painted_print_block = _painted()
    painted_print_block(block, use_ansi=should_use_ansi())


def emit_output(result) -> None:
    """Dispatch formatter output to the appropriate printer.

    Handles str (markdown/terminal), dict (json), and Block (painted terminal).
    No-ops on falsy values.
    """
    if not result:
        return
    if isinstance(result, str):
        print(result)
    elif isinstance(result, dict):
        import json as json_mod

        print(json_mod.dumps(result, indent=2, default=str))
    else:
        print_block(result)


def _append_multiline(
    lines: list[Line],
    prefix: str,
    prefix_style: Style,
    text: str,
    text_style: Style,
    limit: int,
) -> None:
    rendered = truncate_text(text.strip(), limit)
    if not rendered:
        return
    split = rendered.splitlines() or [rendered]

    lines.append(_line((prefix, prefix_style), (split[0], text_style)))
    continuation = " " * len(prefix)
    for part in split[1:]:
        lines.append(_line((continuation, prefix_style), (part, text_style)))


def _body_render_ctx() -> tuple[int, bool]:
    """(width, ascii_mode) for rendering a markdown body to the current stream.

    Bodies word-wrap to the terminal width (the content budget is a *separate*
    axis, applied upstream by the walker / ``truncate_text``); ``ascii_mode``
    degrades the markdown glyphs (bullets/rules/quote gutters) on a non-Unicode
    stream, the same gate the table/listing/status surfaces use.
    """
    return term_width(), prefers_ascii()


def _body_parts(text: str, ds: DomainStyles, *, width: int, ascii_mode: bool, limit: int) -> list:
    """Render a (markdown) body of text into a list of Blocks.

    Caps the text to the content budget, parses markdown, then groups the
    resulting Lines into Blocks (tables pass through as their own Block). The
    shared body renderer for user prompts, response-text fallbacks, and follow
    text; the assistant narrative routes the same ``render_markdown`` through
    ``PaintedEmitter.text`` so every body speaks one renderer.
    """
    from siftd.output.markdown_render import render_markdown

    _, Line, _, _, _, _, _ = _painted()
    rendered = truncate_text(text.strip(), limit)
    if not rendered:
        return []
    parts: list = []
    run: list = []
    for item in render_markdown(rendered, ds, width, ascii_mode=ascii_mode):
        if isinstance(item, Line):
            run.append(item)
        else:
            if run:
                parts.append(_lines_to_block(run))
                run = []
            parts.append(item)
    if run:
        parts.append(_lines_to_block(run))
    return parts


# ---------------------------------------------------------------------------
# Tool content → painted lines (via format-neutral ToolPresentation)
# ---------------------------------------------------------------------------

_TOOL_INDENT = "      "  # 6-space indent, consistent with current convention


def _presentation_to_lines(
    pres: ToolPresentation,
    styles: _RoleStyles,
    width: int | None = None,
) -> list[Line]:
    """Convert a format-neutral ToolPresentation into painted Lines.

    The headline (a tool's command) and each output line word-wrap to ``width``
    so a long command/log reflows to the tool indent — aligned with the rest of
    the feed — rather than spilling to column 0 under the gutter. ``width=None``
    (natural sizing) keeps the unwrapped form.
    """
    lines: list[Line] = []

    def emit(indent: str, spans: list[tuple[str, Style]]) -> None:
        # Word-wrap one logical line to the content width, every wrapped line at
        # `indent`; the rail then prefixes each so the run stays railed.
        if not width:
            lines.append(_line((indent, styles.meta), *spans))
            return
        _, Line_p, Span, _, _, _, _ = _painted()
        avail = max(width - len(indent), 10)
        for wl in _wrap_spans([Span(t, s) for t, s in spans], avail):
            lines.append(Line_p(spans=(Span(indent, styles.meta), *wl.spans)))

    # Headline + optional meta suffix
    if pres.headline:
        head: list[tuple[str, Style]] = [(pres.headline, styles.tool)]
        if pres.meta:
            head.append((f" ({pres.meta})", styles.meta))
        emit(_TOOL_INDENT, head)

    # Diff (edit tool)
    if pres.removed:
        _append_multiline(lines, _TOOL_INDENT + "- ", styles.tool_input, pres.removed, styles.tool_input, 0)
    if pres.added:
        _append_multiline(lines, _TOOL_INDENT + "+ ", styles.meta, pres.added, styles.tool_result, 0)

    # Tasks (todo tool)
    for text, done in pres.tasks:
        check = "✓" if done else "○"
        lines.append(_line((_TOOL_INDENT + f"  {check} ", styles.meta), (text, styles.tool_input)))

    # Output preview (already truncated by extractor)
    if pres.output:
        for out_line in pres.output.splitlines():
            emit(_TOOL_INDENT, [(out_line, styles.tool_result)])
    if pres.overflow > 0:
        lines.append(_line((_TOOL_INDENT + f"... +{pres.overflow} more lines", styles.summary_hint)))

    # Error
    if pres.error:
        _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, pres.error, styles.tool_error, 0)

    return lines


def _render_tool_content_lines(
    name: str,
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
    width: int | None = None,
) -> list[Line]:
    """Extract tool presentation then render as painted lines."""
    from siftd.output.tool_presenters import extract_tool_presentation

    pres = extract_tool_presentation(name, raw_input, raw_result, status, tool_chars)
    return _presentation_to_lines(pres, styles, width)



_DEFAULT_TOOL_CHARS = 120


def _tool_density(fidelity: Fidelity) -> int:
    """Derive tool content char limit from fidelity."""
    if fidelity.depth >= 3:
        return 0  # full depth = no truncation
    if fidelity.chars > 0:
        return fidelity.chars  # match text density
    return _DEFAULT_TOOL_CHARS


# ---------------------------------------------------------------------------
# Painted emitter — NarrativeEmitter that builds painted Blocks
# ---------------------------------------------------------------------------


class PaintedEmitter:
    """Accumulates painted Blocks from narrative walker events.

    After walk_narrative() returns, call .result() to get the composed Block.
    """

    def __init__(
        self,
        ds: DomainStyles,
        tool_chars: int,
        *,
        width: int | None = None,
        ascii_mode: bool = False,
    ) -> None:
        from siftd.output.gutter import GUTTER_COLS

        self._ds = ds
        self._tool_chars = tool_chars
        # Content wraps to width − the gutter rail, so a rail-prefixed line lands
        # at exactly the requested width (natural sizing keeps None).
        self._width = (width - GUTTER_COLS) if width else width
        self._ascii = ascii_mode
        self._role_styles = _RoleStyles(
            heading=ds.label,
            meta=ds.separator,
            prompt=ds.prompt,
            assistant=ds.assistant,
            thinking=ds.thinking,
            tool=ds.tool_name,
            tool_input=ds.tool_input,
            tool_result=ds.tool_result,
            tool_error=ds.tool_error,
            summary_hint=ds.summary,
        )
        self._parts: list[Block] = []
        self._pending: list[Line] = []
        # The grain-gutter key for the current pending run — (kind, status); a
        # kind change banks the run so the rail switches mark at block boundaries.
        self._pending_kind: tuple[str, str | None] | None = None
        self._content_since_break = False

    def _flush_lines(self) -> None:
        """Bank the current run as one gutter-railed Block."""
        if self._pending:
            block = _lines_to_block(self._pending)
            if self._pending_kind is not None:
                block = self._gutter_block(block, *self._pending_kind)
            self._parts.append(block)
            self._pending = []
            self._content_since_break = True

    def _set_kind(self, kind: str, status: str | None = None) -> None:
        """Switch the pending run's gutter kind, banking the run if it changes."""
        key = (kind, status)
        if self._pending and key != self._pending_kind:
            self._flush_lines()
        self._pending_kind = key

    def _gutter_block(self, block: Block, kind: str, status: str | None = None) -> Block:
        from siftd.output.gutter import apply_event_gutter

        return apply_event_gutter(block, kind, status=status, ascii_mode=self._ascii)

    def _block_break(self) -> None:
        """An ungutterred blank line between blocks — breaks the rail, never doubled."""
        self._flush_lines()
        if self._content_since_break:
            self._parts.append(_blank_block())
            self._content_since_break = False
        self._pending_kind = None

    # -- NarrativeEmitter interface --

    def text(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        # Walker already truncated; render markdown structure onto painted spans.
        # Line-shaped elements join the pending run; a table flushes and lands as
        # its own Block — same interleaving discipline as tool_content.
        from siftd.output.markdown_render import render_markdown

        _, Line, _, _, _, _, _ = _painted()
        self._block_break()
        self._set_kind("assistant")
        for item in render_markdown(content, self._ds, self._width, ascii_mode=self._ascii):
            if isinstance(item, Line):
                self._pending.append(item)
            else:
                self._flush_lines()
                self._parts.append(self._gutter_block(item, "assistant"))
                self._content_since_break = True

    def thinking(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        # No box — a dim `thinking` label over the reasoning, italic via
        # ds.thinking and indented, word-wrapped to the width. Typography over
        # chrome: a transcript is a feed, and a box rule fights a variable-width
        # body (it spanned the terminal and the content overflowed it anyway).
        _, Line, Span, Style_p, _, _, _ = _painted()
        indent = "      "
        avail = max((self._width or term_width()) - len(indent), 20)
        # A label that pops: a warm ✻ glyph (amber, the gold thread) + the word
        # in bold, over the dim italic reasoning. Glyph degrades to * for ASCII.
        glyph = "* " if self._ascii else "✻ "
        self._block_break()
        self._set_kind("thinking")
        self._pending.append(
            _line(
                ("    ", self._ds.separator),
                (glyph, self._ds.metric),
                ("thinking", self._ds.summary.merge(Style_p(bold=True))),
            )
        )
        for para in content.strip().split("\n"):
            if not para.strip():
                self._pending.append(_line())
                continue
            for wl in _wrap_spans([Span(para.strip(), self._ds.thinking)], avail):
                self._pending.append(Line(spans=(Span(indent, self._ds.separator), *wl.spans)))

    def thinking_placeholder(self, *, event_id: str | None = None) -> None:
        del event_id
        self._block_break()
        self._set_kind("thinking")
        glyph = "* " if self._ascii else "✻ "
        self._pending.append(
            _line(
                ("    ", self._ds.separator),
                (glyph, self._ds.metric),
                ("thinking", self._ds.summary),
            )
        )

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        self._block_break()
        # Each tool line takes its own outcome in the rail — _set_kind banks the
        # run when the status changes, so a failed call shows ✗ amid the ✓s.
        for name, count, status in tools:
            self._set_kind("tool", status)
            self._pending.extend(_tool_summary_lines_styled([(name, count, status)], self._ds))

    def tool_content(
        self,
        name: str,
        count: int,
        raw_input: str | None,
        raw_result: str | None,
        status: str | None,
        *,
        event_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        del event_id, tool_call_id
        # A header line — the `→ name` idiom the collapsed tool summary already
        # uses — over the input/result indented beneath. No box (typography over
        # chrome); the call now reads the same expanded or collapsed.
        self._block_break()
        self._set_kind("tool", status)
        title_style = self._ds.tool_error if status == "error" else self._ds.tool_name
        header: list[tuple[str, Style]] = [("    → ", self._ds.separator), (name, title_style)]
        if count > 1:
            header.append((f" ×{count}", self._ds.separator))
        if status and status != "success":
            status_style = self._ds.tool_error if status == "error" else self._ds.separator
            header.append((f" ({status})", status_style))
        self._pending.append(_line(*header))
        self._pending.extend(
            _render_tool_content_lines(
                name, raw_input, raw_result, status, self._role_styles, self._tool_chars, self._width
            )
        )

    def tool_output(self, block_type: str, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self._block_break()
        self._set_kind("tool")
        _append_multiline(
            self._pending,
            f"  [{block_type}] ",
            self._ds.summary,
            content,
            self._ds.tool_result,
            self._tool_chars,
        )

    def result(self) -> Block:
        Block, _, _, _, _, join_vertical, _ = _painted()
        self._flush_lines()
        if not self._parts:
            return Block.empty(0, 0)
        if len(self._parts) == 1:
            return self._parts[0]
        return join_vertical(*self._parts)


def render_narrative_block(
    blocks: list,
    *,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render narrative blocks into a composed painted Block.

    Delegates to walk_narrative() for fidelity gating (what to show),
    PaintedEmitter for rendering (how to show it).
    """
    from siftd.output.theme import domain_styles
    from siftd.serialization.narrative import walk_narrative

    ds = domain_styles(fidelity)
    effective_tool_chars = tool_chars or _tool_density(fidelity)
    width, ascii_mode = _body_render_ctx()
    emitter = PaintedEmitter(ds, effective_tool_chars, width=width, ascii_mode=ascii_mode)
    walk_narrative(blocks, emitter, fidelity=fidelity, tool_chars=effective_tool_chars)
    return emitter.result()


def _tool_summary_lines_styled(
    tools: list[tuple[str, int, str | None]],
    ds: DomainStyles,
) -> list[Line]:
    """Render tool summary lines from (name, count, status) tuples."""
    lines: list[Line] = []
    for name, count, status in tools:
        parts: list[tuple[str, Style]] = [
            ("    → ", ds.separator),
            (name, ds.tool_name),
        ]
        if count > 1:
            parts.append((f" ×{count}", ds.separator))
        if status:
            status_style = ds.tool_error if status == "error" else ds.separator
            parts.append((f" ({status})", status_style))
        lines.append(_line(*parts))
    return lines


def _tool_summary_lines(
    tools: list[tuple[str, int, str | None]],
) -> list[Line]:
    """Render tool summary lines using ambient theme."""
    from siftd.output.theme import domain_styles

    return _tool_summary_lines_styled(tools, domain_styles())


def _peek_workspace(info) -> str:
    workspace = getattr(info, "workspace_name", None) or fmt_workspace(getattr(info, "workspace_path", None))
    branch = getattr(info, "branch", None)
    if branch:
        return f"{workspace} [{branch}]" if workspace else f"[{branch}]"
    return workspace


def _fmt_last_activity(epoch_seconds: float | None) -> str:
    if not epoch_seconds:
        return ""
    return datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M")


def _gutter_ctx(width: int | None, ascii_mode: bool):
    """``(body_width, gut)`` for guttering a detail view's turn blocks.

    The grain gutter takes 2 columns, so a turn's bodies render at
    ``width − GUTTER_COLS`` and the rail prefix brings them back to ``width``;
    ``gut(block, kind, status=None)`` prepends that kind's rail (user / assistant
    / tool) so the prompt + role headers carry the rail like the narrative does.
    """
    from siftd.output.gutter import GUTTER_COLS, apply_event_gutter

    body_width = (width - GUTTER_COLS) if width else width

    def gut(block, kind, status=None):
        return apply_event_gutter(block, kind, status=status, ascii_mode=ascii_mode)

    return body_width, gut


def conversation_header_pairs(detail, ds: DomainStyles) -> list[tuple[str, list[tuple[str, Style]]]]:
    """The conversation-summary header as ``definitions`` pairs.

    The single source of the themed metadata header — ids ride ``ds.identifier``,
    the token line rides the amber metric thread (grand-total bright, the in/out
    split dim). Both the detail-view block (``render_query_detail_block``) and the
    ``--summary`` path (``cli.query``) compose this so the two reads stay identical.
    """
    ws_name = fmt_workspace(detail.workspace_path)
    started = fmt_timestamp(detail.started_at)
    total_tokens = detail.total_input_tokens + detail.total_output_tokens

    header_pairs: list[tuple[str, list[tuple[str, Style]]]] = [
        ("Conversation", [(detail.id, ds.identifier)]),
    ]
    if ws_name:
        header_pairs.append(("Workspace", [(ws_name, ds.workspace)]))
    header_pairs.append(("Started", [(started, ds.temporal)]))
    header_pairs.append(("Model", [(detail.model or "unknown", ds.model)]))
    header_pairs.append((
        "Tokens",
        [
            (fmt_tokens(total_tokens), ds.metric_strong),
            (
                f" (input: {fmt_tokens(detail.total_input_tokens)} / output: {fmt_tokens(detail.total_output_tokens)})",
                ds.metric,
            ),
        ],
    ))
    if detail.tags:
        header_pairs.append(("Tags", [(", ".join(detail.tags), ds.tag)]))
    return header_pairs


def render_conversation_summary_block(detail, *, fidelity: Fidelity) -> Block:
    """Render the ``query --summary`` metadata-only view as a painted block.

    The detail view's header (``conversation_header_pairs``) plus a ``Turns:``
    count on the amber metric thread — no turn bodies. Single-sources the header
    with ``render_query_detail_block`` so a summary reads identically to a full
    detail's top.
    """
    from siftd.output.listing import definitions
    from siftd.output.theme import domain_styles

    ds = domain_styles(fidelity)
    pairs = conversation_header_pairs(detail, ds)
    pairs.append(("Turns", [(fmt_count(len(detail.turns)), ds.metric)]))
    return definitions(pairs, indent=0)


def render_query_detail_block(
    detail,
    *,
    turns: list,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render a conversation detail view as a painted block."""
    from siftd.output.listing import definitions
    from siftd.output.theme import domain_styles

    Block, _, _, _, _, join_vertical, _ = _painted()

    ds = domain_styles(fidelity)
    parts: list[Block] = []

    event_tags: dict[str, list[tuple[str, str]]] = getattr(detail, "event_tags", None) or {}

    def _tag_span(*event_ids: str | None):
        """The tag-chip trailer for an event's meta line, or None if untagged."""
        names: list[str] = []
        for eid in event_ids:
            if eid:
                names.extend(name for name, _kind in event_tags.get(eid, []))
        if not names:
            return None
        # Dedup preserving order (a prompt carries its own + exchange tags).
        seen: dict[str, None] = {}
        for n in names:
            seen.setdefault(n, None)
        return (f"  {' '.join('#' + n for n in seen)}", ds.tag)

    header_pairs = conversation_header_pairs(detail, ds)
    parts.append(definitions(header_pairs, indent=0))
    parts.append(_blank_block())

    width, ascii_mode = _body_render_ctx()
    body_width, gut = _gutter_ctx(width, ascii_mode)
    for turn in turns:
        ts = fmt_timestamp(turn.timestamp, time_only=True)
        prompt_role_label = getattr(turn, "PROMPT_ROLE_LABEL", ROLE_USER)
        response_role_label = getattr(turn, "RESPONSE_ROLE_LABEL", ROLE_ASSISTANT)

        if turn.prompt_text:
            _prompt_meta = [*_role_prefix(prompt_role_label, ds, abbrev=False), (ts, ds.temporal)]
            _pt = _tag_span(getattr(turn, "prompt_id", None))
            if _pt:
                _prompt_meta.append(_pt)
            parts.append(gut(_line_block(_line(*_prompt_meta)), "user"))
            parts.extend(gut(p, "user") for p in _body_parts(turn.prompt_text, ds, width=body_width, ascii_mode=ascii_mode, limit=fidelity.chars))
            parts.append(_blank_block())

        tool_summaries = turn.tool_call_summaries
        has_response = bool(turn.narrative) or turn.total_input_tokens or turn.total_output_tokens or tool_summaries
        if not has_response:
            continue

        tok = turn.total_input_tokens + turn.total_output_tokens
        _resp_meta = [
            *_role_prefix(response_role_label, ds, abbrev=False),
            (ts, ds.temporal),
            (f" ({fmt_tokens(tok)} tok)", ds.metric),
        ]
        _rt = _tag_span(*getattr(turn, "response_ids", []), *getattr(turn, "tool_call_ids", []))
        if _rt:
            _resp_meta.append(_rt)
        parts.append(
            gut(
                _line_block(_line(*_resp_meta)),
                "assistant",
            )
        )

        if turn.narrative:
            parts.append(render_narrative_block(
                turn.narrative,
                fidelity=fidelity,
                tool_chars=tool_chars,
            ))
        elif tool_summaries:
            parts.append(gut(_lines_to_block(_tool_summary_lines(
                [(tc.tool_name, tc.count, tc.status) for tc in tool_summaries]
            )), "tool"))

        parts.append(_blank_block())

    if not parts:
        return Block.empty(0, 0)
    return join_vertical(*parts)


def render_peek_detail_block(
    detail,
    *,
    exchanges: list,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render a peek session detail view as a painted block."""
    from siftd.output.listing import definitions
    from siftd.output.theme import domain_styles

    Block, _, _, _, _, join_vertical, _ = _painted()

    ds = domain_styles(fidelity)
    parts: list[Block] = []

    info = detail.info
    ws_name = _peek_workspace(info)
    started = fmt_timestamp(detail.started_at)
    last_activity = _fmt_last_activity(getattr(info, "last_activity", None))
    shown_exchanges = len(exchanges)
    total_exchanges = getattr(info, "exchange_count", 0) or shown_exchanges
    exchanges_text = str(total_exchanges)
    if shown_exchanges and total_exchanges > shown_exchanges:
        exchanges_text = f"{shown_exchanges} shown / {total_exchanges} total"

    header_pairs: list[tuple[str, list[tuple[str, Style]]]] = [
        ("Session", [(info.session_id, ds.identifier)]),
    ]
    if ws_name:
        header_pairs.append(("Workspace", [(ws_name, ds.workspace)]))
    if started:
        header_pairs.append(("Started", [(started, ds.temporal)]))
    if last_activity:
        header_pairs.append(("Last activity", [(last_activity, ds.temporal)]))
    header_pairs.append(("Model", [(info.model or "unknown", ds.model)]))
    header_pairs.append(("Adapter", [(info.adapter_name or "unknown", ds.adapter)]))
    header_pairs.append(("Exchanges", [(exchanges_text, ds.metric)]))
    if getattr(info, "parent_session_id", None):
        header_pairs.append(("Parent", [(info.parent_session_id, ds.identifier)]))
    header_pairs.append(("File", [(str(info.file_path), ds.workspace)]))
    parts.append(definitions(header_pairs, indent=0))
    parts.append(_blank_block())

    width, ascii_mode = _body_render_ctx()
    body_width, gut = _gutter_ctx(width, ascii_mode)
    for exchange in exchanges:
        ts = fmt_timestamp(exchange.timestamp, time_only=True)
        prompt_role_label = getattr(exchange, "PROMPT_ROLE_LABEL", ROLE_USER)
        response_role_label = getattr(exchange, "RESPONSE_ROLE_LABEL", ROLE_ASSISTANT)

        if exchange.prompt_text:
            parts.append(gut(_line_block(_line(*_role_prefix(prompt_role_label, ds, abbrev=False), (ts, ds.temporal))), "user"))
            parts.extend(gut(p, "user") for p in _body_parts(exchange.prompt_text, ds, width=body_width, ascii_mode=ascii_mode, limit=fidelity.chars))
            parts.append(_blank_block())

        has_response = bool(
            exchange.narrative
            or exchange.response_text
            or exchange.tool_calls
            or exchange.input_tokens
            or exchange.output_tokens
        )
        if not has_response:
            continue

        total_tokens = exchange.input_tokens + exchange.output_tokens
        parts.append(
            gut(
                _line_block(
                    _line(
                        *_role_prefix(response_role_label, ds, abbrev=False),
                        (ts, ds.temporal),
                        (f" ({fmt_tokens(total_tokens)} tok)", ds.metric),
                    )
                ),
                "assistant",
            )
        )

        if exchange.narrative:
            parts.append(render_narrative_block(
                exchange.narrative,
                fidelity=fidelity,
                tool_chars=tool_chars,
            ))
        elif exchange.response_text:
            parts.extend(gut(p, "assistant") for p in _body_parts(exchange.response_text, ds, width=body_width, ascii_mode=ascii_mode, limit=fidelity.chars))

        if not exchange.narrative and exchange.tool_calls:
            parts.append(gut(_lines_to_block(_tool_summary_lines(
                [(name, count, None) for name, count in exchange.tool_calls]
            )), "tool"))

        parts.append(_blank_block())

    if not parts:
        return Block.empty(0, 0)
    return join_vertical(*parts)


def render_follow_event_block(
    event,
    *,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render a single follow-mode event as a painted block."""
    from siftd.output.theme import domain_styles

    Block, _, _, _, _, join_vertical, _ = _painted()

    ds = domain_styles(fidelity)
    ts = fmt_timestamp(getattr(event, "timestamp", None), time_only=True)
    width, ascii_mode = _body_render_ctx()
    body_width, gut = _gutter_ctx(width, ascii_mode)

    if getattr(event, "is_user", False):
        parts: list[Block] = [gut(_line_block(_line(*_role_prefix(ROLE_USER, ds, abbrev=False), (ts, ds.temporal))), "user")]
        text = getattr(event, "text", None)
        if text:
            parts.extend(gut(p, "user") for p in _body_parts(text, ds, width=body_width, ascii_mode=ascii_mode, limit=fidelity.chars))
        return parts[0] if len(parts) == 1 else join_vertical(*parts)

    total_tokens = getattr(event, "input_tokens", 0) + getattr(event, "output_tokens", 0)
    header_parts: list[tuple[str, Style]] = [
        *_role_prefix(ROLE_ASSISTANT, ds, abbrev=False),
        (ts, ds.temporal),
    ]
    if total_tokens:
        header_parts.append((f" ({fmt_tokens(total_tokens)} tok)", ds.metric))

    parts = [gut(_line_block(_line(*header_parts)), "assistant")]

    narrative = getattr(event, "narrative", [])
    if narrative:
        parts.append(render_narrative_block(
            narrative,
            fidelity=fidelity,
            tool_chars=tool_chars,
        ))
    else:
        text = getattr(event, "text", None)
        if text:
            parts.extend(gut(p, "assistant") for p in _body_parts(text, ds, width=body_width, ascii_mode=ascii_mode, limit=fidelity.chars))
        tool_calls = getattr(event, "tool_calls", [])
        if tool_calls:
            parts.append(gut(_lines_to_block(_tool_summary_lines(
                [(name, count, None) for name, count, *_ in tool_calls]
            )), "tool"))

    if len(parts) == 1:
        return parts[0]
    return join_vertical(*parts)


def _fmt_cost(c) -> str:
    """Cost cell — '?' when cost is unknown, else dollar amount.

    Cost of 0 renders as "$0.0000" (truly free); None renders as "?". The
    caveat layer explains *why* a cost is unknown; the renderer just
    doesn't lie about it.
    """
    if c.cost is None:
        return "?"
    return f"${c.cost:.4f}"


def _caveat_footer_block(caveats: list, fidelity: Fidelity) -> Block | None:
    """One-line footer summarizing caveat kinds present in the result.

    Row-scope caveats (target set) get a count line; query-scope caveats
    (target=None) get a single line per kind. Rendered muted below the table.
    """
    if not caveats:
        return None

    import sys

    from painted import current_palette

    p = current_palette()
    # Wrap to the terminal so a long caveat message doesn't overflow the line
    # right under the now-width-budgeted table. None (piped) = natural width.
    width = term_width() if sys.stdout.isatty() else None

    by_kind_rows: dict[str, int] = {}
    by_kind_query: list[str] = []
    for c in caveats:
        if c.target:
            by_kind_rows[c.check] = by_kind_rows.get(c.check, 0) + 1
        else:
            by_kind_query.append(c.message)

    lines: list[Line] = []

    def _emit(text: str) -> None:
        line = _line((text, p.muted))
        if width is not None and line.width > width:
            lines.extend(_wrap_spans(list(line.spans), width))
        else:
            lines.append(line)

    for kind, count in sorted(by_kind_rows.items()):
        _emit(f"{count} row(s) with {kind}")
    for msg in by_kind_query:
        _emit(msg)

    if not lines:
        return None
    return _lines_to_block(lines)


def render_list_block(
    summaries: list,
    fidelity: Fidelity,
    *,
    caveats: list | None = None,
) -> Block | None:
    """Render conversation list as a styled painted table.

    Depth controls which columns are visible:
        0 (brief): id, timestamp, workspace
        1-2 (default): + model, turns, tokens, cost
        3+ (full): + prompts, responses, tags

    `caveats`, when provided, drives a per-kind footer summarizing the
    caveats below the table. Cost cells render `None` as "?" and `0` as
    "$0.0000" purely from the row's value — independent of caveats.

    Returns None for empty lists (emit_output no-ops on None).
    """
    if not summaries:
        return None

    from painted import Align, join_vertical

    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace
    from siftd.output.table import Col, render_table, table_budget
    from siftd.output.theme import domain_styles

    ds = domain_styles(fidelity)
    depth = fidelity.depth

    # Columns name their semantic role (ds.*), not a raw palette slot: temporal and
    # metric both rode `p.muted` before the theme split them (metric → amber thread).
    cols: list[Col] = [
        Col("id", lambda c: short_id(c.id) if c.id else "", style=ds.identifier),
        Col("started_at", lambda c: fmt_timestamp(c.started_at), style=ds.temporal),
        Col(
            "workspace",
            lambda c: fmt_workspace(c.workspace_path),
            fill=True,
            min_width=12,
            ellipsis_left=True,
        ),
    ]
    if depth >= 1:
        cols.extend([
            Col("model", lambda c: fmt_model(c.model) if c.model else "", style=ds.model),
            Col("turns", lambda c: fmt_count(c.prompt_count), style=ds.metric, align=Align.END),
            Col("tokens", lambda c: fmt_tokens(c.total_tokens), style=ds.metric, align=Align.END),
        ])
    if depth >= 3:
        cols.extend([
            Col("cost", lambda c: _fmt_cost(c), style=ds.metric, align=Align.END),
            Col("responses", lambda c: fmt_count(c.response_count), style=ds.metric, align=Align.END),
            Col("tags", lambda c: ", ".join(c.tags) if c.tags else "", style=ds.tag),
        ])

    width, as_ascii = table_budget()
    table_block = render_table(cols, summaries, width=width, as_ascii=as_ascii)
    footer = _caveat_footer_block(caveats or [], fidelity)
    if footer is None:
        return table_block
    return join_vertical(table_block, footer)


def render_peek_list_block(
    sessions: list,
    children_by_parent: dict[str, list],
) -> Block | None:
    """Render peek session list as a styled painted table.

    Returns None for empty lists.
    """
    if not sessions:
        return None

    import time

    from siftd.output import fmt_ago, fmt_model
    from siftd.output.table import Col, render_table, table_budget
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    now = time.time()

    def _workspace(s) -> str:
        ws = s.workspace_name or ""
        if s.branch:
            return f"{ws} [{s.branch}]" if ws else f"[{s.branch}]"
        return ws

    def _exchanges(s) -> str:
        if s.preview_available:
            return f"{s.exchange_count} exchanges"
        return "(preview unavailable)"

    def _suffix(s) -> str:
        child_count = len(children_by_parent.get(s.session_id, []))
        return f"+{child_count} agents" if child_count > 0 else ""

    cols: list[Col] = [
        Col("session", lambda s: short_id(s.session_id), style=ds.identifier),
        Col("workspace", _workspace, fill=True, min_width=12, ellipsis_left=True),
        Col("activity", lambda s: fmt_ago(now - s.last_activity), style=ds.temporal),
        Col("exchanges", _exchanges, style=ds.metric),
        Col("model", lambda s: fmt_model(s.model), style=ds.model),
        Col("adapter", lambda s: s.adapter_name or "", style=ds.adapter),
        Col("agents", _suffix, style=ds.agent),
    ]

    width, as_ascii = table_budget()
    return render_table(cols, sessions, width=width, as_ascii=as_ascii)


# ---------------------------------------------------------------------------
# Search results → painted Block
#
# Matched terms arrive wrapped in FTS5 snippet() delimiters (>>>...<<<); they
# become accent spans instead of literal markers. A left rail encodes relevance
# rank (top hit promoted, tail dim) so the score stops competing for attention.
# ---------------------------------------------------------------------------

# Disclosure gradient for chunk results: the top hits render the full snippet
# (word-wrapped to the width, untruncated) so the first screen is useful at a
# glance; the rest collapse to one line. The same count drives the rail tiers
# (◆ top hit / │ rest of the expanded head / · the collapsed tail), so they stay
# in lock-step. The head is adaptive — see _expand_count.
_EXPAND_HEAD = 3       # full-snippet head once the result set is "large"
_EXPAND_ALL_MAX = 5    # at or below this many results, expand every hit


def _expand_count(n: int) -> int:
    """How many top hits render the full snippet (the rest collapse to one line).

    Adaptive: a small result set expands entirely — there's screen room and
    nothing is gained by collapsing four or five hits — while a large set keeps a
    fixed head so the first screen stays useful. Drives both the disclosure gate
    and the rail tiers, keeping them in lock-step by construction.
    """
    return n if n <= _EXPAND_ALL_MAX else _EXPAND_HEAD


def _search_width(fidelity: Fidelity) -> tuple[int | None, bool]:
    """Return (width, oneline) for search rendering.

    Default truncates each snippet to one line at the terminal width; --full
    (no char limit at depth >= 2) returns ``None`` — the natural-sizing escape
    that shows full, untruncated text for piping/review.
    """
    import sys

    full = not (fidelity.chars == 0 and fidelity.depth < 2)
    if full:
        return None, False
    if sys.stdout.isatty():
        return term_width(100), True
    return 100, True


def _match_spans(text: str) -> list:
    """Split text into spans — matched terms in accent, literals plain.

    Delegates the marker parse to the shared, painted-free splitter
    (``split_match_segments``) so the terminal and markdown paths agree on the
    delimiters; here the matched runs become accent (weight-only) spans. The
    splitter always yields at least one segment, so the result is never empty.
    """
    _, _, Span, Style, current_palette, _, _ = _painted()
    accent = current_palette().accent
    plain = Style()
    return [
        Span(seg, accent if is_match else plain)
        for seg, is_match in split_match_segments(text)
    ]


def _role_prefix(role: str, ds: DomainStyles, *, abbrev: bool) -> list[tuple[str, Style]]:
    """Bracketed role token: muted brackets framing the role word.

    The one role-label idiom shared by the detail (reading) and search (scan)
    surfaces — brackets recede to the separator role, the word carries the prompt
    weight. ``abbrev`` collapses ``assistant`` → ``asst`` for the dense search
    list; the detail path passes the full word. Returns ``(text, style)`` segments
    to splice into a row line.
    """
    return [
        ("[", ds.separator),
        (role_label(role, abbrev=abbrev), ds.prompt),
        ("] ", ds.separator),
    ]


def _wrap_spans(spans: list, width: int) -> list:
    """Word-wrap styled spans into Lines of <= width display columns.

    Thin delegator to the leaf ``output.row.wrap_spans`` (the one home for the
    wcwidth-correct styled word-wrap, shared with the markdown body and help
    renderers). Kept as a module-local name so this module's call sites read
    unchanged.
    """
    from siftd.output.row import wrap_spans

    return wrap_spans(spans, width)


def _snippet_block(
    text: str, width: int | None, *, oneline: bool, wrap: bool = False, ascii_mode: bool = False
) -> Block:
    """Render search text with matched terms highlighted.

    oneline: collapse to one truncated line. Otherwise render the snippet's
    natural lines. With ``wrap`` (and a known width) long lines word-wrap to the
    width so the full snippet shows untruncated; without it lines are
    width-truncated (``width=None`` = natural sizing, for --full / piping).
    ``ascii_mode`` degrades the ``…`` truncation marker to ``...`` so the truncated
    forms don't crash on a non-UTF-8 stream (painted hardcodes ``…`` otherwise).
    """
    _, Line, _, _, _, join_vertical, _ = _painted()
    from painted import truncate

    ellipsis = "..." if ascii_mode else "…"

    if oneline:
        flat = " ".join(text.split())
        line = Line(spans=tuple(_match_spans(flat)))
        block = _line_block(line)
        if width is not None and line.width > width:
            return truncate(block, width, ellipsis)
        return block

    rows = []
    for ln in text.splitlines() or [text]:
        spans = _match_spans(ln)
        if wrap and width is not None:
            rows.extend(_line_block(wl) for wl in _wrap_spans(spans, width))
        else:
            line = Line(spans=tuple(spans))
            block = _line_block(line)
            if width is not None and line.width > width:
                block = truncate(block, width, ellipsis)
            rows.append(block)
    return join_vertical(*rows) if rows else _blank_block()


def _relevance_rail(
    rank: int, height: int, expand: int, *, force_tail: bool = False
) -> Block:
    """Left rail encoding relevance rank: top hit promoted, tail dim.

    The glyph carries the rank even when color is stripped (NO_COLOR), matching
    doctor's severity glyphs. The glyph vocabulary is the ambient IconSet's rank
    ladder (``rank_top``/``rank_mid``/``rank_tail``), so it ASCII-degrades via the
    one icon lever set at ``main()`` rather than a threaded flag. ``expand`` is the
    disclosure-head size (the rail tier boundary, from ``_expand_count``);
    ``force_tail`` marks an always-collapsed row (the thread "more results" tier).
    """
    Block, _, _, Style, current_palette, _, _ = _painted()
    from painted import current_icons

    p = current_palette()
    ic = current_icons()
    if force_tail or rank >= expand:
        glyph, style = ic.rank_tail, p.muted
    elif rank == 0:
        glyph, style = ic.rank_top, p.accent
    else:
        glyph, style = ic.rank_mid, p.accent
    rows = [(f"{glyph} ", style)] + [("  ", Style())] * max(0, height - 1)
    return Block.column(rows)


def _railed(
    content: Block, rank: int, expand: int, *, force_tail: bool = False
) -> Block:
    """Prefix a content block with its relevance rail."""
    from painted import join_horizontal

    rail = _relevance_rail(rank, content.height, expand, force_tail=force_tail)
    return join_horizontal(rail, content)


def _meta_header(left_parts: list, score: float | None, inner: int | None) -> Block:
    """A record header: left metadata + a right-aligned quiet score."""
    _, _, _, Style, current_palette, _, _ = _painted()
    p = current_palette()
    line = _line(*left_parts)
    score_str = "" if score is None else f"{score:.2f}"
    if not score_str:
        if inner is None:
            return _line_block(line)
        return line.truncate(inner).to_block(inner)
    if inner is None:
        return _line_block(_line(*left_parts, ("  ", Style()), (score_str, p.muted)))
    pad = max(1, inner - line.width - len(score_str))
    return _line(*left_parts, (" " * pad, Style()), (score_str, p.muted)).to_block(inner)


def _minimal_chunk_line(r: dict, inner: int, ds: DomainStyles) -> Block:
    """Collapsed one-line form for tail hits: [role] id ws  snippet  score.

    Shares the role idiom (``_role_prefix``) and ``ds`` roles (identifier,
    workspace) with the expanded form; the prefix is built via the ``row_line``
    atom, and only the right-aligning pad/score stay hand-composed (a
    width-budget the atom doesn't model).
    """
    _, Line, Span, Style, current_palette, _, _ = _painted()
    p = current_palette()
    label = r.get("display_label", "")
    score = r.get("score")
    score_str = "" if score is None else f"{score:.2f}"

    prefix_parts: list[tuple[str, Style]] = [
        *_role_prefix(label, ds, abbrev=True),
        (f"{short_id(r.get('conversation_id', ''))}  ", ds.identifier),
    ]
    ws = r.get("_workspace", "")
    if ws:
        prefix_parts.append((f"{ws}  ", ds.workspace))
    prefix = _line(*prefix_parts)
    prefix_w = prefix.width

    snip = Line(spans=tuple(_match_spans(" ".join(r.get("text", "").split()))))
    avail = max(1, inner - prefix_w - len(score_str) - 1)
    if snip.width > avail:
        snip = snip.truncate(avail)
    pad = max(1, inner - prefix_w - snip.width - len(score_str))
    spans = prefix.spans + snip.spans + (Span(" " * pad, Style()), Span(score_str, p.muted))
    return Line(spans=spans).to_block(inner)


def render_search_block(
    results: list,
    fidelity: Fidelity,
    *,
    query: str = "",
    mode: str = "chunks",
    engine: str | None = None,
    tier1: list | None = None,
    tier2: list | None = None,
    caveats: list | None = None,
    **_ignore,
) -> Block:
    """Render search results as a painted Block (chunks / conversations / thread).

    ``mode`` is the view shape; ``engine`` is the resolved engine that ran
    ("fts"/"semantic"/"hybrid") — rendered as a muted ``[engine]`` tag after the
    query, mirroring the markdown/html surfaces so a degraded search reads truthfully
    on the terminal too.

    Matched terms render as accent spans; a left rail encodes relevance rank. Like
    the detail/peek paths, the meta line reads off DomainStyles (ids terracotta,
    workspace/timestamps their roles, the role label via the shared _role_prefix)
    rather than raw palette slots, and degrades its rail/ellipsis glyphs on a
    non-Unicode stream. Returns a Block consumed by emit_output like render_detail.
    """
    from painted import current_palette, join_vertical

    from siftd.output.theme import domain_styles

    p = current_palette()
    ds = domain_styles(fidelity)
    ascii_mode = prefers_ascii()
    caveats = caveats or []
    width, oneline = _search_width(fidelity)
    inner = None if width is None else max(width - 2, 1)

    out: list[Block] = []
    title_label = "Conversations for: " if mode == "conversations" else "Results for: "
    title_segs = [(title_label, ds.label), (query, p.accent)]
    if engine:
        title_segs.append((f" [{engine}]", ds.summary))
    out.append(_line_block(_line(*title_segs)))
    out.append(_blank_block())

    if mode == "conversations":
        expand = _expand_count(len(results))
        for rank, r in enumerate(results):
            conv_id = r.get("conversation_id", "")
            left = [
                (f"{short_id(conv_id)}  ", ds.identifier),
                (f"{r.get('_started_at', '')}  ", ds.temporal),
                (r.get("_workspace", ""), ds.workspace),
                (f"  ({r.get('chunk_count', 0)} chunks)", ds.summary),
            ]
            header = _meta_header(left, r.get("max_score"), inner)
            excerpt = _snippet_block(r.get("best_excerpt", ""), inner, oneline=oneline, ascii_mode=ascii_mode)
            out.append(_railed(join_vertical(header, excerpt), rank, expand))
            out.append(_blank_block())

    elif mode == "thread":
        thread_sep = "──" if not ascii_mode else "--"
        for r in tier1 or []:
            ws = r.get("_workspace", "")
            started = r.get("_started_at", "")
            out.append(_line_block(_line((f"{thread_sep} {ws}  {started} ", ds.prompt))))
            exchanges = r.get("_exchanges")
            if exchanges:
                for _pid, ptext, rtext in exchanges:
                    if ptext:
                        out.append(_snippet_block(
                            f"  [{role_label('user', abbrev=True)}] {ptext}",
                            inner, oneline=oneline, ascii_mode=ascii_mode,
                        ))
                    if rtext:
                        out.append(_snippet_block(
                            f"  [{role_label('assistant', abbrev=True)}] {rtext}",
                            inner, oneline=oneline, ascii_mode=ascii_mode,
                        ))
            else:
                label = r.get("display_label", "")
                out.append(_snippet_block(
                    f"  [{role_label(label, abbrev=True)}] {r.get('text', '')}",
                    inner, oneline=oneline, ascii_mode=ascii_mode,
                ))
            file_refs = r.get("file_refs")
            if file_refs:
                out.append(_line_block(_line((f"  {format_refs_annotation(file_refs)}", ds.separator))))
            out.append(_blank_block())
        if tier2:
            out.append(_line_block(_line(("More results:", ds.label))))
            out.append(_blank_block())
            expand2 = _expand_count(len(tier2))
            for rank, r in enumerate(tier2):
                conv_id = r.get("conversation_id", "")
                left = [
                    (f"{short_id(conv_id)}  ", ds.identifier),
                    (f"{r.get('_workspace', '')}  ", ds.workspace),
                    (r.get("_started_at", ""), ds.temporal),
                ]
                header = _meta_header(left, r.get("score"), inner)
                snippet = _snippet_block(r.get("text", ""), inner, oneline=True, ascii_mode=ascii_mode)
                out.append(_railed(
                    join_vertical(header, snippet), rank, expand2, force_tail=True,
                ))
                out.append(_blank_block())

    else:  # chunks (default, --around context, exchanges)
        expand = _expand_count(len(results))
        match_caret = "▸ " if not ascii_mode else "* "
        for rank, r in enumerate(results):
            exchanges = r.get("_exchanges")
            context_data = r.get("_context")
            # Disclosure gradient: the head expands (multi-line snippet); the tail
            # collapses to one line. --around results (exchanges/context) and
            # --full (inner is None) always expand.
            if inner is not None and rank >= expand and not exchanges and not context_data:
                out.append(_railed(_minimal_chunk_line(r, inner, ds), rank, expand))
                out.append(_blank_block())
                continue

            conv_id = r.get("conversation_id", "")
            label = r.get("display_label", "")
            left = [
                *_role_prefix(label, ds, abbrev=True),
                (f"{r.get('_started_at', '')}  ", ds.temporal),
                (r.get("_workspace", ""), ds.workspace),
            ]
            _tags = r.get("tags")
            if _tags:
                left.append((f"  {' '.join('#' + t for t in _tags)}", ds.tag))
            rows = [_meta_header(left, r.get("score"), inner)]

            if exchanges:
                for _pid, ptext, rtext in exchanges:
                    if ptext:
                        rows.append(_snippet_block(f"> {ptext}", inner, oneline=oneline, ascii_mode=ascii_mode))
                    if rtext:
                        rows.append(_snippet_block(rtext, inner, oneline=oneline, ascii_mode=ascii_mode))
            elif context_data:
                for _pid, ptext, rtext, is_match in context_data:
                    prefix = match_caret if is_match else "  "
                    if ptext:
                        rows.append(_snippet_block(f"{prefix}> {ptext}", inner, oneline=oneline, ascii_mode=ascii_mode))
                    if rtext:
                        rows.append(_snippet_block(f"{prefix}{rtext}", inner, oneline=oneline, ascii_mode=ascii_mode))
            else:
                # Top-tier hits show the full snippet, word-wrapped (untruncated).
                rows.append(_snippet_block(r.get("text", ""), inner, oneline=False, wrap=True, ascii_mode=ascii_mode))

            file_refs = r.get("file_refs")
            if file_refs:
                rows.append(_line_block(_line((format_refs_annotation(file_refs), ds.separator))))

            turn_index = r.get("turn_index")
            hint = f"> siftd show {short_id(conv_id)}"
            if turn_index is not None:
                hint += f" --at-turn {turn_index}"
            rows.append(_line_block(_line((hint, ds.summary))))

            out.append(_railed(join_vertical(*rows), rank, expand))
            out.append(_blank_block())

    for c in caveats:
        out.append(_line_block(_line((f"note: {c.message}", ds.separator))))

    return join_vertical(*out) if out else _blank_block()
