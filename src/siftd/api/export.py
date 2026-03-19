"""Export API for siftd.

Renders full conversation exchanges as markdown or JSON.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from siftd.api.conversations import (
    ConversationDetail,
    NarrativeBlock,
    ToolCallDetail,
    Turn,
    get_conversation,
    list_conversations,
)
from siftd.output.common import fmt_model, fmt_timestamp, fmt_tokens, fmt_workspace


@dataclass
class ExportOptions:
    """Options controlling export output."""

    json_mode: bool = False
    include_thinking: bool = False
    include_tools: bool = False
    brief: bool = False
    no_header: bool = False


@dataclass
class ExportedConversation:
    """A conversation prepared for export."""

    id: str
    workspace_path: str | None
    workspace_name: str | None
    model: str | None
    started_at: str | None
    turns: list[Turn]
    tags: list[str]
    total_tokens: int


def export_conversations(
    *,
    conversation_ids: list[str] | None = None,
    last: int | None = None,
    workspace: str | None = None,
    tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    since: str | None = None,
    before: str | None = None,
    search: str | None = None,
    db_path: Path | None = None,
    include_thinking: bool = True,
    include_tool_content: bool = False,
) -> list[ExportedConversation]:
    """Export conversations matching the specified criteria.

    Always fetches with include_thinking=True so thinking block presence
    is known (for placeholder rendering). Tool content is fetched only
    when include_tool_content=True (for --tools/--full).
    """
    if conversation_ids:
        results = []
        for cid in conversation_ids:
            detail = get_conversation(
                cid,
                db_path=db_path,
                include_thinking=include_thinking,
                include_tool_content=include_tool_content,
            )
            if detail:
                results.append(_detail_to_export(detail))
        return results

    limit = last if last else 10
    summaries = list_conversations(
        db_path=db_path,
        workspace=workspace,
        tags=tags,
        exclude_tags=exclude_tags,
        since=since,
        before=before,
        search=search,
        limit=limit,
    )

    results = []
    for summary in summaries:
        detail = get_conversation(
            summary.id,
            db_path=db_path,
            include_thinking=include_thinking,
            include_tool_content=include_tool_content,
        )
        if detail:
            results.append(_detail_to_export(detail))

    return results


def _detail_to_export(detail: ConversationDetail) -> ExportedConversation:
    """Convert ConversationDetail to ExportedConversation."""
    workspace_name = None
    if detail.workspace_path:
        workspace_name = Path(detail.workspace_path).name

    return ExportedConversation(
        id=detail.id,
        workspace_path=detail.workspace_path,
        workspace_name=workspace_name,
        model=detail.model,
        started_at=detail.started_at,
        turns=detail.turns,
        tags=detail.tags,
        total_tokens=detail.total_input_tokens + detail.total_output_tokens,
    )


# ---------------------------------------------------------------------------
# Markdown formatter
# ---------------------------------------------------------------------------


def _tool_summary(tool_calls: list[ToolCallDetail]) -> str:
    """Render tool calls as compact summary: [file.read ×3, shell.execute ×1].

    Aggregates by tool name, preserving order of first occurrence.
    """
    from collections import Counter

    counts: Counter[str] = Counter()
    order: list[str] = []
    for tc in tool_calls:
        if tc.tool_name not in counts:
            order.append(tc.tool_name)
        counts[tc.tool_name] += tc.count

    parts = []
    for name in order:
        n = counts[name]
        if n > 1:
            parts.append(f"{name} ×{n}")
        else:
            parts.append(name)
    return f"*[{', '.join(parts)}]*"


def _tool_detail_lines(tool_calls: list[ToolCallDetail]) -> list[str]:
    """Render tool calls with input/result detail."""
    lines: list[str] = []
    for tc in tool_calls:
        count_suffix = f" ×{tc.count}" if tc.count > 1 else ""
        status_suffix = f" ({tc.status})" if tc.status and tc.status != "success" else ""
        header = f"- **{tc.tool_name}**{count_suffix}{status_suffix}"

        if tc.input:
            # Show first line of input as inline hint
            first_line = tc.input.strip().split("\n")[0]
            if len(first_line) > 100:
                first_line = first_line[:100] + "..."
            header += f" `{first_line}`"

        lines.append(header)

        if tc.result:
            result_text = tc.result.strip()
            if len(result_text) > 200:
                result_text = result_text[:200] + "..."
            for rline in result_text.split("\n"):
                lines.append(f"  {rline}")

    return lines


def _render_narrative_md(
    narrative: list[NarrativeBlock],
    *,
    include_thinking: bool = False,
    include_tools: bool = False,
    brief: bool = False,
) -> list[str]:
    """Render narrative blocks to markdown lines.

    In default mode (no --tools), consecutive tool_calls blocks are
    consolidated into a single summary line with collapsed counts.
    """
    lines: list[str] = []

    # Collect all tool calls and thinking occurrences for summary mode
    if not include_tools or not include_thinking:
        pending_tools: list[ToolCallDetail] = []
        has_thinking = False

        def _flush_pending() -> None:
            nonlocal pending_tools, has_thinking
            hints: list[str] = []
            if has_thinking and not include_thinking:
                hints.append("*[thinking]*")
                has_thinking = False
            if pending_tools and not include_tools:
                hints.append(_tool_summary(pending_tools))
                pending_tools = []
            if hints:
                lines.append("  ".join(hints))
                lines.append("")

        for block in narrative:
            if block.block_type == "text" and block.content:
                _flush_pending()
                text = block.content.strip()
                if brief and len(text) > 300:
                    text = text[:300] + "..."
                lines.append(text)
                lines.append("")

            elif block.block_type == "thinking":
                if include_thinking and block.content:
                    _flush_pending()
                    lines.append("> **Thinking**")
                    lines.append(">")
                    for tline in block.content.strip().split("\n"):
                        lines.append(f"> {tline}")
                    lines.append("")
                else:
                    has_thinking = True

            elif block.block_type == "tool_calls" and block.tool_calls:
                if include_tools:
                    _flush_pending()
                    lines.extend(_tool_detail_lines(block.tool_calls))
                    lines.append("")
                else:
                    pending_tools.extend(block.tool_calls)

            elif block.block_type in ("tool_result", "tool_output"):
                if include_tools and block.content:
                    _flush_pending()
                    lines.append(f"```\n{block.content.strip()}\n```")
                    lines.append("")

        _flush_pending()
    else:
        # Full mode: render everything inline
        for block in narrative:
            if block.block_type == "text" and block.content:
                lines.append(block.content.strip())
                lines.append("")
            elif block.block_type == "thinking" and block.content:
                lines.append("> **Thinking**")
                lines.append(">")
                for tline in block.content.strip().split("\n"):
                    lines.append(f"> {tline}")
                lines.append("")
            elif block.block_type == "tool_calls" and block.tool_calls:
                lines.extend(_tool_detail_lines(block.tool_calls))
                lines.append("")
            elif block.block_type in ("tool_result", "tool_output") and block.content:
                lines.append(f"```\n{block.content.strip()}\n```")
                lines.append("")

    return lines


def format_markdown(
    conversations: list[ExportedConversation],
    options: ExportOptions,
) -> str:
    """Format conversations as markdown with full exchanges."""
    sections: list[str] = []

    for conv in conversations:
        lines: list[str] = []

        if not options.no_header:
            lines.append(f"# Session {conv.id[:12]}")
            meta_parts = []
            ws = fmt_workspace(conv.workspace_path)
            if ws:
                meta_parts.append(ws)
            ts = fmt_timestamp(conv.started_at)
            if ts:
                meta_parts.append(ts)
            model = fmt_model(conv.model)
            if model:
                meta_parts.append(model)
            if conv.total_tokens:
                meta_parts.append(fmt_tokens(conv.total_tokens) + " tokens")
            if conv.tags:
                meta_parts.append("tags: " + ", ".join(conv.tags))
            if meta_parts:
                lines.append(f"*{' · '.join(meta_parts)}*")
            lines.append("")

        for turn in conv.turns:
            ts = fmt_timestamp(turn.timestamp, time_only=True)
            ts_prefix = f"{ts} — " if ts else ""

            # User turn
            if turn.prompt_text:
                lines.append(f"### {ts_prefix}User")
                lines.append("")
                prompt = turn.prompt_text.strip()
                if options.brief and len(prompt) > 300:
                    prompt = prompt[:300] + "..."
                lines.append(prompt)
                lines.append("")

            # Assistant turn
            if turn.narrative:
                lines.append(f"### {ts_prefix}Assistant")
                lines.append("")
                narrative_lines = _render_narrative_md(
                    turn.narrative,
                    include_thinking=options.include_thinking,
                    include_tools=options.include_tools,
                    brief=options.brief,
                )
                lines.extend(narrative_lines)

            lines.append("---")
            lines.append("")

        sections.append("\n".join(lines))

    return "\n".join(sections).rstrip()


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


def _narrative_to_json(
    narrative: list[NarrativeBlock],
    *,
    include_thinking: bool = False,
    include_tools: bool = False,
) -> list[dict]:
    """Serialize narrative blocks to JSON-ready dicts."""
    blocks = []
    for block in narrative:
        d: dict = {"type": block.block_type}

        if block.block_type == "text":
            d["content"] = block.content

        elif block.block_type == "thinking":
            if include_thinking and block.content:
                d["content"] = block.content
            # Always include the block so consumer knows thinking occurred

        elif block.block_type == "tool_calls":
            d["tools"] = [
                {
                    "name": tc.tool_name,
                    "status": tc.status,
                    "count": tc.count,
                    **({"input": tc.input} if include_tools and tc.input else {}),
                    **({"result": tc.result} if include_tools and tc.result else {}),
                }
                for tc in block.tool_calls
            ]

        elif block.block_type in ("tool_result", "tool_output"):
            if include_tools and block.content:
                d["content"] = block.content
            else:
                continue  # skip tool output blocks when not requested

        blocks.append(d)

    return blocks


def format_json(
    conversations: list[ExportedConversation],
    options: ExportOptions,
) -> str:
    """Format conversations as structured JSON."""
    output = []

    for conv in conversations:
        turns_data = []
        for turn in conv.turns:
            turn_data: dict = {
                "timestamp": turn.timestamp,
                "prompt": turn.prompt_text,
                "narrative": _narrative_to_json(
                    turn.narrative,
                    include_thinking=options.include_thinking,
                    include_tools=options.include_tools,
                ),
                "tokens": {
                    "input": turn.total_input_tokens,
                    "output": turn.total_output_tokens,
                },
            }
            turns_data.append(turn_data)

        conv_data = {
            "id": conv.id,
            "workspace": conv.workspace_path,
            "model": conv.model,
            "started_at": conv.started_at,
            "turns": turns_data,
            "tags": conv.tags,
            "total_tokens": conv.total_tokens,
        }
        output.append(conv_data)

    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def format_export(
    conversations: list[ExportedConversation],
    options: ExportOptions,
) -> str:
    """Format conversations according to export options."""
    if options.json_mode:
        return format_json(conversations, options)
    return format_markdown(conversations, options)
