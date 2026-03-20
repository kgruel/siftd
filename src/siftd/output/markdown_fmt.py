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


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation list as a markdown table.

    Depth controls column density:
        0 (brief): ID, Started, Workspace
        1-2 (default): adds Model, Turns, Tokens, Cost
        3+ (full): adds Tags
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace

    if not summaries:
        return ""

    depth = fidelity.depth

    headers = ["ID", "Started", "Workspace"]
    if depth >= 1:
        headers += ["Model", "Turns", "Tokens", "Cost"]
    if depth >= 3:
        headers.append("Tags")

    rows = []
    for c in summaries:
        row = [
            c.id[:12] if c.id else "",
            fmt_timestamp(c.started_at),
            fmt_workspace(c.workspace_path),
        ]
        if depth >= 1:
            row += [
                fmt_model(c.model) if c.model else "",
                f"{c.prompt_count}p/{c.response_count}r",
                fmt_tokens(c.total_tokens),
                f"${c.cost:.4f}" if c.cost else "$0.0000",
            ]
        if depth >= 3:
            row.append(", ".join(c.tags) if c.tags else "")
        rows.append(row)

    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
