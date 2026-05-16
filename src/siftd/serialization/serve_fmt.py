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


def render_stats(stats: Any, fidelity: Fidelity) -> dict:
    """Serialize DatabaseStats to JSON-safe dict."""
    from siftd.serialization.stats import serialize_stats

    return serialize_stats(stats)


def render_workspaces(rows: list, fidelity: Fidelity) -> dict:
    """Serialize workspace rows to JSON-safe dict."""
    return {
        "workspaces": [
            {"path": r["path"], "conversations": r["convs"], "last_activity": r["last_activity"]}
            for r in rows
        ]
    }


def render_tags(tags: list, fidelity: Fidelity) -> dict:
    """Serialize TagInfo list to JSON-safe dict."""
    return {
        "tags": [
            t if isinstance(t, dict) else dataclasses.asdict(t)
            for t in tags
        ]
    }


def render_search(results: list, fidelity: Fidelity, debug_ids: bool = True) -> dict:
    """Serialize SearchResult list to JSON-safe dict.

    chunk_id and source_ids are emitted by default. The debug_ids kwarg is
    accepted for backward compatibility through v0.9.x and removed in v0.10.0.
    """
    del debug_ids
    serialized = []
    for r in results:
        if isinstance(r, dict):
            serialized.append(r)
        else:
            serialized.append(dataclasses.asdict(r))
    return {
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
