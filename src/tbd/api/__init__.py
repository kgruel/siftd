"""Public library API for tbd.

This module provides programmatic access to tbd functionality.
CLI commands are thin wrappers over these functions.
"""

from tbd.api.conversations import (
    ConversationDetail,
    ConversationSummary,
    Exchange,
    QueryError,
    QueryFile,
    QueryResult,
    ToolCallSummary,
    get_conversation,
    list_conversations,
    list_query_files,
    run_query_file,
)
from tbd.api.search import (
    ConversationScore,
    SearchResult,
    aggregate_by_conversation,
    build_index,
    first_mention,
    hybrid_search,
)
from tbd.api.stats import (
    DatabaseStats,
    HarnessInfo,
    TableCounts,
    ToolStats,
    WorkspaceStats,
    get_stats,
)

__all__ = [
    # conversations
    "ConversationSummary",
    "ConversationDetail",
    "Exchange",
    "ToolCallSummary",
    "list_conversations",
    "get_conversation",
    # query files
    "QueryFile",
    "QueryResult",
    "QueryError",
    "list_query_files",
    "run_query_file",
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
]
