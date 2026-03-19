"""Export API for siftd.

Renders full conversation exchanges as markdown or JSON.
Narrative rendering is delegated to the shared walker in output.narrative.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from siftd.api.conversations import (
    ConversationDetail,
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
# Narrative rendering (delegated to shared walker)
# ---------------------------------------------------------------------------


def _options_to_fidelity(options: ExportOptions):
    """Convert ExportOptions to a Fidelity spec for the narrative walker."""
    from painted import Fidelity

    visible: set[str] = {"text"}
    if options.include_thinking:
        visible.add("thinking")
    if options.include_tools:
        visible.add("tools")

    chars = 300 if options.brief else 0

    return Fidelity(
        depth=3 if options.include_tools else 1,
        visible=frozenset(visible),
        chars=chars,
    )


def _render_narrative_md(narrative: list, options: ExportOptions) -> list[str]:
    """Render narrative blocks to markdown via the shared walker."""
    from siftd.output.narrative import MarkdownEmitter, walk_narrative

    fidelity = _options_to_fidelity(options)
    emitter = MarkdownEmitter()
    walk_narrative(narrative, emitter, fidelity=fidelity)
    return emitter.lines


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
                lines.extend(_render_narrative_md(turn.narrative, options))

            lines.append("---")
            lines.append("")

        sections.append("\n".join(lines))

    return "\n".join(sections).rstrip()


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


def _narrative_to_json(narrative: list, options: ExportOptions) -> list[dict]:
    """Serialize narrative blocks to JSON via the shared walker."""
    from siftd.output.narrative import JsonEmitter, walk_narrative

    fidelity = _options_to_fidelity(options)
    emitter = JsonEmitter()
    walk_narrative(narrative, emitter, fidelity=fidelity)
    return emitter.blocks


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
                "narrative": _narrative_to_json(turn.narrative, options),
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
