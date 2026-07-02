"""Export API for siftd.

Fetches and prepares conversations for export.

export_conversations: returns raw ExportedConversation objects for programmatic use.
export_document: produces a complete serialized artifact (markdown or JSON document).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from painted import Fidelity

from siftd.api.conversations import (
    ConversationDetail,
    Turn,
    get_conversation,
    list_conversations,
)
from siftd.output._id_format import short_id
from siftd.storage.filters import EVENT_TAG_KINDS


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
    fidelity: Fidelity,
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
    owner: str | None = None,
) -> list[ExportedConversation]:
    """Export conversations matching the specified criteria.

    The fetch fidelity always carries "thinking" in its visible set so the
    renderer can emit ``*[thinking]*`` placeholders even when the caller's
    fidelity omits it; expanded vs. placeholder is then a render-time
    decision against the caller-supplied fidelity.
    """
    from dataclasses import replace

    fetch_fidelity = replace(
        fidelity, visible=fidelity.visible | frozenset({"thinking"}),
    )

    if id:
        results = []
        for cid in id:
            detail = get_conversation(
                cid,
                fidelity=fetch_fidelity,
                db_path=db_path,
                owner=owner,
            )
            if detail:
                results.append(_detail_to_export(detail))
        return results

    n = last if last else (n if n > 0 else 10)
    summaries = list_conversations(
        fidelity=fidelity,
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
            fidelity=fetch_fidelity,
            db_path=db_path,
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
class ExportedElement:
    """A single tagged element prepared for export (WS6)."""

    event_id: str
    kind: str  # prompt | response | tool_call | exchange | block
    conversation_id: str
    workspace_path: str | None
    timestamp: str | None
    alias: str  # colon-path <conv>:<kind>:<n>[:<b>]
    tags: list[str]
    text: str
    response_text: str | None = None  # only for exchange: the anchored response(s)
    block_type: str | None = None  # only for block: the content-block flavor


# Event-anchored kinds exported by the event arm; 'block' rides its own arm
# (its target is an event_content.id, not an events.id).
_EXPORT_ELEMENT_KINDS = tuple(sorted(EVENT_TAG_KINDS))


def _element_text(conn, event_id: str) -> str:
    """Concatenate an event's text blocks (decoded from the JSON content)."""
    rows = conn.execute(
        "SELECT json_extract(content, '$.text') AS text FROM event_content "
        "WHERE event_id = ? AND json_extract(content, '$.text') IS NOT NULL "
        "ORDER BY block_index",
        (event_id,),
    ).fetchall()
    return "\n".join(r["text"] for r in rows if r["text"]).strip()


def _block_text(conn, block_id: str) -> str:
    """The single content block's own text (decoded from its JSON content)."""
    row = conn.execute(
        "SELECT json_extract(content, '$.text') AS text FROM event_content WHERE id = ?",
        (block_id,),
    ).fetchone()
    return (row["text"] or "").strip() if row else ""


def export_elements(
    *,
    tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    workspace: str | None = None,
    since: str | None = None,
    before: str | None = None,
    owner: str | None = None,
    db_path: Path | None = None,
) -> list[ExportedElement]:
    """Select the tagged elements matching the filters (WS6, decision 4).

    Emits exactly the tagged targets — no surrounding-context knob. An
    ``exchange`` target carries its anchored prompt and response(s); other kinds
    emit only their own content. Recency-ordered (element timestamp desc).
    """
    from siftd.api.target_ref import alias as target_alias
    from siftd.paths import db_path as _default_db_path
    from siftd.storage.filters import tag_condition
    from siftd.storage.queries import fetch_prompt_response_texts
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate
    from siftd.storage.sqlite import open_database

    kinds = tuple(k for k in (tag_kind or _EXPORT_ELEMENT_KINDS) if k in _EXPORT_ELEMENT_KINDS)
    want_block = (tag_kind is None or "block" in tag_kind)
    if not tag or (not kinds and not want_block):
        return []

    db = db_path or _default_db_path()
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    def _tag_facet_clause(params: list[object]) -> str:
        ors: list[str] = []
        for t in tag:
            clause, val = tag_condition(t)
            ors.append(f"({clause})")
            params.append(val)
        return (
            "ta.target_id IN (SELECT ta2.target_id FROM tag_assignments ta2 "
            "JOIN tags tg ON tg.id = ta2.tag_id "
            f"WHERE {' OR '.join(ors)})"
        )

    conn = open_database(db, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return []

        elements: list[ExportedElement] = []

        # --- event-kind elements (prompt/response/tool_call/exchange) ---
        if kinds:
            where: list[str] = [f"ta.target_kind IN ({','.join('?' * len(kinds))})"]
            params: list[object] = list(kinds)
            where.append(_tag_facet_clause(params))
            if workspace:
                where.append("w.path LIKE ?")
                params.append(f"%{workspace}%")
            if since:
                where.append("e.timestamp >= ?")
                params.append(since)
            if before:
                where.append("e.timestamp < ?")
                params.append(before)
            if owner:
                where.append(owner_predicate("c.id"))
                params.append(owner)

            rows = conn.execute(
                "SELECT DISTINCT ta.target_kind, ta.target_id, e.conversation_id, "
                "e.timestamp AS ev_ts, w.path AS workspace "
                "FROM tag_assignments ta "
                "JOIN events e ON e.id = ta.target_id "
                "JOIN conversations c ON c.id = e.conversation_id "
                "LEFT JOIN workspaces w ON w.id = c.workspace_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY e.timestamp DESC, e.id DESC",
                params,
            ).fetchall()
            for row in rows:
                kind = row["target_kind"]
                eid = row["target_id"]
                tag_rows = conn.execute(
                    "SELECT tg.name FROM tag_assignments ta JOIN tags tg ON tg.id = ta.tag_id "
                    "WHERE ta.target_id = ? AND ta.target_kind = ? ORDER BY tg.name",
                    (eid, kind),
                ).fetchall()
                response_text = None
                if kind == "exchange":
                    pairs = fetch_prompt_response_texts(conn, [eid])
                    response_text = pairs[0][2] if pairs else None
                elements.append(
                    ExportedElement(
                        event_id=eid,
                        kind=kind,
                        conversation_id=row["conversation_id"],
                        workspace_path=row["workspace"],
                        timestamp=row["ev_ts"],
                        alias=target_alias(conn, kind, eid),
                        tags=[r["name"] for r in tag_rows],
                        text=_element_text(conn, eid),
                        response_text=response_text,
                    )
                )

        # --- block-kind elements (event_content) ---
        # target_id is an event_content.id, so the join descends
        # event_content → events → conversations (distinct from the event query).
        if want_block:
            where = ["ta.target_kind = 'block'"]
            params = []
            where.append(_tag_facet_clause(params))
            if workspace:
                where.append("w.path LIKE ?")
                params.append(f"%{workspace}%")
            if since:
                where.append("e.timestamp >= ?")
                params.append(since)
            if before:
                where.append("e.timestamp < ?")
                params.append(before)
            if owner:
                where.append(owner_predicate("c.id"))
                params.append(owner)

            rows = conn.execute(
                "SELECT DISTINCT ta.target_id AS block_id, ec.block_type, "
                "e.id AS event_id, e.conversation_id, e.timestamp AS ev_ts, "
                "w.path AS workspace "
                "FROM tag_assignments ta "
                "JOIN event_content ec ON ec.id = ta.target_id "
                "JOIN events e ON e.id = ec.event_id "
                "JOIN conversations c ON c.id = e.conversation_id "
                "LEFT JOIN workspaces w ON w.id = c.workspace_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY e.timestamp DESC, ec.id DESC",
                params,
            ).fetchall()
            for row in rows:
                bid = row["block_id"]
                tag_rows = conn.execute(
                    "SELECT tg.name FROM tag_assignments ta JOIN tags tg ON tg.id = ta.tag_id "
                    "WHERE ta.target_id = ? AND ta.target_kind = 'block' ORDER BY tg.name",
                    (bid,),
                ).fetchall()
                elements.append(
                    ExportedElement(
                        event_id=row["event_id"],
                        kind="block",
                        conversation_id=row["conversation_id"],
                        workspace_path=row["workspace"],
                        timestamp=row["ev_ts"],
                        alias=target_alias(conn, "block", bid),
                        tags=[r["name"] for r in tag_rows],
                        text=_block_text(conn, bid),
                        block_type=row["block_type"],
                    )
                )

        # Merge the two arms, recency-ordered (element timestamp desc).
        elements.sort(key=lambda el: (el.timestamp or ""), reverse=True)
        return elements
    finally:
        conn.close()


@dataclass
class ExportArtifact:
    """A complete, serialized export document ready to serve or write."""

    content: str
    media_type: str
    filename: str
    count: int


def export_document(
    *,
    fidelity: Fidelity,
    format: str = "md",
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
    view: str = "conversations",
    db_path: Path | None = None,
    owner: str | None = None,
) -> ExportArtifact:
    """Export conversations as a complete document.

    Selects conversations, serializes them via the appropriate formatter,
    and returns a ready-to-serve artifact. The format choice is an export
    parameter, not a render-time decision.

    Args:
        fidelity: Cross-stage rendering contract. Drives both fetch (via
            ``shows("tools")``) and render (placeholder vs. expanded
            thinking/tool blocks). Thinking blocks are always fetched so
            placeholders can render — see ``export_conversations``.
        format: Output format — "md" (markdown) or "json".
        no_header: Omit per-conversation metadata headers.
        last: Export N most recent conversations (takes precedence over n).
        n: Max conversations when neither id nor last is given. Passed through
            to export_conversations where it defaults to 10 when 0. CLI callers
            use ``last`` instead; ``n`` exists for programmatic use.
        Other args: passed through to export_conversations.

    Returns:
        ExportArtifact with serialized content, media_type, and filename.
    """
    if view == "elements":
        if not tag:
            raise ValueError("elements view requires --tag")
        return _export_elements_document(
            fidelity=fidelity, format=format, tag=tag, tag_kind=tag_kind,
            workspace=workspace, since=since, before=before,
            db_path=db_path, owner=owner,
        )

    conversations = export_conversations(
        fidelity=fidelity,
        id=id, last=last, n=n, workspace=workspace, tag=tag,
        no_tag=no_tag, tag_kind=tag_kind, since=since, before=before,
        search=search, db_path=db_path, owner=owner,
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


def _export_elements_document(
    *,
    fidelity: Fidelity,
    format: str,
    tag: list[str] | None,
    tag_kind: list[str] | None,
    workspace: str | None,
    since: str | None,
    before: str | None,
    db_path: Path | None,
    owner: str | None,
) -> ExportArtifact:
    """Serialize the tagged elements as a document (WS6). md + json."""
    elements = export_elements(
        tag=tag, tag_kind=tag_kind, workspace=workspace,
        since=since, before=before, db_path=db_path, owner=owner,
    )

    if format == "json":
        import json

        payload = [
            {
                "event_id": el.event_id,
                "kind": el.kind,
                "conversation_id": el.conversation_id,
                "workspace": el.workspace_path,
                "timestamp": el.timestamp,
                "alias": el.alias,
                "tags": el.tags,
                "text": el.text,
                **({"response_text": el.response_text} if el.response_text is not None else {}),
                **({"block_type": el.block_type} if el.block_type is not None else {}),
            }
            for el in elements
        ]
        content = json.dumps(payload, indent=2)
        media_type = "application/json"
        ext = "json"
    else:
        sections: list[str] = []
        for el in elements:
            kind_label = f"{el.kind} ({el.block_type})" if el.block_type else el.kind
            head = (
                f"### {kind_label} · {el.alias}\n\n"
                f"- conversation: {short_id(el.conversation_id)}\n"
                f"- workspace: {el.workspace_path or '—'}\n"
                f"- timestamp: {el.timestamp or '—'}\n"
                f"- tags: {', '.join(el.tags) if el.tags else '—'}\n"
            )
            body = el.text
            if el.kind == "exchange" and el.response_text:
                body = f"{el.text}\n\n**Response:**\n\n{el.response_text}"
            sections.append(f"{head}\n{body}".rstrip())
        content = "\n\n---\n\n".join(sections)
        media_type = "text/markdown"
        ext = "md"

    return ExportArtifact(
        content=content,
        media_type=media_type,
        filename=f"siftd-elements-{len(elements)}.{ext}",
        count=len(elements),
    )
