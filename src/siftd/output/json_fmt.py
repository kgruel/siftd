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
brief_chars = 0  # JSON never truncates


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
