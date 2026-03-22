"""Canonical JSON serialization for database statistics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from siftd.api.stats import DatabaseStats


def serialize_stats(stats: DatabaseStats) -> dict[str, Any]:
    """Serialize DatabaseStats to a JSON-safe dict."""
    return {
        "db_path": str(stats.db_path),
        "db_size_bytes": stats.db_size_bytes,
        "counts": {
            "conversations": stats.counts.conversations,
            "prompts": stats.counts.prompts,
            "responses": stats.counts.responses,
            "tool_calls": stats.counts.tool_calls,
            "harnesses": stats.counts.harnesses,
            "workspaces": stats.counts.workspaces,
            "tools": stats.counts.tools,
            "models": stats.counts.models,
            "ingested_files": stats.counts.ingested_files,
        },
        "harnesses": [
            {"name": h.name, "source": h.source, "log_format": h.log_format}
            for h in stats.harnesses
        ],
        "harness_counts": [
            {"name": hc.name, "conversation_count": hc.conversation_count}
            for hc in stats.harness_counts
        ],
        "top_workspaces": [
            {
                "path": w.path,
                "conversation_count": w.conversation_count,
                "last_activity": w.last_activity,
            }
            for w in stats.top_workspaces
        ],
        "models": stats.models,
        "top_tools": [
            {"name": t.name, "usage_count": t.usage_count}
            for t in stats.top_tools
        ],
        "top_tags": [
            {"name": t.name, "count": t.count} for t in stats.top_tags
        ],
        "token_coverage": {
            "responses": stats.token_coverage.responses,
            "with_tokens": stats.token_coverage.with_tokens,
            "pct_with_tokens": stats.token_coverage.pct_with_tokens,
            "by_harness": [
                {
                    "name": h.name,
                    "responses": h.responses,
                    "with_tokens": h.with_tokens,
                    "pct_with_tokens": h.pct_with_tokens,
                }
                for h in stats.token_coverage.by_harness
            ],
        },
        "activity_window": list(stats.activity_window),
        "last_ingest_at": stats.last_ingest_at,
    }
