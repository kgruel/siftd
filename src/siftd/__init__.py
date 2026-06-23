"""siftd - LLM conversation analytics.

Public API re-exports for programmatic access.
"""

from siftd.api import (
    ConversationDetail,
    ConversationSummary,
    DatabaseStats,
    Exchange,
    HarnessInfo,
    NarrativeBlock,
    TableCounts,
    ToolCallDetail,
    ToolCallSummary,
    ToolStats,
    Turn,
    WorkspaceStats,
    apply_tag,
    apply_tags,
    delete_tag_safe,
    get_conversation,
    get_or_create_tag,
    get_stats,
    list_conversations,
    list_tags,
    rename_tag_safe,
)

# Search-related symbols are lazy to avoid pulling numpy into non-search commands.
_LAZY_SEARCH_NAMES = {
    "ConversationScore",
    "ConversationSearchSummary",
    "ScoreBreakdown",
    "SearchChunk",
    "SearchResult",
    "SearchView",
    "aggregate_by_conversation",
    "build_index",
    "compute_thread_tiers",
    "enrich_around_window",
    "enrich_context_window",
    "enrich_exchanges",
    "enrich_file_refs",
    "enrich_search_metadata",
    "embeddings_available",
    "filter_by_threshold",
    "first_mention",
    "hybrid_search",
    "parse_turns_range",
    "process_search_view",
    "search_chunks",
    "search_view",
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
    # conversations
    "ConversationSummary",
    "ConversationDetail",
    "Exchange",
    "NarrativeBlock",
    "ToolCallDetail",
    "ToolCallSummary",
    "Turn",
    "list_conversations",
    "get_conversation",
    # search
    "SearchChunk",
    "SearchResult",
    "ScoreBreakdown",
    "ConversationSearchSummary",
    "ConversationScore",
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
    "enrich_around_window",
    "embeddings_available",
    "first_mention",
    "SearchView",
    "process_search_view",
    "search_view",
    "parse_turns_range",
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
    "apply_tags",
    "get_or_create_tag",
    "rename_tag_safe",
    "delete_tag_safe",
]
