"""Search API extensions.

Re-exports core search functionality and adds post-processing functions.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean as _mean

# Re-export core search API
from siftd.search import SearchResult, hybrid_search

__all__ = [
    "SearchResult",
    "hybrid_search",
    "ConversationScore",
    "aggregate_by_conversation",
    "first_mention",
    "build_index",
]


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
            Dicts must have 'score', 'conversation_id', and optionally 'chunk_id'.
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

    # Get timestamps for conversations
    conv_ids = list({_get(r, "conversation_id") for r in above})
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(conv_ids))
    rows = conn.execute(
        f"SELECT id, started_at FROM conversations WHERE id IN ({placeholders})",
        conv_ids,
    ).fetchall()
    conn.close()

    conv_times = {row["id"]: row["started_at"] or "" for row in rows}

    # Sort by conversation start time, then by chunk_id (ULID = time-ordered)
    above.sort(key=lambda r: (conv_times.get(_get(r, "conversation_id"), ""), _get(r, "chunk_id") or ""))

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
