"""siftd - LLM conversation analytics.

Public API re-exports for programmatic access.
"""

from siftd.api import (
    ConversationDetail,
    ConversationScore,
    ConversationSummary,
    DatabaseStats,
    Exchange,
    HarnessInfo,
    SearchResult,
    TableCounts,
    ToolCallSummary,
    ToolStats,
    WorkspaceStats,
    aggregate_by_conversation,
    build_index,
    first_mention,
    get_conversation,
    get_stats,
    hybrid_search,
    list_conversations,
)
from siftd.storage.tags import apply_tag, get_or_create_tag, list_tags

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
