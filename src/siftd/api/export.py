"""Export API for siftd.

Fetches and prepares conversations for export.

export_conversations: returns raw ExportedConversation objects for programmatic use.
export_document: produces a complete serialized artifact (markdown or JSON document).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.api.conversations import (
    ConversationDetail,
    Turn,
    get_conversation,
    list_conversations,
)
from siftd.output._id_format import short_id

if TYPE_CHECKING:
    from painted import Fidelity


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
    id: list[str] | None = None,
    last: int | None = None,
    n: int = 0,
    workspace: str | None = None,
    tag: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    since: str | None = None,
    before: str | None = None,
    search: str | None = None,
    db_path: Path | None = None,
    include_thinking: bool = True,
    include_tool_content: bool = False,
    owner: str | None = None,
) -> list[ExportedConversation]:
    """Export conversations matching the specified criteria.

    Always fetches with include_thinking=True so thinking block presence
    is known (for placeholder rendering). Tool content is fetched only
    when include_tool_content=True (for --tools/--full).
    """
    if id:
        results = []
        for cid in id:
            detail = get_conversation(
                cid,
                db_path=db_path,
                include_thinking=include_thinking,
                include_tool_content=include_tool_content,
                owner=owner,
            )
            if detail:
                results.append(_detail_to_export(detail))
        return results

    n = last if last else (n if n > 0 else 10)
    summaries = list_conversations(
        db_path=db_path,
        workspace=workspace,
        tag=tag,
        no_tag=no_tag,
        tag_kind=tag_kind,
        since=since,
        before=before,
        search=search,
        n=n,
        owner=owner,
    )

    results = []
    for summary in summaries:
        detail = get_conversation(
            summary.id,
            db_path=db_path,
            include_thinking=include_thinking,
            include_tool_content=include_tool_content,
            owner=owner,
        )
        if detail:
            results.append(_detail_to_export(detail))

    if n > 0:
        results = results[:n]
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


@dataclass
class ExportArtifact:
    """A complete, serialized export document ready to serve or write."""

    content: str
    media_type: str
    filename: str
    count: int


def export_document(
    *,
    format: str = "md",
    fidelity: Fidelity | None = None,
    no_header: bool = False,
    id: list[str] | None = None,
    last: int | None = None,
    n: int = 0,
    workspace: str | None = None,
    tag: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    since: str | None = None,
    before: str | None = None,
    search: str | None = None,
    db_path: Path | None = None,
    include_thinking: bool = True,
    include_tool_content: bool = False,
    owner: str | None = None,
) -> ExportArtifact:
    """Export conversations as a complete document.

    Selects conversations, serializes them via the appropriate formatter,
    and returns a ready-to-serve artifact. The format choice is an export
    parameter, not a render-time decision.

    Args:
        format: Output format — "md" (markdown) or "json".
        fidelity: Rendering fidelity. Defaults to full (show everything).
        no_header: Omit per-conversation metadata headers.
        last: Export N most recent conversations (takes precedence over n).
        n: Max conversations when neither id nor last is given. Passed through
            to export_conversations where it defaults to 10 when 0. CLI callers
            use ``last`` instead; ``n`` exists for programmatic use.
        Other args: passed through to export_conversations.

    Returns:
        ExportArtifact with serialized content, media_type, and filename.
    """
    if fidelity is None:
        from painted import Fidelity as F

        fidelity = F(visible={"text", "thinking", "tools"}, depth=3, chars=0)

    conversations = export_conversations(
        id=id, last=last, n=n, workspace=workspace, tag=tag,
        no_tag=no_tag, tag_kind=tag_kind, since=since, before=before,
        search=search, db_path=db_path, include_thinking=include_thinking,
        include_tool_content=include_tool_content, owner=owner,
    )

    if format == "json":
        import json

        from siftd.output import json_fmt

        sections = [
            json_fmt.render_detail(conv, fidelity, no_header=no_header)
            for conv in conversations
        ]
        content = json.dumps(sections, indent=2)
        media_type = "application/json"
        ext = "json"
    else:
        from siftd.output import markdown_fmt

        sections = [
            markdown_fmt.render_detail(conv, fidelity, no_header=no_header)
            for conv in conversations
        ]
        content = "\n\n---\n\n".join(sections) if len(sections) > 1 else (sections[0] if sections else "")
        media_type = "text/markdown"
        ext = "md"

    # Build a descriptive filename
    if conversations and len(conversations) == 1:
        slug = short_id(conversations[0].id)
        filename = f"siftd-{slug}.{ext}"
    else:
        filename = f"siftd-export-{len(conversations)}.{ext}"

    return ExportArtifact(
        content=content,
        media_type=media_type,
        filename=filename,
        count=len(conversations),
    )
