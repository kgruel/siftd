"""Presentation-layer emitters for narrative rendering.

The narrative walker, protocol, and JsonEmitter have moved to
siftd.serialization.narrative. This module re-exports them for
backward compatibility and provides presentation-specific emitters
(MarkdownEmitter, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-export from serialization layer for backward compatibility
from siftd.serialization.narrative import (
    JsonEmitter,
    NarrativeEmitter,
    walk_narrative,
)

if TYPE_CHECKING:
    pass

__all__ = ["JsonEmitter", "MarkdownEmitter", "NarrativeEmitter", "walk_narrative"]


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
