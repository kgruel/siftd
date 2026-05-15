"""JSON output format — renders conversations as structured JSON.

Selected via --json flag. Serializes the full data model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity

FORMATTER_INTERFACE_VERSION = 1
name = "json"
media_type = "application/json"


def render_detail(result: Any, fidelity: Fidelity, **context: Any) -> dict:
    """Render conversation detail as a dict.

    Args:
        result: ConversationDetail object, or raw turns list (backward compat).

    Returns a dict (not a JSON string) so callers can compose
    (e.g. wrapping multiple conversations in an array for export).
    Caller serializes with json.dumps().
    """
    from siftd.serialization.conversations import serialize_conversation_detail

    if hasattr(result, "turns"):
        detail = result
        turns = context.get("turns", detail.turns)
    else:
        turns = result
        detail = context.get("detail")
    if detail is not None:
        return serialize_conversation_detail(detail, fidelity=fidelity)

    # Fallback for cases where only turns are provided (no detail object)
    from siftd.serialization.narrative import JsonEmitter, walk_narrative

    turns_data = []
    for turn in turns:
        emitter = JsonEmitter()
        narrative = getattr(turn, "narrative", [])
        walk_narrative(narrative, emitter, fidelity=fidelity)

        turns_data.append({
            "timestamp": getattr(turn, "timestamp", None),
            "prompt": getattr(turn, "prompt_text", None),
            "narrative": emitter.blocks,
            "tokens": {
                "input": getattr(turn, "total_input_tokens", 0),
                "output": getattr(turn, "total_output_tokens", 0),
            },
        })

    return {"turns": turns_data}


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation list as a JSON envelope.

    Shape (always): ``{"result": [...summaries...], "caveats": [...]}``.
    The envelope is unconditional — empty caveats render as ``[]`` —
    so downstream pipelines don't have to branch on shape. This is a
    one-time break from the prior bare-array shape; consumers that
    used ``siftd query --json | jq '.[]'`` should switch to ``.result[]``.

    Context keys:
        caveats: list[Finding] — threaded from dispatch.
    """
    import json
    from dataclasses import asdict

    from siftd.serialization.conversations import serialize_conversation_list

    caveats = context.get("caveats") or []
    envelope = {
        "result": serialize_conversation_list(summaries),
        "caveats": [asdict(c) for c in caveats],
    }
    return json.dumps(envelope, indent=2)


def render_search(results: list, fidelity: Fidelity, **context: Any) -> dict:
    """Render search results as a dict (caller serializes).

    Context keys:
        query: str — the search query
        mode: str — "chunks", "conversations", or "thread"
        tier1: list — expanded results (thread mode)
        tier2: list — compact results (thread mode)
        caveats: list[Finding] — threaded from dispatch; serialized as
            ``"caveats": [...]`` in the envelope (empty list when absent).
    """
    from dataclasses import asdict
    from datetime import UTC, datetime

    query = context.get("query", "")
    mode = context.get("mode", "chunks")
    caveats = context.get("caveats") or []

    output: dict[str, Any] = {
        "query": query,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "result_count": len(results),
        "caveats": [asdict(c) for c in caveats],
    }

    if mode == "conversations":
        output["results"] = [
            {
                "conversation_id": r.get("conversation_id"),
                "max_score": round(r.get("max_score", 0.0), 4),
                "mean_score": round(r.get("mean_score", 0.0), 4),
                "chunk_count": r.get("chunk_count", 0),
                "started_at": r.get("_started_at"),
                "workspace": r.get("_workspace"),
                "best_excerpt": r.get("best_excerpt", ""),
            }
            for r in results
        ]
        return output

    if mode == "thread":
        tier1 = context.get("tier1", [])
        tier2 = context.get("tier2", [])
        output["result_count"] = len(tier1) + len(tier2)
        output["tier1"] = _json_chunk_list(tier1)
        output["tier2"] = _json_chunk_list(tier2)
        return output

    # Chunks mode
    output["results"] = _json_chunk_list(results)
    return output


def render_stats(stats: Any, fidelity: Fidelity, **context: Any) -> dict:
    """Render database stats as a dict."""
    from siftd.serialization.stats import serialize_stats

    return serialize_stats(stats)


def render_workspaces(rows: list, fidelity: Fidelity, **context: Any) -> dict:
    """Render workspace list as a dict."""
    from siftd.serialization.serve_fmt import render_workspaces as _impl

    return _impl(rows, fidelity)


def render_tags(tags: list, fidelity: Fidelity, **context: Any) -> dict:
    """Render tag list as a dict."""
    from siftd.serialization.serve_fmt import render_tags as _impl

    return _impl(tags, fidelity)


def _json_chunk_list(results: list) -> list[dict]:
    """Build JSON-safe list of chunk dicts. Emits chunk_id and source_ids."""
    from siftd.domain.search_types import ScoreBreakdown

    out = []
    for r in results:
        chunk: dict[str, Any] = {
            "conversation_id": r.get("conversation_id"),
            "score": round(r.get("score", 0.0), 4),
            "chunk_type": r.get("chunk_type", ""),
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
        if breakdown and isinstance(breakdown, ScoreBreakdown):
            chunk["breakdown"] = breakdown.to_dict()

        file_refs = r.get("file_refs")
        if file_refs:
            chunk["file_refs"] = [
                {
                    "basename": ref.basename,
                    "path": ref.path,
                    "op": ref.op,
                    "content_length": len(ref.content) if ref.content else 0,
                }
                for ref in file_refs
            ]

        out.append(chunk)
    return out
