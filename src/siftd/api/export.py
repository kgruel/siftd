"""Export API for siftd.

Fetches and prepares conversations for export. Rendering is handled by
the output format system (markdown_fmt, json_fmt, etc.).
"""

from dataclasses import dataclass
from pathlib import Path

from siftd.api.conversations import (
    ConversationDetail,
    Turn,
    get_conversation,
    list_conversations,
)


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
