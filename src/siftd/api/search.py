"""Search API extensions.

Re-exports core search functionality and adds post-processing functions.

Heavy dependencies (numpy via siftd.search, siftd.storage.embeddings) are
lazy-imported so that non-search CLI commands don't pull in numpy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean as _mean
from typing import TYPE_CHECKING

from siftd.storage.queries import fetch_conversation_timestamps, fetch_prompt_timestamps

if TYPE_CHECKING:
    from siftd.search import SearchResult, apply_temporal_weight, hybrid_search
    from siftd.storage.embeddings import IndexCompatError

# Lazy re-exports — resolved on first access to avoid eager numpy import.
_LAZY_IMPORTS = {
    "SearchResult": "siftd.search",
    "apply_temporal_weight": "siftd.search",
    "hybrid_search": "siftd.search",
    "IndexCompatError": "siftd.storage.embeddings",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib

        mod = importlib.import_module(_LAZY_IMPORTS[name])
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "SearchResult",
    "hybrid_search",
    "ConversationScore",
    "aggregate_by_conversation",
    "first_mention",
    "build_index",
    # Temporal weighting
    "apply_temporal_weight",
    "fetch_conversation_timestamps",
    # Embeddings
    "open_embeddings_db",
    "search_similar",
    "validate_index_compat",
    "IndexCompatError",
    # FTS5
    "fts5_recall_conversations",
    "rebuild_fts_index",
]


def open_embeddings_db(
    db_path: Path,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open the embeddings database.

    Args:
        db_path: Path to the embeddings database file.
        read_only: If True, open in read-only mode.

    Returns:
        An open sqlite3.Connection.
    """
    from siftd.storage.embeddings import open_embeddings_db as _open_embeddings_db

    return _open_embeddings_db(db_path, read_only=read_only)


def search_similar(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    *,
    limit: int = 10,
    conversation_ids: set[str] | None = None,
    include_embeddings: bool = False,
) -> list[dict]:
    """Search for similar chunks in the embeddings database.

    Args:
        conn: Connection to embeddings database.
        query_embedding: The query embedding vector.
        limit: Maximum results to return.
        conversation_ids: Optional set of conversation IDs to filter by.
        include_embeddings: If True, include embedding vectors in results.

    Returns:
        List of result dicts with score, chunk_id, conversation_id, text, etc.
    """
    from siftd.storage.embeddings import search_similar as _search_similar

    return _search_similar(
        conn,
        query_embedding,
        limit=limit,
        conversation_ids=conversation_ids,
        include_embeddings=include_embeddings,
    )


def validate_index_compat(
    conn: sqlite3.Connection,
    backend_name: str,
    backend_model: str,
    backend_dimension: int,
    current_schema_version: int,
) -> None:
    """Validate that stored index metadata is compatible with the current backend.

    Args:
        conn: Embeddings database connection.
        backend_name: Current backend name (e.g., "fastembed", "ollama").
        backend_model: Current backend model (e.g., "BAAI/bge-small-en-v1.5").
        backend_dimension: Current embedding dimension.
        current_schema_version: Current schema version constant.

    Raises:
        IndexCompatError: If metadata indicates incompatibility with actionable message.

    Note:
        Missing metadata keys (pre-versioning indexes) are allowed with warning-level
        degradation — dimension validation still applies via search_similar().
    """
    from siftd.storage.embeddings import validate_index_compat as _validate

    return _validate(
        conn,
        backend_name,
        backend_model,
        backend_dimension,
        current_schema_version,
    )


def fts5_recall_conversations(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 80,
) -> tuple[set[str], str]:
    """FTS5 recall to narrow candidate conversations for embedding search.

    Args:
        conn: Connection to main database.
        query: The search query string.
        limit: Maximum conversation IDs to return.

    Returns:
        Tuple of (conversation_id set, mode string).
        Mode is "and", "or", or "none".
    """
    from siftd.storage.fts import fts5_recall_conversations as _fts5_recall

    return _fts5_recall(conn, query, limit=limit)


def rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index for the main database."""
    from siftd.storage.fts import rebuild_fts_index as _rebuild_fts_index

    _rebuild_fts_index(conn)


def fts5_search_content(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """FTS5 keyword search over content.

    Args:
        conn: Connection to main database.
        query: The search query string.
        limit: Maximum results to return.

    Returns:
        List of dicts with: conversation_id, side, snippet, rank.
    """
    from siftd.storage.fts import search_content as _search_content

    return _search_content(conn, query, limit=limit)


@dataclass
class ConversationScore:
    """Aggregated conversation-level search result."""

    conversation_id: str
    max_score: float
    mean_score: float
    chunk_count: int
    best_excerpt: str
    workspace_path: str | None
    started_at: str | None


def aggregate_by_conversation(
    results: list[SearchResult],
    *,
    limit: int = 10,
) -> list[ConversationScore]:
    """Aggregate chunk results to conversation-level scores.

    Groups results by conversation, computes max/mean scores,
    and returns ranked conversations.

    Args:
        results: List of SearchResult from hybrid_search.
        limit: Maximum conversations to return.

    Returns:
        List of ConversationScore, sorted by max_score descending.
    """
    if not results:
        return []

    # Group by conversation
    by_conv: dict[str, list[SearchResult]] = {}
    for r in results:
        by_conv.setdefault(r.conversation_id, []).append(r)

    # Score each conversation
    conv_scores = []
    for conv_id, chunks in by_conv.items():
        scores = [c.score for c in chunks]
        best_chunk = max(chunks, key=lambda c: c.score)
        conv_scores.append(
            ConversationScore(
                conversation_id=conv_id,
                max_score=max(scores),
                mean_score=_mean(scores),
                chunk_count=len(chunks),
                best_excerpt=best_chunk.text[:500],
                workspace_path=best_chunk.workspace_path,
                started_at=best_chunk.started_at,
            )
        )

    conv_scores.sort(key=lambda x: x.max_score, reverse=True)
    return conv_scores[:limit]


def first_mention(
    results: list[SearchResult] | list[dict],
    *,
    threshold: float = 0.65,
    db_path: Path | None = None,
) -> SearchResult | dict | None:
    """Find chronologically earliest result above relevance threshold.

    Args:
        results: List of SearchResult or raw dicts from search.
            Dicts must have 'score', 'conversation_id', and 'source_ids'.
        threshold: Minimum score to consider relevant.
        db_path: Path to database (for timestamp lookup). Uses default if not specified.

    Returns:
        Earliest result above threshold (same type as input), or None if none qualify.
    """
    from siftd.paths import db_path as default_db_path

    def _get(r, key):
        """Access attribute or dict key."""
        return getattr(r, key, None) or r.get(key) if isinstance(r, dict) else getattr(r, key)

    # Filter to results above threshold
    above = [r for r in results if _get(r, "score") >= threshold]
    if not above:
        return None

    db = db_path or default_db_path()

    from siftd.api.database import open_database

    conn = open_database(db, read_only=True)

    # Collect all prompt IDs from source_ids for timestamp lookup
    all_prompt_ids = []
    for r in above:
        source_ids = _get(r, "source_ids") or []
        all_prompt_ids.extend(source_ids)

    # Get prompt timestamps (preferred) and conversation timestamps (fallback)
    prompt_times = fetch_prompt_timestamps(conn, all_prompt_ids) if all_prompt_ids else {}
    conv_ids = list({_get(r, "conversation_id") for r in above})
    conv_times = fetch_conversation_timestamps(conn, conv_ids)
    conn.close()

    def earliest_prompt_time(r):
        """Get earliest prompt timestamp for a result, fallback to conversation start."""
        source_ids = _get(r, "source_ids") or []
        if source_ids:
            # Get timestamps for this result's prompts
            times = [prompt_times.get(pid, "") for pid in source_ids]
            valid_times = [t for t in times if t]
            if valid_times:
                return min(valid_times)
        # Fallback to conversation start time
        return conv_times.get(_get(r, "conversation_id"), "")

    # Sort by earliest prompt timestamp, then by chunk_id as tiebreaker
    above.sort(key=lambda r: (earliest_prompt_time(r), _get(r, "chunk_id") or ""))

    return above[0]


def build_index(
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
    rebuild: bool = False,
    backend: str | None = None,
    verbose: bool = False,
) -> dict:
    """Build or update the embeddings index.

    Thin wrapper over siftd.embeddings.build_embeddings_index that returns
    a dict for backwards compatibility.

    Args:
        db_path: Path to main database. Uses default if not specified.
        embed_db_path: Path to embeddings database. Uses default if not specified.
        rebuild: If True, clear and rebuild from scratch.
        backend: Preferred embedding backend name.
        verbose: Print progress messages.

    Returns:
        Dict with 'chunks_added' and 'total_chunks' counts.

    Raises:
        FileNotFoundError: If main database doesn't exist.
        RuntimeError: If no embedding backend is available.
        EmbeddingsNotAvailable: If embedding dependencies are not installed.
    """
    from siftd.embeddings import require_embeddings

    require_embeddings("Building embeddings index")

    from siftd.embeddings import build_embeddings_index

    stats = build_embeddings_index(
        db_path=db_path,
        embed_db_path=embed_db_path,
        rebuild=rebuild,
        backend_name=backend,
        verbose=verbose,
    )
    return {"chunks_added": stats.chunks_added, "total_chunks": stats.total_chunks}
