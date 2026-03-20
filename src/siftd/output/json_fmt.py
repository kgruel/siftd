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


def render_detail(turns: list, fidelity: Fidelity, **context: Any) -> str:
    """Render conversation detail as JSON string.

    This is a stub that will be replaced by the narrative walker (Stage 4).
    Currently delegates to the caller via context for backward compat.

    Context keys:
        _render_fn: callable(turns, fidelity, **context) -> str
    """
    render_fn = context.get("_render_fn")
    if render_fn:
        return render_fn(turns, fidelity, **context)
    return "[]"


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
