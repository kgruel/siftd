"""Narrative walker: shared decision logic for rendering conversation narratives.

Walks NarrativeBlock lists and calls format-agnostic emitter callbacks based
on Fidelity settings. The walker decides *what* to show; emitters decide *how*.

Handles both NarrativeBlock (from DB/query) and PeekNarrativeBlock (from
disk/peek) via duck typing on block_type, content, and tool_calls attributes.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Protocol

from siftd.output.common import truncate_text

if TYPE_CHECKING:
    from painted import Fidelity


class NarrativeEmitter(Protocol):
    """Receives narrative events in display order.

    Emitters accumulate output in their preferred format
    (painted Lines, markdown strings, JSON dicts, etc.).
    """

    def text(self, content: str) -> None:
        """Emit a text content block."""
        ...

    def thinking(self, content: str) -> None:
        """Emit expanded thinking content."""
        ...

    def thinking_placeholder(self) -> None:
        """Emit a thinking placeholder (thinking exists but not expanded)."""
        ...

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        """Emit a consolidated tool summary.

        Each tuple is (tool_name, count, status_or_None).
        Called when tools are not expanded — the emitter renders a compact hint.
        """
        ...

    def tool_content(
        self,
        name: str,
        count: int,
        raw_input: str | None,
        raw_result: str | None,
        status: str | None,
    ) -> None:
        """Emit detailed tool call content (input/result).

        Called when tools ARE expanded — one call per tool.
        """
        ...

    def tool_output(self, block_type: str, content: str) -> None:
        """Emit tool_result or tool_output content."""
        ...


def _collapse_tools(tools: list) -> list[tuple[str, int, str | None]]:
    """Collapse a list of tool call objects into (name, count, status) tuples.

    Aggregates by name, preserving order of first occurrence.
    """
    counts: Counter[str] = Counter()
    statuses: dict[str, str | None] = {}
    order: list[str] = []

    for tc in tools:
        name = getattr(tc, "tool_name", "unknown")
        count = getattr(tc, "count", 1)
        status = getattr(tc, "status", None)
        if name not in counts:
            order.append(name)
            statuses[name] = status
        counts[name] += count
        # Propagate error status
        if status and status != "success":
            statuses[name] = status

    return [(name, counts[name], statuses.get(name)) for name in order]


def walk_narrative(
    blocks: list,
    emitter: NarrativeEmitter,
    *,
    fidelity: Fidelity,
    tool_chars: int = 0,
) -> None:
    """Walk narrative blocks, calling emitter methods based on fidelity.

    Decision logic:
    - fidelity.shows("thinking"): expanded thinking vs placeholder
    - fidelity.shows("tools"): detailed tool content vs consolidated summary
    - fidelity.chars: text truncation limit (0 = no truncation)

    When tools or thinking are not expanded, consecutive non-text blocks
    are accumulated and flushed as a single summary before the next text
    block or at end of narrative.
    """
    show_thinking = fidelity.shows("thinking")
    show_tools = fidelity.shows("tools")
    chars_limit = fidelity.chars

    # Accumulate non-expanded blocks for consolidated summary
    pending_tools: list = []
    pending_has_thinking = False

    def _flush() -> None:
        nonlocal pending_tools, pending_has_thinking
        if pending_has_thinking and not show_thinking:
            emitter.thinking_placeholder()
            pending_has_thinking = False
        if pending_tools and not show_tools:
            collapsed = _collapse_tools(pending_tools)
            emitter.tool_summary(collapsed)
            pending_tools = []

    for block in blocks:
        block_type = getattr(block, "block_type", "")
        content = getattr(block, "content", None) or ""

        if block_type == "text":
            _flush()
            text = content.strip()
            if text:
                if chars_limit > 0:
                    text = truncate_text(text, chars_limit)
                emitter.text(text)

        elif block_type == "thinking":
            if show_thinking and content.strip():
                _flush()
                emitter.thinking(content.strip())
            elif content:
                pending_has_thinking = True

        elif block_type == "tool_calls":
            tool_calls = getattr(block, "tool_calls", [])
            if show_tools:
                _flush()
                for tc in tool_calls:
                    emitter.tool_content(
                        getattr(tc, "tool_name", "unknown"),
                        getattr(tc, "count", 1),
                        getattr(tc, "input", None),
                        getattr(tc, "result", None),
                        getattr(tc, "status", None),
                    )
            else:
                pending_tools.extend(tool_calls)

        elif block_type in ("tool_result", "tool_output"):
            if show_tools and content.strip():
                _flush()
                emitter.tool_output(block_type, content.strip())

    _flush()


# ---------------------------------------------------------------------------
# Built-in emitters
# ---------------------------------------------------------------------------


class MarkdownEmitter:
    """Emits narrative as markdown lines.

    Accumulates into self.lines: list[str].
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def text(self, content: str) -> None:
        self.lines.append(content)
        self.lines.append("")

    def thinking(self, content: str) -> None:
        self.lines.append("> **Thinking**")
        self.lines.append(">")
        for line in content.split("\n"):
            self.lines.append(f"> {line}")
        self.lines.append("")

    def thinking_placeholder(self) -> None:
        self.lines.append("*[thinking]*")
        self.lines.append("")

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        parts = []
        for name, count, _status in tools:
            if count > 1:
                parts.append(f"{name} ×{count}")
            else:
                parts.append(name)
        self.lines.append(f"*[{', '.join(parts)}]*")
        self.lines.append("")

    def tool_content(
        self,
        name: str,
        count: int,
        raw_input: str | None,
        raw_result: str | None,
        status: str | None,
    ) -> None:
        count_suffix = f" ×{count}" if count > 1 else ""
        status_suffix = f" ({status})" if status and status != "success" else ""
        header = f"- **{name}**{count_suffix}{status_suffix}"

        if raw_input:
            first_line = raw_input.strip().split("\n")[0]
            if len(first_line) > 100:
                first_line = first_line[:100] + "..."
            header += f" `{first_line}`"

        self.lines.append(header)

        if raw_result:
            result_text = raw_result.strip()
            if len(result_text) > 200:
                result_text = result_text[:200] + "..."
            for rline in result_text.split("\n"):
                self.lines.append(f"  {rline}")

    def tool_output(self, block_type: str, content: str) -> None:
        self.lines.append(f"```\n{content}\n```")
        self.lines.append("")


class JsonEmitter:
    """Emits narrative as JSON-serializable dicts.

    Accumulates into self.blocks: list[dict].
    """

    def __init__(self) -> None:
        self.blocks: list[dict] = []

    def text(self, content: str) -> None:
        self.blocks.append({"type": "text", "content": content})

    def thinking(self, content: str) -> None:
        self.blocks.append({"type": "thinking", "content": content})

    def thinking_placeholder(self) -> None:
        self.blocks.append({"type": "thinking"})

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        self.blocks.append({
            "type": "tool_calls",
            "tools": [
                {"name": name, "count": count, **({"status": status} if status else {})}
                for name, count, status in tools
            ],
        })

    def tool_content(
        self,
        name: str,
        count: int,
        raw_input: str | None,
        raw_result: str | None,
        status: str | None,
    ) -> None:
        d: dict = {"name": name, "count": count}
        if status:
            d["status"] = status
        if raw_input:
            d["input"] = raw_input
        if raw_result:
            d["result"] = raw_result
        self.blocks.append({"type": "tool_call", **d})

    def tool_output(self, block_type: str, content: str) -> None:
        self.blocks.append({"type": block_type, "content": content})
