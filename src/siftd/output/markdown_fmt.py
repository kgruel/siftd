"""Markdown output format — renders conversations as GFM-compatible markdown.

Used for `siftd export` and file output contexts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity

FORMATTER_INTERFACE_VERSION = 1
name = "markdown"
media_type = "text/markdown"
brief_chars = 300


def render_detail(turns: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation detail as markdown string.

    This is a stub that will be replaced by the narrative walker (Stage 3).
    Currently delegates to the caller via context for backward compat.

    Context keys:
        _render_fn: callable(turns, fidelity, **context) -> str
    """
    render_fn = context.get("_render_fn")
    if render_fn:
        return render_fn(turns, fidelity, **context)
    return ""
