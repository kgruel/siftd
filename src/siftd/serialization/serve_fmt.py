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
