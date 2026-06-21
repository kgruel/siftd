"""Format-neutral tool content extraction.

Parses raw JSON tool input/result into structured presentations that any
output format (terminal, HTML, markdown) can render. The extraction logic
(JSON field selection, truncation, preview) happens once; each emitter
decides how to style the result.

Each extractor returns a ToolPresentation — a dataclass with semantic fields
that carry enough information for rich rendering without format coupling.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from siftd.output.common import truncate_text

MAX_PREVIEW_LINES = 6


@dataclass(frozen=True)
class ToolPresentation:
    """Format-neutral tool content extracted from raw JSON.

    Fields:
        headline: Primary display line ("$ cmd", "path:range", "/pattern/ in dir").
        meta: Secondary info ("exit: 0 · 1.2s", "(45 tokens)").
        output: Command/search output preview (may be truncated).
        removed: Content that was removed (file.edit old_string).
        added: Content that was added (file.edit new_string).
        tasks: Checklist items (ui.todo) as (text, done) tuples.
        error: Error message when status is error.
        overflow: Number of output lines truncated from preview.
        status: Original status string ("success", "error").
    """

    headline: str
    meta: str | None = None
    output: str | None = None
    removed: str | None = None
    added: str | None = None
    tasks: list[tuple[str, bool]] = field(default_factory=list)
    error: str | None = None
    overflow: int = 0
    status: str | None = None


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def _parse(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _first_key(d: dict, *keys: str) -> str:
    """Return value of first present non-empty key, or empty string."""
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def _preview(text: str, tool_chars: int) -> tuple[str, int]:
    """Truncate output to preview size. Returns (preview, overflow_lines)."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return "", 0
    max_lines = 0 if tool_chars == 0 else MAX_PREVIEW_LINES
    if max_lines == 0 or len(lines) <= max_lines:
        return truncate_text("\n".join(lines), tool_chars), 0
    preview = truncate_text("\n".join(lines[:max_lines]), tool_chars)
    return preview, len(lines) - max_lines


# ---------------------------------------------------------------------------
# Tool-specific extractors
# ---------------------------------------------------------------------------


def _extract_shell(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)
    res = _parse(raw_result)

    command = ""
    if isinstance(inp, dict):
        command = _first_key(inp, "command", "cmd")
    elif raw_input:
        command = str(raw_input)

    headline = f"$ {command}" if command else "$ (unknown)"
    meta_parts: list[str] = []
    output = None
    overflow = 0

    if isinstance(res, dict):
        exit_code = res.get("exit_code")
        if exit_code is not None:
            meta_parts.append(f"exit: {exit_code}")
        wall = res.get("wall_time_seconds") or res.get("wall_time")
        if wall is not None:
            meta_parts.append(f"{float(wall):.1f}s")
        raw_output = res.get("output", "")
        if raw_output:
            output, overflow = _preview(raw_output, tool_chars)
    elif raw_result:
        output, overflow = _preview(raw_result, tool_chars)

    error = None
    if status == "error" and output:
        error = output
        output = None

    return ToolPresentation(
        headline=headline,
        meta=" · ".join(meta_parts) if meta_parts else None,
        output=output,
        error=error,
        overflow=overflow,
        status=status,
    )


def _extract_file_read(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)
    res = _parse(raw_result)

    path_str = ""
    if isinstance(inp, dict):
        path = _first_key(inp, "file_path", "path")
        if path:
            path_str = path
            offset = inp.get("offset")
            limit = inp.get("limit")
            if offset is not None and limit is not None:
                path_str += f":{offset}-{offset + limit - 1}"
            elif offset is not None:
                path_str += f":{offset}"
    elif raw_input:
        path_str = str(raw_input)

    headline = path_str or "(unknown)"

    meta = None
    if isinstance(res, dict):
        tokens = res.get("original_token_count")
        if tokens:
            meta = f"({tokens} tokens)"

    error = None
    if status == "error":
        if isinstance(res, dict):
            error = _first_key(res, "error", "message", "output")
        elif raw_result:
            error = raw_result

    return ToolPresentation(headline=headline, meta=meta, error=error, status=status)


def _extract_file_edit(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)

    path_str = ""
    removed = None
    added = None
    if isinstance(inp, dict):
        path_str = _first_key(inp, "file_path", "path")
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        if old:
            removed = old.strip() if tool_chars == 0 else old.strip()[:tool_chars]
        if new:
            added = new.strip() if tool_chars == 0 else new.strip()[:tool_chars]
    elif raw_input:
        path_str = str(raw_input)

    error = None
    if status == "error":
        res = _parse(raw_result)
        if isinstance(res, dict):
            error = _first_key(res, "error", "message", "text")
        elif raw_result:
            error = raw_result

    return ToolPresentation(
        headline=path_str or "(unknown)",
        removed=removed,
        added=added,
        error=error,
        status=status,
    )


def _extract_file_write(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)

    path_str = ""
    meta = None
    if isinstance(inp, dict):
        path_str = _first_key(inp, "file_path", "path")
        content = inp.get("content", "")
        if content:
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            meta = f"({line_count} lines)"
    elif raw_input:
        path_str = str(raw_input)

    error = None
    if status == "error":
        res = _parse(raw_result)
        if isinstance(res, dict):
            error = _first_key(res, "error", "message", "text")
        elif raw_result:
            error = raw_result

    return ToolPresentation(
        headline=path_str or "(unknown)", meta=meta, error=error, status=status,
    )


def _extract_search_grep(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)
    res = _parse(raw_result)

    if isinstance(inp, dict):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        glob_filter = inp.get("include") or inp.get("glob", "")
        parts = []
        if pattern:
            parts.append(f"/{pattern}/")
        if path:
            parts.append(f"in {path}")
        if glob_filter:
            parts.append(glob_filter)
        headline = " ".join(parts) if parts else "(unknown)"
    elif raw_input:
        headline = str(raw_input)
    else:
        headline = "(unknown)"

    output = None
    overflow = 0
    if isinstance(res, dict):
        raw_output = res.get("output", "")
        if raw_output:
            output, overflow = _preview(raw_output, tool_chars)
    elif raw_result and status != "error":
        output, overflow = _preview(raw_result, tool_chars)

    error = None
    if status == "error":
        if isinstance(res, dict):
            error = _first_key(res, "error", "output", "message")
        elif raw_result:
            error = raw_result

    return ToolPresentation(
        headline=headline, output=output, error=error, overflow=overflow, status=status,
    )


def _extract_file_glob(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)
    res = _parse(raw_result)

    if isinstance(inp, dict):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        parts = []
        if pattern:
            parts.append(pattern)
        if path:
            parts.append(f"in {path}")
        headline = " ".join(parts) if parts else "(unknown)"
    elif raw_input:
        headline = str(raw_input)
    else:
        headline = "(unknown)"

    output = None
    overflow = 0
    if isinstance(res, dict):
        raw_output = res.get("output", "")
        if raw_output:
            output, overflow = _preview(raw_output, tool_chars)
    elif raw_result:
        output, overflow = _preview(raw_result, tool_chars)

    return ToolPresentation(
        headline=headline, output=output, overflow=overflow, status=status,
    )


def _extract_todo(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    inp = _parse(raw_input)

    if isinstance(inp, dict):
        title = _first_key(inp, "title")
        headline = title or "Tasks"
        task_list = inp.get("tasks") or inp.get("plan") or []
        tasks: list[tuple[str, bool]] = []
        for item in task_list:
            if isinstance(item, dict):
                text = _first_key(item, "description", "step", "content")
                done = item.get("status") in ("done", "completed")
                if text:
                    tasks.append((text, done))
            elif isinstance(item, str) and item.strip():
                tasks.append((item.strip(), False))
        return ToolPresentation(headline=headline, tasks=tasks, status=status)

    headline = str(raw_input) if raw_input else "Tasks"
    return ToolPresentation(headline=headline, status=status)


def _extract_generic(
    raw_input: str | None, raw_result: str | None, status: str | None, tool_chars: int,
) -> ToolPresentation:
    headline = _format_generic_input(raw_input) if raw_input else "(no input)"

    output = None
    overflow = 0
    error = None

    if raw_result:
        formatted = _format_generic_result(raw_result)
        if formatted:
            if status == "error":
                error = formatted
            else:
                output, overflow = _preview(formatted, tool_chars)

    return ToolPresentation(
        headline=headline, output=output, error=error, overflow=overflow, status=status,
    )


# ---------------------------------------------------------------------------
# Generic formatting helpers (ported from painted_bridge)
# ---------------------------------------------------------------------------

_INPUT_PRIORITY_KEYS = (
    "description", "command", "cmd", "file_path", "path",
    "pattern", "query", "url", "title",
)

_RESULT_OUTPUT_KEYS = ("output", "text", "result", "message")
_RESULT_META_KEYS = ("exit_code", "wall_time_seconds", "wall_time", "duration")
_RESULT_COMPACT_KEYS = ("output", "result", "message", "error", "status")


def _format_generic_input(raw: str) -> str:
    d = _parse(raw)
    if isinstance(d, dict):
        parts = []
        for key in _INPUT_PRIORITY_KEYS:
            val = d.get(key)
            if val:
                parts.append(f"{key}: {val}")
        if parts:
            return " · ".join(parts)
        return json.dumps(d, ensure_ascii=False)[:200]
    return str(raw).strip()


def _format_generic_result(raw: str) -> str:
    d = _parse(raw)
    if isinstance(d, dict):
        # Check for output field
        for key in _RESULT_OUTPUT_KEYS:
            val = d.get(key)
            if val:
                meta = []
                for mk in _RESULT_META_KEYS:
                    mv = d.get(mk)
                    if mv is not None:
                        meta.append(f"{mk}: {mv}")
                prefix = (" · ".join(meta) + "\n") if meta else ""
                return prefix + str(val)
        # Compact format
        parts = []
        for key in _RESULT_COMPACT_KEYS:
            val = d.get(key)
            if val:
                parts.append(f"{key}: {val}")
        if parts:
            return " · ".join(parts)
        return json.dumps(d, ensure_ascii=False)[:200]
    return str(raw).strip()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_Extractor = Callable[[str | None, str | None, str | None, int], ToolPresentation]

_EXTRACTORS: dict[str, _Extractor] = {
    # Long-form names (some adapters use these)
    "shell.execute": _extract_shell,
    "file.read": _extract_file_read,
    "file.edit": _extract_file_edit,
    "file.write": _extract_file_write,
    "search.grep": _extract_search_grep,
    "file.glob": _extract_file_glob,
    "ui.todo": _extract_todo,
    # Short names (as stored by Claude Code adapter)
    "bash": _extract_shell,
    "read": _extract_file_read,
    "edit": _extract_file_edit,
    "write": _extract_file_write,
    "grep": _extract_search_grep,
    "glob": _extract_file_glob,
    "todo": _extract_todo,
}


def extract_tool_presentation(
    name: str,
    raw_input: str | None,
    raw_result: str | None,
    status: str | None,
    tool_chars: int = 120,
) -> ToolPresentation:
    """Extract format-neutral presentation from raw tool JSON.

    Dispatches to tool-specific extractor or falls back to generic.
    """
    extractor = _EXTRACTORS.get(name, _extract_generic)
    return extractor(raw_input, raw_result, status, tool_chars)
