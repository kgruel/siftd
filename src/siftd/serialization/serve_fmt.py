"""Serve format — JSON serialization for serve route dispatch.

This module is the serve-side equivalent of output/json_fmt.py.
It lives in serialization/ so serve can import it without violating
the architecture boundary (serve cannot import output).

Render methods match the format protocol interface: render_{name}(result, fidelity).
"""

from __future__ import annotations

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
            {
                "name": t.name,
                "conversation_count": t.conversation_count,
                "workspace_count": t.workspace_count,
                "tool_call_count": t.tool_call_count,
                "prompt_count": t.prompt_count,
            }
            for t in tags
        ]
    }


def render_tool_search(result: Any, fidelity: Fidelity) -> dict:
    """Serialize (ToolQuery, list[ToolSearchResult]) to JSON-safe dict."""
    parsed, results = result
    return {
        "query": parsed.raw,
        "result_count": len(results),
        "results": [
            {
                "tool_call_id": r.tool_call_id,
                "conversation_id": r.conversation_id,
                "timestamp": r.timestamp,
                "tool_name": r.tool_name,
                "tool_family": r.tool_family,
                "status": r.status,
                "path": r.path,
                "basename": r.basename,
                "command": r.command,
                "command_verb": r.command_verb,
                "result_snippet": r.result_snippet,
                "workspace_path": r.workspace_path,
                "rank": r.rank,
            }
            for r in results
        ],
    }


def render_tools(tags: list, fidelity: Fidelity) -> dict:
    """Serialize tool tag summary to JSON-safe dict."""
    total = sum(t.count for t in tags)
    return {
        "total": total,
        "tags": [
            {"name": t.name, "count": t.count, "percentage": round((t.count / total) * 100, 1) if total else 0}
            for t in tags
        ],
    }


def render_tools_by_workspace(results: list, fidelity: Fidelity) -> dict:
    """Serialize per-workspace tool tag usage to JSON-safe dict."""
    return {
        "workspaces": [
            {
                "workspace": ws.workspace,
                "total": ws.total,
                "tags": [{"name": t.name, "count": t.count} for t in ws.tags],
            }
            for ws in results
        ]
    }


def render_search(results: list, fidelity: Fidelity) -> dict:
    """Serialize SearchResult list to JSON-safe dict."""
    import dataclasses

    serialized = [
        r if isinstance(r, dict) else dataclasses.asdict(r)
        for r in results
    ]
    return {
        "result_count": len(serialized),
        "results": serialized,
    }


def render_export(conversations: list, fidelity: Fidelity) -> dict:
    """Serialize exported conversations to JSON-safe dict."""
    from siftd.serialization.conversations import serialize_conversation_detail

    return {
        "conversations": [serialize_conversation_detail(c) for c in conversations],
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
