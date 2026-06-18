"""Serve format — JSON serialization for serve route dispatch.

This module is the serve-side equivalent of output/json_fmt.py.
It lives in serialization/ so serve can import it without violating
the architecture boundary (serve cannot import output).

Render methods match the format protocol interface: render_{name}(result, fidelity).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity


def serialize_caveats(findings: list[Any]) -> list[dict]:
    """Serialize caveat Findings to JSON-ready dicts for the wire envelope.

    Mirrors the shape output/json_fmt emits (``asdict(finding)``) so the HTTP
    and CLI ``--json`` caveat envelopes agree. Lives here, not in serve/, so
    the serve layer never reaches for dataclasses.asdict directly.
    """
    return [f if isinstance(f, dict) else dataclasses.asdict(f) for f in findings]


def render_stats(stats: Any, fidelity: Fidelity) -> dict:
    """Serialize DatabaseStats to JSON-safe dict."""
    from siftd.serialization.stats import serialize_stats

    return serialize_stats(stats)


def render_workspaces(rows: list, fidelity: Fidelity) -> dict:
    """Serialize workspace rows to JSON-safe dict."""
    return {
        "workspaces": [
            {
                "id": r["id"],
                "path": r["path"],
                "git_remote": r["git_remote"],
                "conversations": r["convs"],
                "last_activity": r["last_activity"],
            }
            for r in rows
        ]
    }


def render_workspace_detail(detail: Any, fidelity: Fidelity) -> dict:
    """Serialize a WorkspaceDetail to a JSON-safe dict.

    The workspace is identified by its ULID ``id``; ``model_mix`` is the
    by-model breakdown within the workspace (GroupUsage rows) and ``recent``
    reuses the conversation-list serializer.
    """
    from siftd.serialization.conversations import serialize_conversation_list

    return {
        "id": detail.id,
        "path": detail.path,
        "git_remote": detail.git_remote,
        "sessions": detail.sessions,
        "input_tokens": detail.input_tokens,
        "output_tokens": detail.output_tokens,
        "cost": detail.cost,
        "model_mix": [dataclasses.asdict(g) for g in detail.model_mix],
        "recent": serialize_conversation_list(detail.recent),
    }


def render_tags(tags: list, fidelity: Fidelity) -> dict:
    """Serialize TagInfo list to JSON-safe dict."""
    return {
        "tags": [
            t if isinstance(t, dict) else dataclasses.asdict(t)
            for t in tags
        ]
    }


def render_search(results: list, fidelity: Fidelity, **context: Any) -> dict:
    """Serialize SearchResult list to JSON-safe dict (the serve/REST envelope).

    context keys:
        mode: str — resolved search engine that ran ("fts"/"semantic"/"hybrid");
            lets REST callers tell which engine produced the results.
        view: str — render shape (always "chunks" on the serve search route).
    chunk_id and source_ids are emitted by default; any ``debug_ids`` in context
    is accepted and ignored.
    """
    serialized = []
    for r in results:
        if isinstance(r, dict):
            serialized.append(r)
        else:
            serialized.append(dataclasses.asdict(r))
    return {
        "mode": context.get("mode"),
        "view": context.get("view", "chunks"),
        "result_count": len(serialized),
        "results": serialized,
    }


def render_export(conversations: list, fidelity: Fidelity) -> dict:
    """Serialize exported conversations to JSON-safe dict.

    Legacy export shape — returns the list of conversation dicts. Used by the
    pre-Phase-C export route which dispatched through ``export_conversations``.
    """
    from siftd.serialization.conversations import serialize_conversation_detail

    return {
        "conversations": [serialize_conversation_detail(c) for c in conversations],
    }


def render_export_artifact(artifact: Any, fidelity: Fidelity) -> dict:
    """Serialize an ``ExportArtifact`` to JSON-safe dict.

    Used by the format-aware export route which dispatches through
    ``export_document`` and returns a fully-rendered artifact (markdown or
    JSON content already in ``.content``). The CLI delegation client
    reconstructs the dataclass via
    :func:`siftd.api.deserialize.deserialize_export_artifact`.
    """
    return {
        "content": artifact.content,
        "media_type": artifact.media_type,
        "filename": artifact.filename,
        "count": artifact.count,
    }


def render_detail(detail: Any, fidelity: Fidelity) -> dict:
    """Serialize ConversationDetail to JSON-safe dict."""
    from siftd.serialization.conversations import serialize_conversation_detail

    if detail is None:
        return {"error": "conversation not found"}
    return {"conversation": serialize_conversation_detail(detail)}


def render_list(summaries: list, fidelity: Fidelity) -> dict:
    """Serialize conversation list to JSON-safe dict."""
    from siftd.serialization.conversations import serialize_conversation_list

    return {"conversations": serialize_conversation_list(summaries)}
