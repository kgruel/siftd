"""Bridge normalized narrative data onto painted rendering primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from siftd.output.common import fmt_timestamp, fmt_tokens, fmt_workspace, truncate_text

if TYPE_CHECKING:
    from painted import Block, Fidelity, Line, Style


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


# ---------------------------------------------------------------------------
# Tool-specific presenters
# ---------------------------------------------------------------------------

_TOOL_INDENT = "      "  # 6-space indent, consistent with current convention
_MAX_PREVIEW_LINES = 6


def _parse_json_safe(raw: str | None) -> dict | None:
    """Parse raw JSON string to dict, returning None on failure."""
    if not raw:
        return None
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _output_preview_lines(
    output: str,
    *,
    result_style: Style,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render output text as a line-limited preview with overflow indicator."""
    lines: list[Line] = []
    raw_lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    if not raw_lines:
        return lines
    max_lines = 0 if tool_chars == 0 else _MAX_PREVIEW_LINES
    preview = raw_lines if max_lines == 0 else raw_lines[:max_lines]
    for out_line in preview:
        _append_multiline(lines, _TOOL_INDENT, styles.meta, out_line, result_style, tool_chars)
    if max_lines > 0 and len(raw_lines) > max_lines:
        overflow = len(raw_lines) - max_lines
        lines.append(_line((_TOOL_INDENT + f"... +{overflow} more lines", styles.summary_hint)))
    return lines


def _render_shell_execute_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render shell.execute (Bash) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)
    res = _parse_json_safe(raw_result)

    command = ""
    if isinstance(inp, dict):
        command = inp.get("command") or inp.get("cmd", "")
    elif raw_input:
        command = str(raw_input)
    if command:
        lines.append(_line((_TOOL_INDENT + "$ ", styles.meta), (command, styles.tool)))

    if isinstance(res, dict):
        meta_parts: list[str] = []
        exit_code = res.get("exit_code")
        if exit_code is not None:
            meta_parts.append(f"exit: {exit_code}")
        wall = res.get("wall_time_seconds") or res.get("wall_time")
        if wall is not None:
            meta_parts.append(f"wall: {wall}s")
        if meta_parts:
            meta_style = styles.tool_error if status == "error" else styles.meta
            lines.append(_line((_TOOL_INDENT, meta_style), (" · ".join(meta_parts), meta_style)))

        output = res.get("output", "")
        if isinstance(output, str) and output.strip():
            result_style = styles.tool_error if status == "error" else styles.tool_result
            lines.extend(
                _output_preview_lines(output, result_style=result_style, styles=styles, tool_chars=tool_chars)
            )
    elif raw_result:
        result_style = styles.tool_error if status == "error" else styles.tool_result
        _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, str(raw_result), result_style, tool_chars)

    return lines


def _render_file_read_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render file.read (Read) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)
    res = _parse_json_safe(raw_result)

    path_str = ""
    if isinstance(inp, dict):
        path = inp.get("file_path") or inp.get("path", "")
        if path:
            path_str = str(path)
            offset = inp.get("offset")
            limit = inp.get("limit")
            if offset is not None and limit is not None:
                path_str += f":{offset}-{offset + limit - 1}"
            elif offset is not None:
                path_str += f":{offset}"
    elif raw_input:
        path_str = str(raw_input)

    if path_str:
        suffix_parts: list[str] = []
        if isinstance(res, dict):
            tokens = res.get("original_token_count")
            if tokens:
                suffix_parts.append(f"{tokens} tokens")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        lines.append(_line((_TOOL_INDENT, styles.meta), (path_str, styles.tool), (suffix, styles.meta)))

    if status == "error" and isinstance(res, dict):
        error_text = res.get("error") or res.get("message") or res.get("output", "")
        if error_text:
            _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, str(error_text), styles.tool_error, tool_chars)

    return lines


def _render_file_edit_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render file.edit (Edit) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)
    res = _parse_json_safe(raw_result)

    if isinstance(inp, dict):
        path = inp.get("file_path") or inp.get("path", "")
        if path:
            lines.append(_line((_TOOL_INDENT, styles.meta), (str(path), styles.tool)))

        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        if old:
            _append_multiline(lines, _TOOL_INDENT + "- ", styles.tool_input, str(old), styles.tool_input, tool_chars)
        if new:
            _append_multiline(lines, _TOOL_INDENT + "+ ", styles.meta, str(new), styles.tool_result, tool_chars)
    elif raw_input:
        lines.append(_line((_TOOL_INDENT, styles.meta), (str(raw_input), styles.tool)))

    if status == "error" and isinstance(res, dict):
        error_text = res.get("error") or res.get("message") or res.get("text", "")
        if error_text:
            _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, str(error_text), styles.tool_error, tool_chars)

    return lines


def _render_file_write_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render file.write (Write) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)
    res = _parse_json_safe(raw_result)

    if isinstance(inp, dict):
        path = inp.get("file_path") or inp.get("path", "")
        if path:
            content = inp.get("content", "")
            line_count = len(content.splitlines()) if isinstance(content, str) and content else 0
            suffix = f" ({line_count} lines)" if line_count else ""
            lines.append(_line((_TOOL_INDENT, styles.meta), (str(path), styles.tool), (suffix, styles.meta)))
    elif raw_input:
        lines.append(_line((_TOOL_INDENT, styles.meta), (str(raw_input), styles.tool)))

    if status == "error" and isinstance(res, dict):
        error_text = res.get("error") or res.get("message") or res.get("text", "")
        if error_text:
            _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, str(error_text), styles.tool_error, tool_chars)

    return lines


def _render_search_grep_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render search.grep (Grep) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)
    res = _parse_json_safe(raw_result)

    if isinstance(inp, dict):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        include = inp.get("include") or inp.get("glob", "")
        parts: list[tuple[str, Style]] = [(_TOOL_INDENT, styles.meta)]
        if pattern:
            parts.append((f"/{pattern}/", styles.tool))
        if path:
            parts.append((f" in {path}", styles.tool_input))
        if include:
            parts.append((f" {include}", styles.tool_input))
        if len(parts) > 1:
            lines.append(_line(*parts))
    elif raw_input:
        _append_multiline(lines, _TOOL_INDENT + "input: ", styles.tool_input, str(raw_input), styles.tool_input, tool_chars)

    if isinstance(res, dict):
        output = res.get("output", "")
        if isinstance(output, str) and output.strip():
            result_style = styles.tool_error if status == "error" else styles.tool_result
            lines.extend(
                _output_preview_lines(output, result_style=result_style, styles=styles, tool_chars=tool_chars)
            )
    elif raw_result:
        result_style = styles.tool_error if status == "error" else styles.tool_result
        _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, str(raw_result), result_style, tool_chars)

    return lines


def _render_file_glob_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render file.glob (Glob) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)
    res = _parse_json_safe(raw_result)

    if isinstance(inp, dict):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        parts: list[tuple[str, Style]] = [(_TOOL_INDENT, styles.meta)]
        if pattern:
            parts.append((pattern, styles.tool))
        if path:
            parts.append((f" in {path}", styles.tool_input))
        if len(parts) > 1:
            lines.append(_line(*parts))
    elif raw_input:
        _append_multiline(lines, _TOOL_INDENT + "input: ", styles.tool_input, str(raw_input), styles.tool_input, tool_chars)

    if isinstance(res, dict):
        output = res.get("output", "")
        if isinstance(output, str) and output.strip():
            lines.extend(
                _output_preview_lines(output, result_style=styles.tool_result, styles=styles, tool_chars=tool_chars)
            )
    elif raw_result:
        _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, str(raw_result), styles.tool_result, tool_chars)

    return lines


def _render_todo_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render ui.todo (TodoWrite) tool call content."""
    lines: list[Line] = []
    inp = _parse_json_safe(raw_input)

    if isinstance(inp, dict):
        title = inp.get("title", "")
        if title:
            lines.append(_line((_TOOL_INDENT, styles.meta), (str(title), styles.tool)))
        tasks = inp.get("tasks") or inp.get("plan") or []
        if isinstance(tasks, list):
            for item in tasks:
                if isinstance(item, dict):
                    step = item.get("description") or item.get("step") or item.get("content", "")
                    item_status = item.get("status", "")
                    check = "✓" if item_status in ("done", "completed") else "○"
                    if step:
                        lines.append(
                            _line((_TOOL_INDENT + f"  {check} ", styles.meta), (str(step), styles.tool_input))
                        )
                elif isinstance(item, str):
                    lines.append(_line((_TOOL_INDENT + "  ○ ", styles.meta), (item, styles.tool_input)))
    elif raw_input:
        _append_multiline(lines, _TOOL_INDENT + "input: ", styles.tool_input, str(raw_input), styles.tool_input, tool_chars)

    return lines


def _format_generic_input(raw: str) -> str:
    """Format tool input JSON into a compact, readable summary."""
    obj = _parse_json_safe(raw)
    if isinstance(obj, dict):
        priority_keys = (
            "description", "command", "cmd", "file_path", "path",
            "pattern", "query", "url", "title",
        )
        parts: list[str] = []
        for key in priority_keys:
            value = obj.get(key)
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}: {value}")
        if parts:
            return " · ".join(parts)
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return raw


def _format_generic_result(raw: str) -> str:
    """Format tool result JSON into a readable summary."""
    obj = _parse_json_safe(raw)
    if isinstance(obj, dict):
        output = obj.get("output")
        if isinstance(output, str) and output.strip():
            meta_parts: list[str] = []
            for mkey, label in (
                ("exit_code", "exit"),
                ("wall_time_seconds", "wall"),
                ("wall_time", "wall"),
                ("duration", "duration"),
            ):
                mvalue = obj.get(mkey)
                if mvalue not in (None, "", [], {}):
                    meta_parts.append(f"{label}: {mvalue}")
            prefix = " · ".join(meta_parts) + "\n" if meta_parts else ""
            return prefix + output

        for key in ("text", "result", "message"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value

        compact: list[str] = []
        for key in ("output", "result", "message", "error", "status"):
            value = obj.get(key)
            if value not in (None, "", [], {}):
                compact.append(f"{key}: {value}")
        if compact:
            return " · ".join(compact)
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return raw


def _render_generic_lines(
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Render any tool call with generic input/result formatting."""
    lines: list[Line] = []

    if raw_input:
        formatted = _format_generic_input(raw_input)
        if formatted:
            _append_multiline(lines, _TOOL_INDENT + "input: ", styles.tool_input, formatted, styles.tool_input, tool_chars)

    if raw_result:
        formatted = _format_generic_result(raw_result)
        if formatted:
            result_style = styles.tool_error if status == "error" else styles.tool_result
            _append_multiline(lines, _TOOL_INDENT + "← ", styles.meta, formatted, result_style, tool_chars)

    return lines


_TOOL_PRESENTERS = {
    "shell.execute": _render_shell_execute_lines,
    "file.read": _render_file_read_lines,
    "file.edit": _render_file_edit_lines,
    "file.write": _render_file_write_lines,
    "search.grep": _render_search_grep_lines,
    "file.glob": _render_file_glob_lines,
    "ui.todo": _render_todo_lines,
}


def _render_tool_content_lines(
    name: str,
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    styles: _RoleStyles,
    tool_chars: int,
) -> list[Line]:
    """Dispatch to tool-specific or generic presenter."""
    renderer = _TOOL_PRESENTERS.get(name, _render_generic_lines)
    return renderer(raw_input, raw_result, status, styles, tool_chars)


_DEFAULT_TOOL_CHARS = 120


def _tool_density(fidelity: Fidelity) -> int:
    """Derive tool content char limit from fidelity."""
    if fidelity.depth >= 3:
        return 0  # full depth = no truncation
    if fidelity.chars > 0:
        return fidelity.chars  # match text density
    return _DEFAULT_TOOL_CHARS


def render_narrative_lines(
    blocks: list,
    *,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> list[Line]:
    """Render narrative blocks into styled painted lines.

    Args:
        blocks: Narrative blocks to render.
        fidelity: Three-axis rendering spec (depth, visibility, density).
        tool_chars: Optional tool density override (0=derive from fidelity).
    """
    styles = _styles()
    lines: list[Line] = []
    chars_limit = fidelity.chars
    effective_tool_chars = tool_chars or _tool_density(fidelity)
    show_tool_content = fidelity.shows("tools")

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
                    effective_tool_chars,
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

                lines.extend(
                    _render_tool_content_lines(
                        name,
                        getattr(tc, "input", None),
                        getattr(tc, "result", None),
                        status,
                        styles,
                        effective_tool_chars,
                    )
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
    fidelity: Fidelity,
    tool_chars: int = 0,
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
            _append_multiline(lines, "  ", styles.assistant, turn.prompt_text, styles.assistant, fidelity.chars)
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
                fidelity=fidelity,
                tool_chars=tool_chars,
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
    fidelity: Fidelity,
    tool_chars: int = 0,
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
            _append_multiline(lines, "  ", styles.assistant, exchange.prompt_text, styles.assistant, fidelity.chars)
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
                    fidelity=fidelity,
                    tool_chars=tool_chars,
                )
            )
        elif exchange.response_text:
            _append_multiline(lines, "  ", styles.assistant, exchange.response_text, styles.assistant, fidelity.chars)

        if not exchange.narrative and exchange.tool_calls:
            lines.extend(_peek_tool_summary_lines(exchange.tool_calls))
        lines.append(_line())

    return _lines_to_block(lines)


def render_follow_event_block(
    event,
    *,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render a single follow-mode event as a painted block."""
    styles = _styles()
    lines: list[Line] = []
    ts = fmt_timestamp(getattr(event, "timestamp", None), time_only=True)

    if getattr(event, "is_user", False):
        lines.append(_line(("[prompt] ", styles.prompt), (ts, styles.meta)))
        text = getattr(event, "text", None)
        if text:
            _append_multiline(lines, "  ", styles.assistant, text, styles.assistant, fidelity.chars)
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
                fidelity=fidelity,
                tool_chars=tool_chars,
            )
        )
    else:
        text = getattr(event, "text", None)
        if text:
            _append_multiline(lines, "  ", styles.assistant, text, styles.assistant, fidelity.chars)
        tool_calls = getattr(event, "tool_calls", [])
        if tool_calls:
            lines.extend(_follow_tool_summary_lines(tool_calls))

    return _lines_to_block(lines)
