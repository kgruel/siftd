"""Export API for siftd.

Provides export functionality for PR review workflows.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from siftd.api.conversations import (
    ConversationDetail,
    Exchange,
    get_conversation,
    list_conversations,
)


@dataclass
class ExportOptions:
    """Options controlling export output."""

    format: str = "prompts"  # prompts, exchanges, json
    prompts_only: bool = False  # omit response text and tool calls
    no_header: bool = False  # omit session metadata header


@dataclass
class ExportedConversation:
    """A conversation prepared for export."""

    id: str
    workspace_path: str | None
    workspace_name: str | None
    model: str | None
    started_at: str | None
    exchanges: list[Exchange]
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
) -> list[ExportedConversation]:
    """Export conversations matching the specified criteria.

    Args:
        conversation_ids: Specific conversation IDs to export (prefix match).
        last: Export the N most recent conversations.
        workspace: Filter by workspace path substring.
        tags: Include only conversations with any of these tags.
        exclude_tags: Exclude conversations with any of these tags.
        since: Conversations started after this date.
        before: Conversations started before this date.
        search: FTS5 full-text search filter.
        db_path: Path to database. Uses default if not specified.

    Returns:
        List of ExportedConversation objects with full exchange data.

    Raises:
        FileNotFoundError: If database does not exist.
        ValueError: If no conversations match criteria.
    """
    # If specific IDs provided, fetch each directly
    if conversation_ids:
        results = []
        for cid in conversation_ids:
            detail = get_conversation(cid, db_path=db_path)
            if detail:
                results.append(_detail_to_export(detail))
        return results

    # Otherwise, use list_conversations to find matching sessions
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

    # Fetch full details for each
    results = []
    for summary in summaries:
        detail = get_conversation(summary.id, db_path=db_path)
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
        exchanges=detail.exchanges,
        tags=detail.tags,
        total_tokens=detail.total_input_tokens + detail.total_output_tokens,
    )


def format_prompts(
    conversations: list[ExportedConversation],
    *,
    no_header: bool = False,
) -> str:
    """Format conversations as prompts-only markdown.

    Args:
        conversations: List of exported conversations.
        no_header: If True, omit session metadata header.

    Returns:
        Markdown string with numbered prompts.
    """
    lines = []

    for conv in conversations:
        if not no_header:
            # Session header
            header_parts = []
            if conv.workspace_name:
                header_parts.append(conv.workspace_name)
            if conv.started_at:
                # Format: 2026-01-28 10:30
                date_str = conv.started_at[:16].replace("T", " ")
                header_parts.append(date_str)

            lines.append(f"## Session {conv.id[:12]}")
            if header_parts:
                lines.append(f"*{' · '.join(header_parts)}*")
            lines.append("")

        # Extract prompts
        prompt_num = 0
        for ex in conv.exchanges:
            if ex.prompt_text:
                prompt_num += 1
                lines.append(f"{prompt_num}. {ex.prompt_text}")
                lines.append("")

        if not no_header and len(conversations) > 1:
            lines.append("---")
            lines.append("")

    return "\n".join(lines).rstrip()


def format_exchanges(
    conversations: list[ExportedConversation],
    *,
    prompts_only: bool = False,
    no_header: bool = False,
) -> str:
    """Format conversations as prompt-response exchanges.

    Args:
        conversations: List of exported conversations.
        prompts_only: If True, omit response text and tool calls.
        no_header: If True, omit session metadata header.

    Returns:
        Markdown string with exchanges.
    """
    lines = []

    for conv in conversations:
        if not no_header:
            header_parts = []
            if conv.workspace_name:
                header_parts.append(conv.workspace_name)
            if conv.started_at:
                date_str = conv.started_at[:16].replace("T", " ")
                header_parts.append(date_str)

            lines.append(f"## Session {conv.id[:12]}")
            if header_parts:
                lines.append(f"*{' · '.join(header_parts)}*")
            lines.append("")

        exchange_num = 0
        for ex in conv.exchanges:
            if ex.prompt_text:
                exchange_num += 1
                lines.append(f"### Exchange {exchange_num}")
                lines.append(f"**User:** {ex.prompt_text}")
                lines.append("")

                if not prompts_only and ex.response_text:
                    # Truncate long responses
                    response = ex.response_text
                    if len(response) > 500:
                        response = response[:500] + "..."
                    lines.append(f"**Assistant:** {response}")
                    lines.append("")

                    # Include tool call summary
                    if ex.tool_calls:
                        tool_parts = []
                        for tc in ex.tool_calls:
                            if tc.count > 1:
                                tool_parts.append(f"{tc.tool_name} ×{tc.count}")
                            else:
                                tool_parts.append(tc.tool_name)
                        lines.append(f"*Tools: {', '.join(tool_parts)}*")
                        lines.append("")

                lines.append("---")
                lines.append("")

    return "\n".join(lines).rstrip()


def format_json(
    conversations: list[ExportedConversation],
    *,
    prompts_only: bool = False,
) -> str:
    """Format conversations as JSON.

    Args:
        conversations: List of exported conversations.
        prompts_only: If True, omit response text and tool calls.

    Returns:
        JSON string with structured conversation data.
    """
    output = []

    for conv in conversations:
        exchanges_data = []
        for ex in conv.exchanges:
            ex_data: dict = {
                "timestamp": ex.timestamp,
                "prompt": ex.prompt_text,
            }

            if not prompts_only:
                ex_data["response"] = ex.response_text
                ex_data["tool_calls"] = [
                    {
                        "tool": tc.tool_name,
                        "status": tc.status,
                        "count": tc.count,
                    }
                    for tc in ex.tool_calls
                ]
                ex_data["tokens"] = {
                    "input": ex.input_tokens,
                    "output": ex.output_tokens,
                }

            exchanges_data.append(ex_data)

        conv_data = {
            "id": conv.id,
            "workspace": conv.workspace_path,
            "workspace_name": conv.workspace_name,
            "model": conv.model,
            "started_at": conv.started_at,
            "exchanges": exchanges_data,
            "tags": conv.tags,
            "total_tokens": conv.total_tokens,
        }
        output.append(conv_data)

    return json.dumps(output, indent=2)


def format_export(
    conversations: list[ExportedConversation],
    options: ExportOptions,
) -> str:
    """Format conversations according to export options.

    Args:
        conversations: List of exported conversations.
        options: Export formatting options.

    Returns:
        Formatted string (markdown or JSON).
    """
    if options.format == "json":
        return format_json(conversations, prompts_only=options.prompts_only)
    elif options.format == "exchanges":
        return format_exchanges(
            conversations,
            prompts_only=options.prompts_only,
            no_header=options.no_header,
        )
    else:  # prompts (default)
        return format_prompts(conversations, no_header=options.no_header)
