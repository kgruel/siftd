"""Terminal output format — renders via painted Block/Line/Span primitives.

This is the default format when stdout is a TTY.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity

FORMATTER_INTERFACE_VERSION = 1
name = "terminal"
media_type = "terminal"
brief_chars = 80


def render_detail(turns: list, fidelity: Fidelity, **context: Any) -> Any:
    """Render conversation detail as a painted Block.

    Context keys:
        detail: ConversationDetail — full conversation metadata
        tool_chars: int — tool content char limit (0 = no limit)
    """
    from siftd.output.painted_bridge import render_query_detail_block

    detail = context.get("detail")
    tool_chars = context.get("tool_chars", 0)

    return render_query_detail_block(
        detail,
        turns=turns,
        fidelity=fidelity,
        tool_chars=tool_chars,
    )
