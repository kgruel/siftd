"""siftd - LLM conversation analytics.

Public API re-exports for programmatic access.
"""

from siftd.api import (
    ConversationDetail,
    ConversationSummary,
    DatabaseStats,
    Exchange,
    HarnessInfo,
    TableCounts,
    ToolCallSummary,
    ToolStats,
    WorkspaceStats,
    get_conversation,
    get_stats,
    list_conversations,
)
from siftd.storage.tags import apply_tag, get_or_create_tag, list_tags

# Search-related symbols are lazy to avoid pulling numpy into non-search commands.
_LAZY_SEARCH_NAMES = {
    "ConversationScore",
    "SearchResult",
    "aggregate_by_conversation",
    "build_index",
    "first_mention",
    "hybrid_search",
}


def __getattr__(name: str):
    if name in _LAZY_SEARCH_NAMES:
        from siftd.api import search as _search_mod

        val = getattr(_search_mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # conversations
    "ConversationSummary",
    "ConversationDetail",
    "Exchange",
    "ToolCallSummary",
    "list_conversations",
    "get_conversation",
    # search
    "SearchResult",
    "ConversationScore",
    "hybrid_search",
    "aggregate_by_conversation",
    "first_mention",
    "build_index",
    # stats
    "DatabaseStats",
    "TableCounts",
    "HarnessInfo",
    "WorkspaceStats",
    "ToolStats",
    "get_stats",
    # tags
    "list_tags",
    "apply_tag",
    "get_or_create_tag",
]
