"""Public library API for siftd.

This module provides programmatic access to siftd functionality.
CLI commands are thin wrappers over these functions.
"""

from siftd.api.adapters import (
    AdapterInfo,
    list_adapters,
    list_builtin_adapters,
)
from siftd.api.conversations import (
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
from siftd.api.database import (
    create_database,
    open_database,
)
from siftd.api.doctor import (
    CheckInfo,
    Finding,
    list_checks,
    run_checks,
)
from siftd.api.export import (
    ExportedConversation,
    ExportOptions,
    export_conversations,
    format_exchanges,
    format_export,
    format_json,
    format_prompts,
)
from siftd.api.file_refs import (
    FileRef,
    fetch_file_refs,
)
from siftd.api.peek import (
    PeekExchange,
    SessionDetail,
    SessionInfo,
    find_session_file,
    list_active_sessions,
    read_session_detail,
    tail_session,
)
from siftd.api.resources import (
    CopyError,
    copy_adapter,
    copy_query,
    list_builtin_queries,
)
from siftd.api.search import (
    ConversationScore,
    SearchResult,
    aggregate_by_conversation,
    apply_temporal_weight,
    build_index,
    fetch_conversation_timestamps,
    first_mention,
    fts5_recall_conversations,
    hybrid_search,
    open_embeddings_db,
    search_similar,
)
from siftd.api.stats import (
    DatabaseStats,
    HarnessInfo,
    TableCounts,
    ToolStats,
    WorkspaceStats,
    get_stats,
)
from siftd.api.tags import (
    DERIVATIVE_TAG,
    TagInfo,
    apply_tag,
    delete_tag,
    get_or_create_tag,
    list_tags,
    remove_tag,
    rename_tag,
)
from siftd.api.tools import (
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
    # database
    "create_database",
    "open_database",
    # tags
    "DERIVATIVE_TAG",
    "TagInfo",
    "apply_tag",
    "delete_tag",
    "get_or_create_tag",
    "list_tags",
    "remove_tag",
    "rename_tag",
    # doctor
    "CheckInfo",
    "Finding",
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
    "open_embeddings_db",
    "search_similar",
    "fts5_recall_conversations",
    "apply_temporal_weight",
    "fetch_conversation_timestamps",
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
    # export
    "ExportedConversation",
    "ExportOptions",
    "export_conversations",
    "format_export",
    "format_exchanges",
    "format_json",
    "format_prompts",
]
