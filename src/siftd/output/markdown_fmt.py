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


def render_detail(result: Any, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation detail as GFM markdown.

    Args:
        result: ConversationDetail object, or raw turns list (backward compat).

    Context keys:
        turns: override which turns to render (default: result.turns)
        no_header: bool — omit session header (default: False)
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace
    from siftd.output.narrative import MarkdownEmitter, walk_narrative

    if hasattr(result, "turns"):
        detail = result
        turns = context.get("turns", detail.turns)
    else:
        turns = result
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
        1-2 (default): adds Model, Turns, Tokens
        3+ (full): adds Cost, Tags

    Context keys:
        caveats: list[Finding] — drives '?' Cost cells for rows targeted by
            pricing-missing caveats (depth>=3 only).
    """
    from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace

    if not summaries:
        return ""

    depth = fidelity.depth
    caveats = context.get("caveats") or []
    missing_pricing_ids = frozenset(
        c.target for c in caveats
        if c.check == "pricing-missing" and c.target
    )

    headers = ["ID", "Started", "Workspace"]
    if depth >= 1:
        headers += ["Model", "Turns", "Tokens"]
    if depth >= 3:
        headers += ["Cost", "Tags"]

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
            ]
        if depth >= 3:
            if c.id in missing_pricing_ids:
                cost_cell = "?"
            elif c.cost:
                cost_cell = f"${c.cost:.4f}"
            else:
                cost_cell = "$0.0000"
            row.append(cost_cell)
            row.append(", ".join(c.tags) if c.tags else "")
        rows.append(row)

    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_search(results: list, fidelity: Fidelity, **context: Any) -> str:
    """Render search results as GFM markdown.

    Context keys:
        query: str — the search query
        mode: str — "chunks", "conversations", or "thread"
        tier1: list — expanded results (thread mode)
        tier2: list — compact results (thread mode)
    """
    from siftd.output.common import truncate_text

    query = context.get("query", "")
    mode = context.get("mode", "chunks")

    lines: list[str] = []

    if mode == "conversations":
        lines.append(f"## Conversations for: {query}")
        lines.append("")
        headers = ["ID", "Max Score", "Mean Score", "Chunks", "Started", "Workspace"]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for r in results:
            conv_id = r.get("conversation_id", "")
            row = [
                conv_id[:12],
                f"{r.get('max_score', 0.0):.3f}",
                f"{r.get('mean_score', 0.0):.3f}",
                str(r.get("chunk_count", 0)),
                r.get("_started_at", ""),
                r.get("_workspace", ""),
            ]
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    if mode == "thread":
        tier1 = context.get("tier1", [])
        tier2 = context.get("tier2", [])
        lines.append(f"## Results for: {query}")
        lines.append("")

        for r in tier1:
            ws = r.get("_workspace", "")
            started = r.get("_started_at", "")
            lines.append(f"### {ws} — {started}")
            lines.append("")
            exchanges = r.get("_exchanges")
            if exchanges:
                for _pid, prompt_text, response_text in exchanges:
                    if prompt_text:
                        lines.append(f"> {prompt_text.strip()}")
                        lines.append("")
                    if response_text:
                        lines.append(response_text.strip())
                        lines.append("")
            else:
                text = r.get("text", "").strip()
                if text:
                    lines.append(text)
                    lines.append("")
            lines.append("---")
            lines.append("")

        if tier2:
            lines.append("### More results")
            lines.append("")
            for r in tier2:
                conv_id = r.get("conversation_id", "")
                ws = r.get("_workspace", "")
                started = r.get("_started_at", "")
                score = r.get("score", 0.0)
                snippet = truncate_text(
                    r.get("text", ""), 120
                ).replace("\n", " ")
                lines.append(f"- **{conv_id[:12]}** {score:.3f} — {ws} {started} — {snippet}")
            lines.append("")
        return "\n".join(lines)

    # Chunks mode
    lines.append(f"## Results for: {query}")
    lines.append("")
    for r in results:
        conv_id = r.get("conversation_id", "")
        chunk_type = r.get("chunk_type", "").upper()[:8]
        score = r.get("score", 0.0)
        ws = r.get("_workspace", "")
        started = r.get("_started_at", "")

        lines.append(f"#### {conv_id[:12]} — {score:.3f} [{chunk_type}] {started} {ws}")
        lines.append("")

        exchanges = r.get("_exchanges")
        context_data = r.get("_context")
        if exchanges:
            for _pid, prompt_text, response_text in exchanges:
                if prompt_text:
                    lines.append(f"> {prompt_text.strip()}")
                    lines.append("")
                if response_text:
                    lines.append(response_text.strip())
                    lines.append("")
        elif context_data:
            for pid, prompt_text, response_text, is_match in context_data:
                marker = "**>>>**" if is_match else ""
                if prompt_text:
                    lines.append(f"> {marker} {prompt_text.strip()}")
                    lines.append("")
                if response_text:
                    lines.append(f"{marker} {response_text.strip()}")
                    lines.append("")
        else:
            chars = fidelity.chars
            if chars == 0 and fidelity.depth < 2:
                chars = 200
            text = r.get("text", "")
            if chars > 0:
                text = truncate_text(text, chars)
            lines.append(text)
            lines.append("")

    return "\n".join(lines)
