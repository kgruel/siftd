"""Shared narrative/block rendering for query and peek views."""

from siftd.output.common import truncate_text


def _tool_input_text(tool_call) -> str | None:
    value = getattr(tool_call, "input", None)
    if value is None:
        return None
    return str(value)


def _tool_result_text(tool_call) -> str | None:
    value = getattr(tool_call, "result", None)
    if value is None:
        return None
    return str(value)


def _append_multiline(lines: list[str], prefix: str, text: str, limit: int) -> None:
    rendered = truncate_text(text, limit)
    split = rendered.splitlines() or [rendered]
    if not split:
        return
    lines.append(f"{prefix}{split[0]}")
    continuation_indent = " " * len(prefix)
    for line in split[1:]:
        lines.append(f"{continuation_indent}{line}")


def render_narrative_blocks(
    blocks: list,
    *,
    chars_limit: int,
    tool_chars: int,
    full: bool = False,
    show_tool_content: bool = False,
) -> list[str]:
    """Render narrative blocks into indented text lines.

    Accepts block objects with .block_type, .content, and .tool_calls.
    Tool call objects may have .tool_name, .count, .status, .input, .result.
    show_tool_content controls whether tool input/result payloads are expanded,
    independently of whether tool summary lines are shown.
    """
    lines: list[str] = []

    for block in blocks:
        block_type = getattr(block, "block_type", "")
        content = getattr(block, "content", None) or ""

        if block_type == "text":
            if content:
                lines.append(f"  {truncate_text(content, chars_limit)}")
        elif block_type == "thinking":
            if content:
                lines.append(f"  [thinking] {truncate_text(content, chars_limit)}")
        elif block_type in ("tool_result", "tool_output"):
            if content and show_tool_content:
                lines.append(f"  [{block_type}] {truncate_text(content, tool_chars)}")
        elif block_type == "tool_calls":
            for tc in getattr(block, "tool_calls", []):
                name = getattr(tc, "tool_name", "unknown")
                count = getattr(tc, "count", 1)
                status = getattr(tc, "status", None)
                if count > 1 and not full:
                    line = f"    → {name} ×{count}"
                else:
                    line = f"    → {name}"
                    if count > 1:
                        line += f" ×{count}"
                if status and status != "success":
                    line += f" ({status})"

                input_text = _tool_input_text(tc)
                if input_text and show_tool_content:
                    lines.append(line)
                    _append_multiline(lines, "      input: ", input_text, tool_chars)
                else:
                    lines.append(line)

                result_text = _tool_result_text(tc)
                if result_text and show_tool_content:
                    _append_multiline(lines, "      ← ", result_text, tool_chars)
    return lines
