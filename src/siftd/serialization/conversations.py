"""Canonical JSON serialization for conversation objects.

Defines the single source of truth for how conversations are serialized
to JSON-safe dicts. Field names here are the API contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity

    from siftd.api.conversations import ConversationDetail, ConversationSummary


def serialize_conversation_summary(conv: ConversationSummary) -> dict[str, Any]:
    """Serialize a ConversationSummary to a JSON-safe dict."""
    d = {
        "id": conv.id,
        "workspace": conv.workspace_path,
        "model": conv.model,
        "started_at": conv.started_at,
        "prompts": conv.prompt_count,
        "responses": conv.response_count,
        "tokens": conv.total_tokens,
        "cost": conv.cost,
        "tags": conv.tags,
    }
    owner = getattr(conv, "owner", None)
    if owner is not None:
        d["owner"] = owner
    return d


def serialize_conversation_list(conversations: list[ConversationSummary]) -> list[dict[str, Any]]:
    """Serialize a list of ConversationSummary objects."""
    return [serialize_conversation_summary(c) for c in conversations]


def serialize_conversation_detail(
    detail: ConversationDetail | Any,
    *,
    fidelity: Fidelity | None = None,
) -> dict[str, Any]:
    """Serialize a ConversationDetail to a JSON-safe dict.

    Includes full turn data with narrative blocks via JsonEmitter.
    Fidelity controls which blocks are included (thinking, tools, etc.).
    Default fidelity includes everything.
    """
    from painted import Fidelity as _Fidelity

    from siftd.serialization.narrative import JsonEmitter, walk_narrative

    if fidelity is None:
        # API default: include everything
        fidelity = _Fidelity(visible=frozenset({"text", "thinking", "tools"}))

    total_tokens = getattr(detail, "total_tokens", None)
    if total_tokens is None:
        total_tokens = (
            getattr(detail, "total_input_tokens", 0)
            + getattr(detail, "total_output_tokens", 0)
        )

    turns_data = []
    for turn in detail.turns:
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

    return {
        "id": detail.id,
        "workspace": detail.workspace_path,
        "model": detail.model,
        "started_at": detail.started_at,
        "tags": detail.tags,
        "total_tokens": total_tokens,
        "turns": turns_data,
    }
