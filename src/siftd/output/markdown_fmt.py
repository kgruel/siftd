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
    """Render conversation detail as GFM markdown.

    Context keys:
        detail: conversation metadata object (ConversationDetail or ExportedConversation)
        no_header: bool — omit session header (default: False)
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace
    from siftd.output.narrative import MarkdownEmitter, walk_narrative

    detail = context.get("detail")
    no_header = context.get("no_header", False)

    lines: list[str] = []

    if detail and not no_header:
        detail_id = getattr(detail, "id", "") or ""
        lines.append(f"# Session {detail_id[:12]}")
        meta_parts: list[str] = []
        ws = fmt_workspace(getattr(detail, "workspace_path", None))
        if ws:
            meta_parts.append(ws)
        ts = fmt_timestamp(getattr(detail, "started_at", None))
        if ts:
            meta_parts.append(ts)
        model = fmt_model(getattr(detail, "model", None))
        if model:
            meta_parts.append(model)
        total_tokens = getattr(detail, "total_tokens", None)
        if total_tokens is None:
            total_tokens = (
                getattr(detail, "total_input_tokens", 0)
                + getattr(detail, "total_output_tokens", 0)
            )
        if total_tokens:
            meta_parts.append(fmt_tokens(total_tokens) + " tokens")
        tags = getattr(detail, "tags", None)
        if tags:
            meta_parts.append("tags: " + ", ".join(tags))
        if meta_parts:
            lines.append(f"*{' · '.join(meta_parts)}*")
        lines.append("")

    for turn in turns:
        ts = fmt_timestamp(getattr(turn, "timestamp", None), time_only=True)
        ts_prefix = f"{ts} — " if ts else ""

        prompt_text = getattr(turn, "prompt_text", None)
        if prompt_text:
            lines.append(f"### {ts_prefix}User")
            lines.append("")
            prompt = prompt_text.strip()
            if fidelity.chars > 0 and len(prompt) > fidelity.chars:
                prompt = prompt[:fidelity.chars] + "..."
            lines.append(prompt)
            lines.append("")

        narrative = getattr(turn, "narrative", [])
        if narrative:
            lines.append(f"### {ts_prefix}Assistant")
            lines.append("")
            emitter = MarkdownEmitter()
            walk_narrative(narrative, emitter, fidelity=fidelity)
            lines.extend(emitter.lines)

        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip()


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
