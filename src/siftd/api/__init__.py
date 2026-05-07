"""Public library API for siftd.

This module provides programmatic access to siftd functionality.
CLI commands are thin wrappers over these functions.
"""

from siftd.api.adapters import (
    AdapterInfo,
    list_adapters,
    list_builtin_adapters,
)
from siftd.api.backfill import (
    BackfillOperation,
    BackfillRunResult,
    run_backfill,
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
    PreflightError,
    SchemaUpgradeRequiredError,
    audit_db_integrity,
    backup_database,
    create_database,
    open_database,
    run_preflight,
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
from siftd.api.ingest import (
    AdapterSelectionError,
    IngestRunResult,
    run_ingest,
    run_rebuild_fts,
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
from siftd.api.serve_status import (
    HealthStatus,
    get_health_status,
    record_push_log,
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
    dict_to_stats,
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
    ApplyResult,
    DeleteResult,
    RenameResult,
    TagInfo,
    apply_tag,
    apply_tags,
    delete_tag,
    delete_tag_safe,
    get_or_create_tag,
    get_tag_id,
    list_tags,
    remove_tag,
    rename_tag,
    rename_tag_safe,
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
    "ConversationSearchSummary",
    "IncrementalCompatError",
    "IndexCompatError",
    "ScoreBreakdown",
    "SearchChunk",
    "SearchResult",
    "aggregate_by_conversation",
    "build_index",
    "compute_thread_tiers",
    "enrich_context_window",
    "enrich_exchanges",
    "enrich_file_refs",
    "enrich_search_metadata",
    "embeddings_available",
    "filter_by_threshold",
    "first_mention",
    "hybrid_search",
    "search_chunks",
    "sort_chunks_by_time",
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
    # backfill
    "BackfillOperation",
    "BackfillRunResult",
    "run_backfill",
    # database
    "PreflightError",
    "SchemaUpgradeRequiredError",
    "audit_db_integrity",
    "backup_database",
    "create_database",
    "open_database",
    "run_preflight",
    # tags
    "DERIVATIVE_TAG",
    "ApplyResult",
    "DeleteResult",
    "RenameResult",
    "TagInfo",
    "apply_tags",
    "apply_tag",
    "delete_tag_safe",
    "delete_tag",
    "get_tag_id",
    "get_or_create_tag",
    "list_tags",
    "rename_tag_safe",
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
    # ingest
    "AdapterSelectionError",
    "IngestRunResult",
    "run_ingest",
    "run_rebuild_fts",
    # resources
    "CopyError",
    "copy_adapter",
    "copy_formatter",
    "copy_query",
    "list_builtin_formatters",
    "list_builtin_queries",
    # search
    "SearchChunk",
    "SearchResult",
    "ScoreBreakdown",
    "ConversationSearchSummary",
    "ConversationScore",
    "IndexCompatError",
    "search_chunks",
    "hybrid_search",
    "aggregate_by_conversation",
    "compute_thread_tiers",
    "filter_by_threshold",
    "sort_chunks_by_time",
    "enrich_search_metadata",
    "enrich_file_refs",
    "enrich_exchanges",
    "enrich_context_window",
    "embeddings_available",
    "first_mention",
    "build_index",
    # stats
    "HealthStatus",
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
    "dict_to_stats",
    "list_workspaces",
    "stats_cache_path",
    "write_stats_cache",
    "read_stats_cache",
    "get_health_status",
    "record_push_log",
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
