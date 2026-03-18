"""Bridge normalized narrative data onto painted rendering primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from siftd.output.common import fmt_timestamp, fmt_tokens, fmt_workspace, truncate_text
from siftd.output.zoom import NarrativeZoom

if TYPE_CHECKING:
    from painted import Block, Line, Style


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


def _append_multiline(
    lines: list[Line],
    prefix: str,
    prefix_style: Style,
    text: str,
    text_style: Style,
    limit: int,
) -> None:
    rendered = truncate_text(text, limit)
    split = rendered.splitlines() or [rendered]
    if not split:
        return

    lines.append(_line((prefix, prefix_style), (split[0], text_style)))
    continuation = " " * len(prefix)
    for part in split[1:]:
        lines.append(_line((continuation, prefix_style), (part, text_style)))


def render_narrative_lines(
    blocks: list,
    *,
    chars_limit: int,
    tool_chars: int,
    zoom: NarrativeZoom,
    show_tool_content: bool = False,
) -> list[Line]:
    """Render narrative blocks into styled painted lines.

    Plain output is intentionally text-identical to the current string renderer.
    """
    styles = _styles()
    lines: list[Line] = []

    for block in blocks:
        block_type = getattr(block, "block_type", "")
        content = getattr(block, "content", None) or ""

        if block_type == "text":
            if content:
                _append_multiline(lines, "  ", styles.assistant, content, styles.assistant, chars_limit)
        elif block_type == "thinking":
            if content:
                _append_multiline(lines, "  [thinking] ", styles.thinking, content, styles.thinking, chars_limit)
        elif block_type in ("tool_result", "tool_output"):
            if content and show_tool_content:
                _append_multiline(
                    lines,
                    f"  [{block_type}] ",
                    styles.meta,
                    content,
                    styles.tool_result,
                    tool_chars,
                )
        elif block_type == "tool_calls":
            for tc in getattr(block, "tool_calls", []):
                name = getattr(tc, "tool_name", "unknown")
                count = getattr(tc, "count", 1)
                status = getattr(tc, "status", None)

                header_parts: list[tuple[str, Style]] = [
                    ("    → ", styles.meta),
                    (name, styles.tool),
                ]
                if count > 1:
                    header_parts.append((f" ×{count}", styles.meta))
                if status and status != "success":
                    status_style = styles.tool_error if status == "error" else styles.meta
                    header_parts.append((f" ({status})", status_style))
                lines.append(_line(*header_parts))

                if not show_tool_content:
                    continue

                input_text = getattr(tc, "input", None)
                if input_text:
                    _append_multiline(
                        lines,
                        "      input: ",
                        styles.tool_input,
                        str(input_text),
                        styles.tool_input,
                        tool_chars,
                    )

                result_text = getattr(tc, "result", None)
                if result_text:
                    result_style = styles.tool_error if status == "error" else styles.tool_result
                    _append_multiline(
                        lines,
                        "      ← ",
                        styles.meta,
                        str(result_text),
                        result_style,
                        tool_chars,
                    )

    return lines


def _tool_summary_lines(tool_summaries: list) -> list[Line]:
    styles = _styles()
    lines: list[Line] = []
    for tc in tool_summaries:
        parts: list[tuple[str, Style]] = [
            ("    → ", styles.meta),
            (tc.tool_name, styles.tool),
        ]
        if tc.count > 1:
            parts.append((f" ×{tc.count}", styles.meta))
        parts.append((f" ({tc.status})", styles.tool_error if tc.status == "error" else styles.meta))
        lines.append(_line(*parts))
    return lines


def _peek_tool_summary_lines(tool_summaries: list[tuple[str, int]]) -> list[Line]:
    styles = _styles()
    lines: list[Line] = []
    for tool_name, count in tool_summaries:
        parts: list[tuple[str, Style]] = [
            ("    → ", styles.meta),
            (tool_name, styles.tool),
        ]
        if count > 1:
            parts.append((f" ×{count}", styles.meta))
        lines.append(_line(*parts))
    return lines


def _follow_tool_summary_lines(tool_summaries: list[tuple[str, int, list[str]]]) -> list[Line]:
    styles = _styles()
    lines: list[Line] = []
    for tool_name, count, _hints in tool_summaries:
        parts: list[tuple[str, Style]] = [
            ("    → ", styles.meta),
            (tool_name, styles.tool),
        ]
        if count > 1:
            parts.append((f" ×{count}", styles.meta))
        lines.append(_line(*parts))
    return lines


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
    chars_limit: int,
    tool_chars: int,
    zoom: NarrativeZoom,
    show_tool_content: bool = False,
) -> Block:
    """Render a conversation detail view as a painted block."""
    styles = _styles()
    lines: list[Line] = []

    ws_name = fmt_workspace(detail.workspace_path)
    started = fmt_timestamp(detail.started_at)
    total_tokens = detail.total_input_tokens + detail.total_output_tokens

    lines.append(_line(("Conversation: ", styles.heading), (detail.id, styles.assistant)))
    if ws_name:
        lines.append(_line(("Workspace: ", styles.meta), (ws_name, styles.assistant)))
    lines.append(_line(("Started: ", styles.meta), (started, styles.assistant)))
    lines.append(_line(("Model: ", styles.meta), (detail.model or "unknown", styles.assistant)))
    lines.append(
        _line(
            ("Tokens: ", styles.meta),
            (fmt_tokens(total_tokens), styles.assistant),
            (
                f" (input: {fmt_tokens(detail.total_input_tokens)} / output: {fmt_tokens(detail.total_output_tokens)})",
                styles.meta,
            ),
        )
    )
    if detail.tags:
        lines.append(_line(("Tags: ", styles.meta), (", ".join(detail.tags), styles.assistant)))

    lines.append(_line())

    for turn in turns:
        ts = fmt_timestamp(turn.timestamp, time_only=True)

        if turn.prompt_text:
            lines.append(_line(("[prompt] ", styles.prompt), (ts, styles.meta)))
            _append_multiline(lines, "  ", styles.assistant, turn.prompt_text, styles.assistant, chars_limit)
            lines.append(_line())

        tool_summaries = turn.tool_call_summaries
        has_response = bool(turn.narrative) or turn.total_input_tokens or turn.total_output_tokens or tool_summaries
        if not has_response:
            continue

        tok = turn.total_input_tokens + turn.total_output_tokens
        lines.append(
            _line(
                ("[response] ", styles.prompt),
                (ts, styles.meta),
                (f" ({fmt_tokens(tok)} tok)", styles.meta),
            )
        )
        lines.extend(
            render_narrative_lines(
                turn.narrative,
                chars_limit=chars_limit,
                tool_chars=tool_chars,
                zoom=zoom,
                show_tool_content=show_tool_content,
            )
        )
        if not turn.narrative and tool_summaries:
            lines.extend(_tool_summary_lines(tool_summaries))
        lines.append(_line())

    return _lines_to_block(lines)


def render_peek_detail_block(
    detail,
    *,
    exchanges: list,
    chars_limit: int,
    tool_chars: int,
    zoom: NarrativeZoom,
    show_tool_content: bool = False,
) -> Block:
    """Render a peek session detail view as a painted block."""
    styles = _styles()
    lines: list[Line] = []

    info = detail.info
    ws_name = _peek_workspace(info)
    started = fmt_timestamp(detail.started_at)
    last_activity = _fmt_last_activity(getattr(info, "last_activity", None))
    shown_exchanges = len(exchanges)
    total_exchanges = getattr(info, "exchange_count", 0) or shown_exchanges
    exchanges_text = str(total_exchanges)
    if shown_exchanges and total_exchanges > shown_exchanges:
        exchanges_text = f"{shown_exchanges} shown / {total_exchanges} total"

    lines.append(_line(("Session: ", styles.heading), (info.session_id, styles.assistant)))
    if ws_name:
        lines.append(_line(("Workspace: ", styles.meta), (ws_name, styles.assistant)))
    if started:
        lines.append(_line(("Started: ", styles.meta), (started, styles.assistant)))
    if last_activity:
        lines.append(_line(("Last activity: ", styles.meta), (last_activity, styles.assistant)))
    lines.append(_line(("Model: ", styles.meta), (info.model or "unknown", styles.assistant)))
    lines.append(_line(("Adapter: ", styles.meta), ((info.adapter_name or "unknown"), styles.assistant)))
    lines.append(_line(("Exchanges: ", styles.meta), (exchanges_text, styles.assistant)))
    if getattr(info, "parent_session_id", None):
        lines.append(_line(("Parent: ", styles.meta), (info.parent_session_id, styles.assistant)))
    lines.append(_line(("File: ", styles.meta), (str(info.file_path), styles.assistant)))
    lines.append(_line())

    for exchange in exchanges:
        ts = fmt_timestamp(exchange.timestamp, time_only=True)

        if exchange.prompt_text:
            lines.append(_line(("[prompt] ", styles.prompt), (ts, styles.meta)))
            _append_multiline(lines, "  ", styles.assistant, exchange.prompt_text, styles.assistant, chars_limit)
            lines.append(_line())

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
        lines.append(
            _line(
                ("[response] ", styles.prompt),
                (ts, styles.meta),
                (f" ({fmt_tokens(total_tokens)} tok)", styles.meta),
            )
        )
        if exchange.narrative:
            lines.extend(
                render_narrative_lines(
                    exchange.narrative,
                    chars_limit=chars_limit,
                    tool_chars=tool_chars,
                    zoom=zoom,
                    show_tool_content=show_tool_content,
                )
            )
        elif exchange.response_text:
            _append_multiline(lines, "  ", styles.assistant, exchange.response_text, styles.assistant, chars_limit)

        if not exchange.narrative and exchange.tool_calls:
            lines.extend(_peek_tool_summary_lines(exchange.tool_calls))
        lines.append(_line())

    return _lines_to_block(lines)


def render_follow_event_block(
    event,
    *,
    chars_limit: int,
    tool_chars: int,
    zoom: NarrativeZoom,
    show_tool_content: bool = False,
) -> Block:
    """Render a single follow-mode event as a painted block."""
    styles = _styles()
    lines: list[Line] = []
    ts = fmt_timestamp(getattr(event, "timestamp", None), time_only=True)

    if getattr(event, "is_user", False):
        lines.append(_line(("[prompt] ", styles.prompt), (ts, styles.meta)))
        text = getattr(event, "text", None)
        if text:
            _append_multiline(lines, "  ", styles.assistant, text, styles.assistant, chars_limit)
        return _lines_to_block(lines)

    total_tokens = getattr(event, "input_tokens", 0) + getattr(event, "output_tokens", 0)
    header_parts: list[tuple[str, Style]] = [
        ("[response] ", styles.prompt),
        (ts, styles.meta),
    ]
    if total_tokens:
        header_parts.append((f" ({fmt_tokens(total_tokens)} tok)", styles.meta))
    lines.append(_line(*header_parts))

    narrative = getattr(event, "narrative", [])
    if narrative:
        lines.extend(
            render_narrative_lines(
                narrative,
                chars_limit=chars_limit,
                tool_chars=tool_chars,
                zoom=zoom,
                show_tool_content=show_tool_content,
            )
        )
    else:
        text = getattr(event, "text", None)
        if text:
            _append_multiline(lines, "  ", styles.assistant, text, styles.assistant, chars_limit)
        tool_calls = getattr(event, "tool_calls", [])
        if tool_calls:
            lines.extend(_follow_tool_summary_lines(tool_calls))

    return _lines_to_block(lines)
