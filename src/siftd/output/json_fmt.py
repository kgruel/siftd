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


def render_detail(turns: list, fidelity: Fidelity, **context: Any) -> dict:
    """Render conversation detail as a dict.

    Returns a dict (not a JSON string) so callers can compose
    (e.g. wrapping multiple conversations in an array for export).
    Caller serializes with json.dumps().

    Context keys:
        detail: conversation metadata object (ConversationDetail or ExportedConversation)
    """
    from siftd.output.narrative import JsonEmitter, walk_narrative

    detail = context.get("detail")

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

    conv_data: dict[str, Any] = {}
    if detail:
        total_tokens = getattr(detail, "total_tokens", None)
        if total_tokens is None:
            total_tokens = (
                getattr(detail, "total_input_tokens", 0)
                + getattr(detail, "total_output_tokens", 0)
            )
        conv_data = {
            "id": getattr(detail, "id", None),
            "workspace": getattr(detail, "workspace_path", None),
            "model": getattr(detail, "model", None),
            "started_at": getattr(detail, "started_at", None),
            "tags": getattr(detail, "tags", []),
            "total_tokens": total_tokens,
        }

    conv_data["turns"] = turns_data
    return conv_data


def render_list(summaries: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation list as JSON array.

    Always includes all fields regardless of fidelity depth.
    """
    import json

    out = [
        {
            "id": c.id,
            "workspace": c.workspace_path,
            "model": c.model,
            "started_at": c.started_at,
            "prompts": c.prompt_count,
            "responses": c.response_count,
            "tokens": c.total_tokens,
            "cost": c.cost,
            "tags": c.tags,
        }
        for c in summaries
    ]
    return json.dumps(out, indent=2)


def render_search(results: list, fidelity: Fidelity, **context: Any) -> dict:
    """Render search results as a dict (caller serializes).

    Context keys:
        query: str — the search query
        mode: str — "chunks", "conversations", or "thread"
        tier1: list — expanded results (thread mode)
        tier2: list — compact results (thread mode)
    """
    from datetime import UTC, datetime

    query = context.get("query", "")
    mode = context.get("mode", "chunks")

    output: dict[str, Any] = {
        "query": query,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "result_count": len(results),
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


def _json_chunk_list(results: list) -> list[dict]:
    """Build JSON-safe list of chunk dicts."""
    from siftd.search import ScoreBreakdown

    out = []
    for r in results:
        chunk: dict[str, Any] = {
            "chunk_id": r.get("chunk_id"),
            "conversation_id": r.get("conversation_id"),
            "score": round(r.get("score", 0.0), 4),
            "chunk_type": r.get("chunk_type", ""),
            "text": r.get("text", ""),
            "source_ids": r.get("source_ids", []),
            "conversation": {
                "started_at": r.get("_started_at"),
                "workspace": r.get("_workspace"),
            },
        }

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
