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
    NarrativeBlock,
    QueryError,
    QueryFile,
    QueryResult,
    ToolCallDetail,
    ToolCallSummary,
    Turn,
    get_conversation,
    get_recent_conversation_ids,
    list_conversations,
    list_query_files,
    resolve_entity_id,
    run_query_file,
)
from siftd.api.database import (
    backup_database,
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
    ExportArtifact,
    ExportedConversation,
    export_conversations,
    export_document,
)
from siftd.api.file_refs import (
    FileRef,
    fetch_file_refs,
)
from siftd.api.merge import (
    merge_database,
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
from siftd.api.receive import (
    receive_database,
)
from siftd.api.resources import (
    CopyError,
    copy_adapter,
    copy_formatter,
    copy_query,
    list_builtin_formatters,
    list_builtin_queries,
)
from siftd.api.slice import (
    slice_database,
)
from siftd.api.stats import (
    CostCoverage,
    DatabaseStats,
    GroupUsage,
    HarnessInfo,
    TableCounts,
    ToolStats,
    UsageSummary,
    WorkspaceStats,
    get_cost_coverage,
    get_stats,
    get_usage_by_model,
    get_usage_by_workspace,
    get_usage_summary,
    list_workspaces,
    read_stats_cache,
    stats_cache_path,
    write_stats_cache,
)
from siftd.api.sync import (
    PushResult,
    SyncError,
    SyncRemote,
    sync_push,
)
from siftd.api.tags import (
    DERIVATIVE_TAG,
    TagInfo,
    apply_tag,
    delete_tag,
    get_or_create_tag,
    get_tag_id,
    list_tags,
    remove_tag,
    rename_tag,
)
from siftd.api.tool_search import (
    ToolSearchGroup,
    ToolSearchResult,
    group_tool_search_results,
    search_tool_calls,
)
from siftd.api.tools import (
    TagUsage,
    WorkspaceTagUsage,
    get_tool_tag_summary,
    get_tool_tags_by_workspace,
)

# Search symbols are lazy-imported to avoid pulling numpy into non-search commands.
# Access via siftd.api.SearchResult etc. triggers __getattr__ below.
_LAZY_SEARCH_NAMES = {
    "ConversationScore",
    "IndexCompatError",
    "SearchResult",
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
    # adapters
    "AdapterInfo",
    "list_adapters",
    "list_builtin_adapters",
    # database
    "backup_database",
    "create_database",
    "open_database",
    # tags
    "DERIVATIVE_TAG",
    "TagInfo",
    "apply_tag",
    "delete_tag",
    "get_tag_id",
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
    "NarrativeBlock",
    "ToolCallDetail",
    "ToolCallSummary",
    "Turn",
    "get_recent_conversation_ids",
    "list_conversations",
    "get_conversation",
    "resolve_entity_id",
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
    "copy_formatter",
    "copy_query",
    "list_builtin_formatters",
    "list_builtin_queries",
    # search
    "SearchResult",
    "ConversationScore",
    "IndexCompatError",
    "hybrid_search",
    "first_mention",
    "build_index",
    # stats
    "CostCoverage",
    "DatabaseStats",
    "GroupUsage",
    "TableCounts",
    "HarnessInfo",
    "UsageSummary",
    "WorkspaceStats",
    "ToolStats",
    "get_cost_coverage",
    "get_stats",
    "get_usage_by_model",
    "get_usage_by_workspace",
    "get_usage_summary",
    "list_workspaces",
    "stats_cache_path",
    "write_stats_cache",
    "read_stats_cache",
    # merge
    "merge_database",
    # receive
    "receive_database",
    # slice
    "slice_database",
    # sync
    "PushResult",
    "SyncError",
    "SyncRemote",
    "sync_push",
    # tools
    "TagUsage",
    "WorkspaceTagUsage",
    "get_tool_tag_summary",
    "get_tool_tags_by_workspace",
    "ToolSearchGroup",
    "ToolSearchResult",
    "group_tool_search_results",
    "search_tool_calls",
    # export
    "ExportArtifact",
    "ExportedConversation",
    "export_conversations",
    "export_document",
]
