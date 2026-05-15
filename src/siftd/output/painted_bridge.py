"""Bridge normalized narrative data onto painted rendering primitives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from siftd.domain.search_types import ROLE_ASSISTANT, ROLE_USER
from siftd.output._id_format import short_id
from siftd.output.common import fmt_timestamp, fmt_tokens, fmt_workspace, truncate_text

if TYPE_CHECKING:
    from painted import Align, Block, Fidelity, Line, Style

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


def _styles() -> _RoleStyles:
    _, _, _, Style, current_palette, _, _ = _painted()
    palette = current_palette()
    return _RoleStyles(
        heading=palette.accent.merge(Style(bold=True)),
        meta=palette.muted,
        prompt=palette.accent.merge(Style(bold=True)),
        assistant=Style(),
        thinking=palette.muted.merge(Style(italic=True)),
        tool=palette.accent,
        tool_input=palette.muted,
        tool_result=Style(),
        tool_error=palette.error,
        summary_hint=palette.muted,
    )


def _line(*parts: tuple[str, Style]) -> Line:
    _, Line, Span, _, _, _, _ = _painted()
    spans = tuple(Span(text, style) for text, style in parts if text)
    return Line(spans=spans)


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
    """Print a painted block with auto-detected ANSI/plain behavior."""
    _, _, _, _, _, _, painted_print_block = _painted()
    painted_print_block(block)


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


# ---------------------------------------------------------------------------
# Tool content → painted lines (via format-neutral ToolPresentation)
# ---------------------------------------------------------------------------

_TOOL_INDENT = "      "  # 6-space indent, consistent with current convention


def _presentation_to_lines(
    pres: ToolPresentation,
    styles: _RoleStyles,
) -> list[Line]:
    """Convert a format-neutral ToolPresentation into painted Lines."""
    lines: list[Line] = []

    # Headline + optional meta suffix
    if pres.headline:
        parts: list[tuple[str, Style]] = [(_TOOL_INDENT, styles.meta), (pres.headline, styles.tool)]
        if pres.meta:
            parts.append((f" ({pres.meta})", styles.meta))
        lines.append(_line(*parts))

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
        result_style = styles.tool_result
        for out_line in pres.output.splitlines():
            lines.append(_line((_TOOL_INDENT, styles.meta), (out_line, result_style)))
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
) -> list[Line]:
    """Extract tool presentation then render as painted lines."""
    from siftd.output.tool_presenters import extract_tool_presentation

    pres = extract_tool_presentation(name, raw_input, raw_result, status, tool_chars)
    return _presentation_to_lines(pres, styles)



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

    def __init__(self, ds: DomainStyles, tool_chars: int) -> None:
        from painted import border, pad

        self._border = border
        self._pad = pad
        self._ds = ds
        self._tool_chars = tool_chars
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

    def _flush_lines(self) -> None:
        if self._pending:
            self._parts.append(_lines_to_block(self._pending))
            self._pending = []

    # -- NarrativeEmitter interface --

    def text(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        # Walker already truncated; limit=0 avoids double truncation
        _append_multiline(self._pending, "  ", self._ds.assistant, content, self._ds.assistant, 0)

    def thinking(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self._flush_lines()
        think_lines: list[Line] = []
        _append_multiline(think_lines, "", self._ds.thinking, content, self._ds.thinking, 0)
        inner = _lines_to_block(think_lines)
        title_text = "thinking"
        min_inner_width = len(title_text) + 5
        if inner.width + 2 < min_inner_width:
            inner = self._pad(inner, right=min_inner_width - inner.width - 2)
        bordered = self._border(
            self._pad(inner, left=1, right=1),
            chars=self._ds.thinking_border,
            style=self._ds.separator,
            title=title_text,
            title_style=self._ds.thinking,
        )
        self._parts.append(self._pad(bordered, left=4))

    def thinking_placeholder(self, *, event_id: str | None = None) -> None:
        del event_id
        self._pending.append(_line(("    ", self._ds.separator), ("*[thinking]*", self._ds.thinking)))

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        self._pending.extend(_tool_summary_lines_styled(tools, self._ds))

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
        title = name
        if count > 1:
            title += f" ×{count}"
        if status and status != "success":
            title += f" ({status})"

        tool_lines = _render_tool_content_lines(
            name, raw_input, raw_result, status,
            self._role_styles, self._tool_chars,
        )
        if tool_lines:
            self._flush_lines()
            inner = _lines_to_block(tool_lines)
            title_style = self._ds.tool_error if status == "error" else self._ds.tool_name
            min_inner_width = len(title) + 5
            if inner.width + 2 < min_inner_width:
                inner = self._pad(inner, right=min_inner_width - inner.width - 2)
            bordered = self._border(
                self._pad(inner, left=1, right=1),
                chars=self._ds.tool_border,
                style=self._ds.separator,
                title=title,
                title_style=title_style,
            )
            self._parts.append(self._pad(bordered, left=4))

    def tool_output(self, block_type: str, content: str, *, event_id: str | None = None) -> None:
        del event_id
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
    from siftd.output.narrative import walk_narrative
    from siftd.output.theme import domain_styles

    ds = domain_styles(fidelity)
    effective_tool_chars = tool_chars or _tool_density(fidelity)
    emitter = PaintedEmitter(ds, effective_tool_chars)
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


def render_query_detail_block(
    detail,
    *,
    turns: list,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render a conversation detail view as a painted block."""
    from siftd.output.theme import domain_styles

    Block, _, _, _, _, join_vertical, _ = _painted()

    ds = domain_styles(fidelity)
    parts: list[Block] = []

    ws_name = fmt_workspace(detail.workspace_path)
    started = fmt_timestamp(detail.started_at)
    total_tokens = detail.total_input_tokens + detail.total_output_tokens

    header_lines: list[Line] = []
    header_lines.append(_line(("Conversation: ", ds.label), (detail.id, ds.identifier)))
    if ws_name:
        header_lines.append(_line(("Workspace: ", ds.temporal), (ws_name, ds.workspace)))
    header_lines.append(_line(("Started: ", ds.temporal), (started, ds.temporal)))
    header_lines.append(_line(("Model: ", ds.temporal), (detail.model or "unknown", ds.model)))
    header_lines.append(
        _line(
            ("Tokens: ", ds.temporal),
            (fmt_tokens(total_tokens), ds.metric),
            (
                f" (input: {fmt_tokens(detail.total_input_tokens)} / output: {fmt_tokens(detail.total_output_tokens)})",
                ds.metric,
            ),
        )
    )
    if detail.tags:
        header_lines.append(_line(("Tags: ", ds.temporal), (", ".join(detail.tags), ds.tag)))
    header_lines.append(_line())
    parts.append(_lines_to_block(header_lines))

    for turn in turns:
        ts = fmt_timestamp(turn.timestamp, time_only=True)
        turn_lines: list[Line] = []
        prompt_role_label = getattr(turn, "PROMPT_ROLE_LABEL", ROLE_USER)
        response_role_label = getattr(turn, "RESPONSE_ROLE_LABEL", ROLE_ASSISTANT)

        if turn.prompt_text:
            turn_lines.append(_line((f"[{prompt_role_label}] ", ds.prompt), (ts, ds.temporal)))
            _append_multiline(turn_lines, "  ", ds.assistant, turn.prompt_text, ds.assistant, fidelity.chars)
            turn_lines.append(_line())

        tool_summaries = turn.tool_call_summaries
        has_response = bool(turn.narrative) or turn.total_input_tokens or turn.total_output_tokens or tool_summaries
        if not has_response:
            if turn_lines:
                parts.append(_lines_to_block(turn_lines))
            continue

        tok = turn.total_input_tokens + turn.total_output_tokens
        turn_lines.append(
            _line(
                (f"[{response_role_label}] ", ds.prompt),
                (ts, ds.temporal),
                (f" ({fmt_tokens(tok)} tok)", ds.metric),
            )
        )

        if turn_lines:
            parts.append(_lines_to_block(turn_lines))

        if turn.narrative:
            parts.append(render_narrative_block(
                turn.narrative,
                fidelity=fidelity,
                tool_chars=tool_chars,
            ))
        elif tool_summaries:
            parts.append(_lines_to_block(_tool_summary_lines(
                [(tc.tool_name, tc.count, tc.status) for tc in tool_summaries]
            )))

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

    header_lines: list[Line] = []
    header_lines.append(_line(("Session: ", ds.label), (info.session_id, ds.identifier)))
    if ws_name:
        header_lines.append(_line(("Workspace: ", ds.temporal), (ws_name, ds.workspace)))
    if started:
        header_lines.append(_line(("Started: ", ds.temporal), (started, ds.temporal)))
    if last_activity:
        header_lines.append(_line(("Last activity: ", ds.temporal), (last_activity, ds.temporal)))
    header_lines.append(_line(("Model: ", ds.temporal), (info.model or "unknown", ds.model)))
    header_lines.append(_line(("Adapter: ", ds.temporal), ((info.adapter_name or "unknown"), ds.adapter)))
    header_lines.append(_line(("Exchanges: ", ds.temporal), (exchanges_text, ds.metric)))
    if getattr(info, "parent_session_id", None):
        header_lines.append(_line(("Parent: ", ds.temporal), (info.parent_session_id, ds.identifier)))
    header_lines.append(_line(("File: ", ds.temporal), (str(info.file_path), ds.workspace)))
    header_lines.append(_line())
    parts.append(_lines_to_block(header_lines))

    for exchange in exchanges:
        ts = fmt_timestamp(exchange.timestamp, time_only=True)
        ex_lines: list[Line] = []
        prompt_role_label = getattr(exchange, "PROMPT_ROLE_LABEL", ROLE_USER)
        response_role_label = getattr(exchange, "RESPONSE_ROLE_LABEL", ROLE_ASSISTANT)

        if exchange.prompt_text:
            ex_lines.append(_line((f"[{prompt_role_label}] ", ds.prompt), (ts, ds.temporal)))
            _append_multiline(ex_lines, "  ", ds.assistant, exchange.prompt_text, ds.assistant, fidelity.chars)
            ex_lines.append(_line())

        has_response = bool(
            exchange.narrative
            or exchange.response_text
            or exchange.tool_calls
            or exchange.input_tokens
            or exchange.output_tokens
        )
        if not has_response:
            if ex_lines:
                parts.append(_lines_to_block(ex_lines))
            continue

        total_tokens = exchange.input_tokens + exchange.output_tokens
        ex_lines.append(
            _line(
                (f"[{response_role_label}] ", ds.prompt),
                (ts, ds.temporal),
                (f" ({fmt_tokens(total_tokens)} tok)", ds.metric),
            )
        )

        if ex_lines:
            parts.append(_lines_to_block(ex_lines))

        if exchange.narrative:
            parts.append(render_narrative_block(
                exchange.narrative,
                fidelity=fidelity,
                tool_chars=tool_chars,
            ))
        elif exchange.response_text:
            resp_lines: list[Line] = []
            _append_multiline(resp_lines, "  ", ds.assistant, exchange.response_text, ds.assistant, fidelity.chars)
            if resp_lines:
                parts.append(_lines_to_block(resp_lines))

        if not exchange.narrative and exchange.tool_calls:
            parts.append(_lines_to_block(_tool_summary_lines(
                [(name, count, None) for name, count in exchange.tool_calls]
            )))

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

    if getattr(event, "is_user", False):
        lines: list[Line] = []
        lines.append(_line((f"[{ROLE_USER}] ", ds.prompt), (ts, ds.temporal)))
        text = getattr(event, "text", None)
        if text:
            _append_multiline(lines, "  ", ds.assistant, text, ds.assistant, fidelity.chars)
        return _lines_to_block(lines)

    total_tokens = getattr(event, "input_tokens", 0) + getattr(event, "output_tokens", 0)
    header_parts: list[tuple[str, Style]] = [
        (f"[{ROLE_ASSISTANT}] ", ds.prompt),
        (ts, ds.temporal),
    ]
    if total_tokens:
        header_parts.append((f" ({fmt_tokens(total_tokens)} tok)", ds.metric))

    parts: list[Block] = [_line_block(_line(*header_parts))]

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
            text_lines: list[Line] = []
            _append_multiline(text_lines, "  ", ds.assistant, text, ds.assistant, fidelity.chars)
            if text_lines:
                parts.append(_lines_to_block(text_lines))
        tool_calls = getattr(event, "tool_calls", [])
        if tool_calls:
            parts.append(_lines_to_block(_tool_summary_lines(
                [(name, count, None) for name, count, *_ in tool_calls]
            )))

    if len(parts) == 1:
        return parts[0]
    return join_vertical(*parts)


def _styled_table(
    col_defs: list[tuple[str, Callable, Style, Align]],
    items: list,
) -> Block:
    """Build a painted table from column definitions and data items.

    Each col_def is (header, cell_fn, cell_style, alignment).
    cell_fn(item) -> str for each row.

    Styling comes from the ambient Theme (palette + borders).
    Selection highlight is disabled (static table, not interactive).
    """
    from painted import Style as PStyle
    from painted.views import Column, TableState, table

    # Build cell text grid and compute column widths from content
    cell_texts: list[list[str]] = []
    for item in items:
        cell_texts.append([col_fn(item) for _, col_fn, _, _ in col_defs])

    widths = [len(header) for header, _, _, _ in col_defs]
    for row in cell_texts:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build painted Column definitions and styled rows
    columns: list[Column] = []
    for i, (header, _, _, align) in enumerate(col_defs):
        columns.append(Column(
            header=_line((header, PStyle())),
            width=widths[i],
            align=align,
        ))

    rows: list[list[Line]] = []
    for row_texts in cell_texts:
        rows.append([
            _line((text, col_def[2])) for text, col_def in zip(row_texts, col_defs)
        ])

    state = TableState().with_count(len(rows)).with_visible(len(rows))
    return table(state, columns, rows, visible_height=len(rows), selected_style=PStyle())


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

    from painted import current_palette

    p = current_palette()

    by_kind_rows: dict[str, int] = {}
    by_kind_query: list[str] = []
    for c in caveats:
        if c.target:
            by_kind_rows[c.check] = by_kind_rows.get(c.check, 0) + 1
        else:
            by_kind_query.append(c.message)

    lines: list[Line] = []
    for kind, count in sorted(by_kind_rows.items()):
        lines.append(_line(
            (f"{count} row(s) with {kind}", p.muted),
        ))
    for msg in by_kind_query:
        lines.append(_line((msg, p.muted)))

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

    from painted import Align, current_palette, join_vertical
    from painted import Style as PStyle

    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace

    p = current_palette()
    depth = fidelity.depth

    col_defs: list[tuple[str, Callable, PStyle, Align]] = [
        ("id", lambda c: short_id(c.id) if c.id else "", p.accent, Align.START),
        ("started_at", lambda c: fmt_timestamp(c.started_at), p.muted, Align.START),
        ("workspace", lambda c: fmt_workspace(c.workspace_path), PStyle(), Align.START),
    ]
    if depth >= 1:
        col_defs.extend([
            ("model", lambda c: fmt_model(c.model) if c.model else "", PStyle(), Align.START),
            ("turns", lambda c: str(c.prompt_count), p.muted, Align.END),
            ("tokens", lambda c: fmt_tokens(c.total_tokens), p.muted, Align.END),
        ])
    if depth >= 3:
        col_defs.extend([
            ("cost", lambda c: _fmt_cost(c), p.muted, Align.END),
            ("responses", lambda c: str(c.response_count), p.muted, Align.END),
            ("tags", lambda c: ", ".join(c.tags) if c.tags else "", p.accent, Align.START),
        ])

    table_block = _styled_table(col_defs, summaries)
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

    from painted import Align, current_palette
    from painted import Style as PStyle

    from siftd.output import fmt_ago, fmt_model

    p = current_palette()
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

    col_defs: list[tuple[str, Callable, PStyle, Align]] = [
        ("session", lambda s: short_id(s.session_id), p.accent, Align.START),
        ("workspace", _workspace, PStyle(), Align.START),
        ("activity", lambda s: fmt_ago(now - s.last_activity), p.muted, Align.START),
        ("exchanges", _exchanges, p.muted, Align.START),
        ("model", lambda s: fmt_model(s.model), PStyle(), Align.START),
        ("adapter", lambda s: s.adapter_name or "", p.muted, Align.START),
        ("agents", _suffix, p.accent, Align.START),
    ]

    return _styled_table(col_defs, sessions)
