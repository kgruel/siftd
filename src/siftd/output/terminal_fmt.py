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


def render_search(results: list, fidelity: Fidelity, **context: Any) -> str:
    """Render search results as terminal text.

    Context keys:
        query: str — the search query
        mode: str — "chunks", "conversations", or "thread"
        tier1: list — expanded results (thread mode)
        tier2: list — compact results (thread mode)
    """
    from siftd.output.common import truncate_text
    from siftd.output.formatters import format_refs_annotation

    query = context.get("query", "")
    mode = context.get("mode", "chunks")

    lines: list[str] = []

    if mode == "conversations":
        lines.append(f"Conversations for: {query}\n")
        for r in results:
            conv_id = r.get("conversation_id", "")
            short_id = conv_id[:12]
            ws = r.get("_workspace", "")
            started = r.get("_started_at", "")
            max_s = r.get("max_score", 0.0)
            mean_s = r.get("mean_score", 0.0)
            n_chunks = r.get("chunk_count", 0)

            lines.append(
                f"  {short_id}  max={max_s:.3f}  mean={mean_s:.3f}"
                f"  [{n_chunks} chunks]  {started}  {ws}"
            )
            snippet = truncate_text(
                r.get("best_excerpt", ""), 200
            ).replace("\n", " ")
            lines.append(f"    {snippet}")
            lines.append("")
        return "\n".join(lines)

    if mode == "thread":
        tier1 = context.get("tier1", [])
        tier2 = context.get("tier2", [])
        lines.append(f"Results for: {query}\n")

        for r in tier1:
            ws = r.get("_workspace", "")
            started = r.get("_started_at", "")
            lines.append(
                f"\u2500\u2500\u2500 {ws}  {started} "
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            )
            exchanges = r.get("_exchanges")
            if exchanges:
                for _pid, prompt_text, response_text in exchanges:
                    if prompt_text:
                        pt = truncate_text(prompt_text, 500)
                        lines.append(f"  [user] {pt}")
                    if response_text:
                        rt = truncate_text(response_text, 800)
                        lines.append(f"  [asst] {rt}")
                if not exchanges:
                    lines.append("  (no exchange text available)")
            else:
                chunk_type = r.get("chunk_type", "")
                side = "[user]" if chunk_type == "prompt" else "[asst]"
                text = truncate_text(r.get("text", "").strip(), 600)
                lines.append(f"  {side} {text}")

            file_refs = r.get("file_refs")
            if file_refs:
                annotation = format_refs_annotation(file_refs)
                lines.append(f"  {annotation}")
            lines.append("")

        if tier2:
            lines.append(f"  {'\u2500' * 50}")
            lines.append("  More results:\n")
            for r in tier2:
                conv_id = r.get("conversation_id", "")
                short_id = conv_id[:12]
                ws = r.get("_workspace", "")
                started = r.get("_started_at", "")
                score = r.get("score", 0.0)
                snippet = truncate_text(
                    r.get("text", ""), 120
                ).replace("\n", " ")
                file_refs = r.get("file_refs", [])
                files_tag = f"  [{len(file_refs)} files]" if file_refs else ""
                lines.append(
                    f"  {short_id}  {score:.3f}  {ws:20s}  {started}{files_tag}  {snippet}"
                )
            lines.append("")
        return "\n".join(lines)

    # Default: chunks mode (handles default, verbose, full, context)
    lines.append(f"Results for: {query}\n")
    for r in results:
        conv_id = r.get("conversation_id", "")
        short_id = conv_id[:12]
        ws = r.get("_workspace", "")
        started = r.get("_started_at", "")
        chunk_type = r.get("chunk_type", "").upper()[:8]
        score = r.get("score", 0.0)

        lines.append(
            f"  {short_id}  {score:.3f}  [{chunk_type:8s}]  {started}  {ws}"
        )

        exchanges = r.get("_exchanges")
        context_data = r.get("_context")
        if exchanges:
            for _pid, prompt_text, response_text in exchanges:
                if prompt_text:
                    lines.append(f"    > {prompt_text.splitlines()[0]}")
                    for line in prompt_text.splitlines()[1:]:
                        lines.append(f"    > {line}")
                if response_text:
                    for line in response_text.splitlines():
                        lines.append(f"    {line}")
                if prompt_text or response_text:
                    lines.append("    ---")
        elif context_data:
            for pid, prompt_text, response_text, is_match in context_data:
                marker = ">>>" if is_match else "   "
                if prompt_text:
                    lines.append(f"    {marker} > {prompt_text.splitlines()[0]}")
                    for line in prompt_text.splitlines()[1:]:
                        lines.append(f"    {marker} > {line}")
                if response_text:
                    for line in response_text.splitlines():
                        lines.append(f"    {marker} {line}")
                lines.append(f"    {marker} ---")
        else:
            # Snippet display: use fidelity.chars, default 200 for search
            chars = fidelity.chars
            if chars == 0 and fidelity.depth < 2:
                chars = 200
            if chars > 0:
                snippet = truncate_text(r.get("text", ""), chars).replace("\n", " ")
                lines.append(f"    {snippet}")
            else:
                for line in r.get("text", "").splitlines():
                    lines.append(f"    {line}")

        file_refs = r.get("file_refs")
        if file_refs:
            annotation = format_refs_annotation(file_refs)
            lines.append(f"    {annotation}")

        lines.append("")

    return "\n".join(lines)
