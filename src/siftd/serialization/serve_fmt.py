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
        "cadence": [dataclasses.asdict(b) for b in detail.cadence],
        "tags": [{"name": name, "count": n} for name, n in detail.tags],
    }


def render_tags(tags: list, fidelity: Fidelity) -> dict:
    """Serialize TagInfo list to JSON-safe dict."""
    return {
        "tags": [
            t if isinstance(t, dict) else dataclasses.asdict(t)
            for t in tags
        ]
    }


def _wire_ref(ref: Any) -> dict:
    """Serialize one file reference to wire shape (metadata, no content).

    Mirrors ``output/json_fmt._json_chunk_list``: ``content`` is deliberately
    omitted (the CLI's ``--refs`` content dump runs against the local DB, so
    file content never rides the wire to a REST consumer). A dict passes through
    unchanged (already wire-shaped).
    """
    if isinstance(ref, dict):
        return ref
    return {
        "basename": ref.basename,
        "path": ref.path,
        "op": ref.op,
        "content_length": len(ref.content) if ref.content else 0,
    }


def _wire_chunk(r: dict) -> dict:
    """Serialize one chunk render-dict to the wire chunk shape.

    The public shape mirrors ``output/json_fmt`` (``conversation`` sub-object,
    rounded score), and additionally carries the fields the deserializer needs
    to reconstruct a render-identical chunk: ``display_label`` (recomputable but
    cheaper to carry), and ``exchanges``/``context`` for the thread and
    ``--full``/``--around`` views (json_fmt drops them since ``--json`` never
    shows them; the wire keeps them so the delegated CLI renders identically).
    """
    from siftd.domain.search_types import ScoreBreakdown

    chunk: dict[str, Any] = {
        "conversation_id": r.get("conversation_id"),
        "score": round(r.get("score", 0.0), 4),
        "chunk_type": r.get("chunk_type", ""),
        "display_label": r.get("display_label", ""),
        "text": r.get("text", ""),
        "conversation": {
            "started_at": r.get("_started_at"),
            "workspace": r.get("_workspace"),
        },
        "chunk_id": r.get("chunk_id"),
        "source_ids": r.get("source_ids", []),
        "turn_index": r.get("turn_index"),
    }
    if r.get("event_id") is not None:
        chunk["event_id"] = r.get("event_id")

    breakdown = r.get("breakdown")
    if isinstance(breakdown, ScoreBreakdown):
        chunk["breakdown"] = breakdown.to_dict()

    file_refs = r.get("file_refs")
    if file_refs:
        chunk["file_refs"] = [_wire_ref(ref) for ref in file_refs]

    exchanges = r.get("_exchanges")
    if exchanges:
        chunk["exchanges"] = [list(ex) for ex in exchanges]

    context_window = r.get("_context")
    if context_window:
        chunk["context"] = [list(c) for c in context_window]

    return chunk


def _wire_conv(r: dict) -> dict:
    """Serialize one conversations-view aggregate render-dict to wire shape."""
    return {
        "conversation_id": r.get("conversation_id"),
        "max_score": round(r.get("max_score", 0.0), 4),
        "mean_score": round(r.get("mean_score", 0.0), 4),
        "chunk_count": r.get("chunk_count", 0),
        "started_at": r.get("_started_at"),
        "workspace": r.get("_workspace"),
        "best_excerpt": r.get("best_excerpt", ""),
    }


def render_search(result: Any, fidelity: Fidelity, **context: Any) -> dict:
    """Serialize a :class:`SearchView` to the serve/REST envelope.

    Branches on the view shape (mirroring ``output/json_fmt`` so a REST consumer
    sees the same repertoire ``siftd search --json`` does), and carries
    ``tier1``/``tier2`` (thread), ``n_skipped`` (``--around`` filter) and
    ``empty_reason`` so the delegated CLI reconstructs a render-identical
    ``SearchView`` via :func:`siftd.api.deserialize.deserialize_search_view`.

    context keys:
        mode: str — resolved engine that ran ("fts"/"semantic"/"hybrid").
        view: str — fallback shape when a bare list is passed (the SearchView's
            own ``view`` is authoritative).
    chunk_id/source_ids are emitted by default; any ``debug_ids`` is ignored.
    """
    from siftd.domain.search_types import as_search_view

    sv = as_search_view(result, view=context.get("view", "chunks"))
    out: dict[str, Any] = {
        "mode": context.get("mode"),
        "view": sv.view,
        "n_skipped": sv.n_skipped,
        "empty_reason": sv.empty_reason,
    }

    if sv.view == "conversations":
        out["result_count"] = len(sv.results)
        out["results"] = [_wire_conv(r) for r in sv.results]
        return out

    if sv.view == "thread":
        tier1 = sv.tier1 or []
        tier2 = sv.tier2 or []
        out["result_count"] = len(tier1) + len(tier2)
        out["tier1"] = [_wire_chunk(r) for r in tier1]
        out["tier2"] = [_wire_chunk(r) for r in tier2]
        return out

    out["result_count"] = len(sv.results)
    out["results"] = [_wire_chunk(r) for r in sv.results]
    return out


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
