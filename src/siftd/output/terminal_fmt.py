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


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation list as terminal text.

    Depth controls column density:
        0 (brief): id, timestamp, workspace
        1-2 (default): adds model, turns, tokens, cost
        3+ (full): aligned table with all columns including tags
    """
    from siftd.output.common import (
        fmt_model,
        fmt_timestamp,
        fmt_tokens,
        fmt_workspace,
        format_table,
    )

    if not summaries:
        return ""

    depth = fidelity.depth

    if depth >= 3:
        columns = [
            "id", "workspace", "model", "started_at",
            "prompts", "responses", "tokens", "cost", "tags",
        ]
        rows = []
        for c in summaries:
            rows.append([
                c.id[:12] if c.id else "",
                fmt_workspace(c.workspace_path),
                c.model or "",
                fmt_timestamp(c.started_at),
                str(c.prompt_count),
                str(c.response_count),
                str(c.total_tokens),
                f"${c.cost:.4f}" if c.cost else "$0.0000",
                ", ".join(c.tags) if c.tags else "",
            ])
        return format_table(columns, rows)

    lines = []
    for c in summaries:
        cid = c.id[:12] if c.id else ""
        started = fmt_timestamp(c.started_at)
        ws = fmt_workspace(c.workspace_path)

        if depth <= 0:
            lines.append(f"{cid}  {started}  {ws}")
        else:
            model = fmt_model(c.model) if c.model else ""
            tokens = fmt_tokens(c.total_tokens)
            cost = f"${c.cost:.4f}" if c.cost else "$0.0000"
            lines.append(
                f"{cid}  {started}  {ws}  {model}"
                f"  {c.prompt_count}p/{c.response_count}r  {tokens} tok  {cost}"
            )

    return "\n".join(lines)
