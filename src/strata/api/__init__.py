"""Public library API for strata.

This module provides programmatic access to strata functionality.
CLI commands are thin wrappers over these functions.
"""

from strata.api.adapters import (
    AdapterInfo,
    list_adapters,
    list_builtin_adapters,
)
from strata.api.doctor import (
    CheckInfo,
    Finding,
    FixResult,
    apply_fix,
    list_checks,
    run_checks,
)
from strata.api.peek import (
    PeekExchange,
    SessionDetail,
    SessionInfo,
    find_session_file,
    list_active_sessions,
    read_session_detail,
    tail_session,
)
from strata.api.conversations import (
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
from strata.api.file_refs import (
    FileRef,
    fetch_file_refs,
)
from strata.api.resources import (
    CopyError,
    copy_adapter,
    copy_query,
    list_builtin_queries,
)
from strata.api.search import (
    ConversationScore,
    SearchResult,
    aggregate_by_conversation,
    build_index,
    first_mention,
    hybrid_search,
)
from strata.api.stats import (
    DatabaseStats,
    HarnessInfo,
    TableCounts,
    ToolStats,
    WorkspaceStats,
    get_stats,
)
from strata.api.tools import (
    TagUsage,
    WorkspaceTagUsage,
    get_tool_tag_summary,
    get_tool_tags_by_workspace,
)

__all__ = [
    # adapters
    "AdapterInfo",
    "list_adapters",
    "list_builtin_adapters",
    # doctor
    "CheckInfo",
    "Finding",
    "FixResult",
    "apply_fix",
    "list_checks",
    "run_checks",
    # peek
    "PeekExchange",
    "SessionDetail",
    "SessionInfo",
    "find_session_file",
    "list_active_sessions",
    "read_session_detail",
    "tail_session",
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
    # file refs
    "FileRef",
    "fetch_file_refs",
    # resources
    "CopyError",
    "copy_adapter",
    "copy_query",
    "list_builtin_queries",
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
    # tools
    "TagUsage",
    "WorkspaceTagUsage",
    "get_tool_tag_summary",
    "get_tool_tags_by_workspace",
]
