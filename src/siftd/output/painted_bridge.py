"""Bridge normalized narrative data onto painted rendering primitives."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from siftd.output.common import fmt_timestamp, fmt_tokens, fmt_workspace, truncate_text

if TYPE_CHECKING:
    from painted import Align, Block, Fidelity, Line, Style


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


def render_narrative_block(
    blocks: list,
    *,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> Block:
    """Render narrative blocks into a composed painted Block.

    Text and tool headers render as styled lines. Tool content and thinking
    render as bordered sub-blocks using the domain theme's border chars.

    Args:
        blocks: Narrative blocks to render.
        fidelity: Three-axis rendering spec (depth, visibility, density).
        tool_chars: Optional tool density override (0=derive from fidelity).
    """
    from painted import border, pad

    from siftd.output.theme import domain_styles

    Block, _, _, _, _, join_vertical, _ = _painted()

    ds = domain_styles(fidelity)
    # Bridge: tool presenters still use _RoleStyles internally
    role_styles = _RoleStyles(
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
    parts: list[Block] = []
    chars_limit = fidelity.chars
    effective_tool_chars = tool_chars or _tool_density(fidelity)
    show_tool_content = fidelity.shows("tools")

    def _flush_lines(lines: list[Line]) -> None:
        if lines:
            parts.append(_lines_to_block(lines))
            lines.clear()

    pending: list[Line] = []

    for block in blocks:
        block_type = getattr(block, "block_type", "")
        content = getattr(block, "content", None) or ""

        if block_type == "text":
            if content:
                _append_multiline(pending, "  ", ds.assistant, content, ds.assistant, chars_limit)

        elif block_type == "thinking":
            if content:
                _flush_lines(pending)
                think_lines: list[Line] = []
                _append_multiline(think_lines, "", ds.thinking, content, ds.thinking, chars_limit)
                inner = _lines_to_block(think_lines)
                # Ensure minimum width for border title to render
                title_text = "thinking"
                min_inner_width = len(title_text) + 5  # title + 3 (border rule) + 2 (padding)
                if inner.width + 2 < min_inner_width:
                    inner = pad(inner, right=min_inner_width - inner.width - 2)
                bordered = border(
                    pad(inner, left=1, right=1),
                    chars=ds.thinking_border,
                    style=ds.separator,
                    title=title_text,
                    title_style=ds.thinking,
                )
                parts.append(pad(bordered, left=4))

        elif block_type in ("tool_result", "tool_output"):
            if content and show_tool_content:
                _append_multiline(
                    pending,
                    f"  [{block_type}] ",
                    ds.summary,
                    content,
                    ds.tool_result,
                    effective_tool_chars,
                )

        elif block_type == "tool_calls":
            for tc in getattr(block, "tool_calls", []):
                name = getattr(tc, "tool_name", "unknown")
                count = getattr(tc, "count", 1)
                status = getattr(tc, "status", None)

                # Build title suffix for count/status
                title = name
                if count > 1:
                    title += f" ×{count}"
                if status and status != "success":
                    title += f" ({status})"

                if not show_tool_content:
                    # Compact: arrow + name header
                    header_parts: list[tuple[str, Style]] = [
                        ("    → ", ds.separator),
                        (name, ds.tool_name),
                    ]
                    if count > 1:
                        header_parts.append((f" ×{count}", ds.separator))
                    if status and status != "success":
                        status_style = ds.tool_error if status == "error" else ds.separator
                        header_parts.append((f" ({status})", status_style))
                    pending.append(_line(*header_parts))
                    continue

                # Expanded: bordered block with tool name as title
                tool_lines = _render_tool_content_lines(
                    name,
                    getattr(tc, "input", None),
                    getattr(tc, "result", None),
                    status,
                    role_styles,
                    effective_tool_chars,
                )
                if tool_lines:
                    _flush_lines(pending)
                    inner = _lines_to_block(tool_lines)
                    title_style = ds.tool_error if status == "error" else ds.tool_name
                    min_inner_width = len(title) + 5
                    if inner.width + 2 < min_inner_width:
                        inner = pad(inner, right=min_inner_width - inner.width - 2)
                    bordered = border(
                        pad(inner, left=1, right=1),
                        chars=ds.tool_border,
                        style=ds.separator,
                        title=title,
                        title_style=title_style,
                    )
                    parts.append(pad(bordered, left=4))

    _flush_lines(pending)

    if not parts:
        return Block.empty(0, 0)
    if len(parts) == 1:
        return parts[0]
    return join_vertical(*parts)


def _tool_summary_lines(
    tools: list[tuple[str, int, str | None]],
) -> list[Line]:
    """Render tool summary lines from (name, count, status) tuples."""
    from siftd.output.theme import domain_styles

    ds = domain_styles()
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

        if turn.prompt_text:
            turn_lines.append(_line(("[prompt] ", ds.prompt), (ts, ds.temporal)))
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
                ("[response] ", ds.prompt),
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

        if exchange.prompt_text:
            ex_lines.append(_line(("[prompt] ", ds.prompt), (ts, ds.temporal)))
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
                ("[response] ", ds.prompt),
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
        lines.append(_line(("[prompt] ", ds.prompt), (ts, ds.temporal)))
        text = getattr(event, "text", None)
        if text:
            _append_multiline(lines, "  ", ds.assistant, text, ds.assistant, fidelity.chars)
        return _lines_to_block(lines)

    total_tokens = getattr(event, "input_tokens", 0) + getattr(event, "output_tokens", 0)
    header_parts: list[tuple[str, Style]] = [
        ("[response] ", ds.prompt),
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


def render_list_block(
    summaries: list,
    fidelity: Fidelity,
) -> Block | None:
    """Render conversation list as a styled painted table.

    Depth controls which columns are visible:
        0 (brief): id, timestamp, workspace
        1-2 (default): + model, turns, tokens, cost
        3+ (full): + prompts, responses, tags

    Returns None for empty lists (emit_output no-ops on None).
    """
    if not summaries:
        return None

    from painted import Align, current_palette
    from painted import Style as PStyle

    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace

    p = current_palette()
    depth = fidelity.depth

    col_defs: list[tuple[str, Callable, PStyle, Align]] = [
        ("id", lambda c: c.id[:12] if c.id else "", p.accent, Align.START),
        ("started_at", lambda c: fmt_timestamp(c.started_at), p.muted, Align.START),
        ("workspace", lambda c: fmt_workspace(c.workspace_path), PStyle(), Align.START),
    ]
    if depth >= 1:
        col_defs.extend([
            ("model", lambda c: fmt_model(c.model) if c.model else "", PStyle(), Align.START),
            ("turns", lambda c: f"{c.prompt_count}p/{c.response_count}r", p.muted, Align.END),
            ("tokens", lambda c: fmt_tokens(c.total_tokens), p.muted, Align.END),
            ("cost", lambda c: f"${c.cost:.4f}" if c.cost else "$0.0000", p.muted, Align.END),
        ])
    if depth >= 3:
        col_defs.extend([
            ("prompts", lambda c: str(c.prompt_count), p.muted, Align.END),
            ("responses", lambda c: str(c.response_count), p.muted, Align.END),
            ("tags", lambda c: ", ".join(c.tags) if c.tags else "", p.accent, Align.START),
        ])

    return _styled_table(col_defs, summaries)


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
        ("session", lambda s: s.session_id[:8], p.accent, Align.START),
        ("workspace", _workspace, PStyle(), Align.START),
        ("activity", lambda s: fmt_ago(now - s.last_activity), p.muted, Align.START),
        ("exchanges", _exchanges, p.muted, Align.START),
        ("model", lambda s: fmt_model(s.model), PStyle(), Align.START),
        ("adapter", lambda s: s.adapter_name or "", p.muted, Align.START),
        ("agents", _suffix, p.accent, Align.START),
    ]

    return _styled_table(col_defs, sessions)
