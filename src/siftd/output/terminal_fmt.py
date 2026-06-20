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


def render_detail(result: Any, fidelity: Fidelity, **context: Any) -> Any:
    """Render conversation detail as a painted Block.

    Args:
        result: ConversationDetail object, or raw turns list (backward compat).

    Context keys:
        turns: override which turns to render (default: result.turns)
        tool_chars: int — tool content char limit (0 = no limit)
    """
    from siftd.output.painted_bridge import render_query_detail_block

    if hasattr(result, "turns"):
        detail = result
        turns = context.get("turns", detail.turns)
    else:
        turns = result
        detail = context.get("detail")
    tool_chars = context.get("tool_chars", 0)

    return render_query_detail_block(
        detail,
        turns=turns,
        fidelity=fidelity,
        tool_chars=tool_chars,
    )


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> Any:
    """Render conversation list as a painted Block.

    Depth controls column density:
        0 (brief): id, timestamp, workspace
        1-2 (default): adds model, turns, tokens, cost
        3+ (full): aligned table with all columns including tags

    Context keys:
        caveats: list[Finding] — row-scope and query-scope caveats threaded
            from dispatch. Drives '?' cells for unpriced rows and a footer
            line summarizing kinds.
    """
    from siftd.output.painted_bridge import render_list_block

    return render_list_block(summaries, fidelity, caveats=context.get("caveats"))


def render_search(results: list, fidelity: Fidelity, **context: Any) -> Any:
    """Render search results as a painted Block.

    Matched terms (FTS5 >>>...<<< markers) become accent spans and a left rail
    encodes relevance rank — see painted_bridge.render_search_block.

    Context keys:
        query: str — the search query
        mode: str — "chunks", "conversations", or "thread"
        tier1: list — expanded results (thread mode)
        tier2: list — compact results (thread mode)
        caveats: list[Finding] — threaded from dispatch; rendered as
            ``note: <message>`` lines after the last result.
    """
    from siftd.output.painted_bridge import render_search_block

    return render_search_block(
        results,
        fidelity,
        query=context.get("query", ""),
        mode=context.get("mode", "chunks"),
        tier1=context.get("tier1"),
        tier2=context.get("tier2"),
        caveats=context.get("caveats"),
    )
