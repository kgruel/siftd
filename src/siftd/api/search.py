"""Search API extensions.

Re-exports core search functionality and adds post-processing functions.

Heavy dependencies (numpy via siftd.search, siftd.storage.embeddings) are
lazy-imported so that non-search CLI commands don't pull in numpy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from statistics import mean as _mean
from typing import TYPE_CHECKING, Any, Protocol

from siftd.domain.search_types import ConversationSearchSummary, SearchChunk
from siftd.storage.queries import (
    fetch_all_conversation_ids,
    fetch_conversation_timestamps,
    fetch_prompt_response_texts,
    fetch_prompt_timestamps,
)

if TYPE_CHECKING:
    from siftd.search import apply_temporal_weight
    from siftd.storage.embeddings import IndexCompatError


class EmbeddingBackend(Protocol):
    """Minimal protocol for embedding backends (real or fake)."""

    name: str
    model: str
    dimension: int

    def embed_one(self, text: str) -> list[float]: ...

# Lazy re-exports — resolved on first access to avoid eager numpy import.
_LAZY_IMPORTS = {
    "SearchResult": "siftd.search",
    "apply_temporal_weight": "siftd.search",
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
    "search_chunks",
    "hybrid_search",
    "ConversationScore",
    "aggregate_by_conversation",
    "compute_thread_tiers",
    "filter_by_threshold",
    "sort_chunks_by_time",
    "enrich_search_metadata",
    "enrich_file_refs",
    "enrich_exchanges",
    "enrich_context_window",
    "first_mention",
    "build_index",
    # Temporal weighting
    "apply_temporal_weight",
    "fetch_conversation_timestamps",
    "list_conversation_ids",
    # Embeddings
    "open_embeddings_db",
    "search_similar",
    "validate_index_compat",
    "IndexCompatError",
    # Candidate resolution
    "resolve_candidates",
    # FTS5
    "fts5_recall_conversations",
    "rebuild_fts_index",
    # Exchange data
    "fetch_prompt_response_texts",
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


def list_conversation_ids(conn: sqlite3.Connection) -> set[str]:
    """Return all conversation IDs."""
    return set(fetch_all_conversation_ids(conn))


def resolve_candidates(
    db: Path,
    *,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
) -> set[str] | None:
    """Resolve candidate conversation IDs from filters + scope options.

    Composes filter_conversations() with active-session exclusion and
    the derivative-tag default. Returns None if no constraints apply.
    """
    from siftd.search import resolve_candidates as _resolve

    return _resolve(
        db,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        tag=tag,
        all_tags=all_tags,
        no_tag=no_tag,
        exclude_active=exclude_active,
        include_derivative=include_derivative,
        owner=owner,
    )


ConversationScore = ConversationSearchSummary
SearchResult = SearchChunk


def _as_chunk(r: SearchChunk | dict[str, Any]) -> SearchChunk:
    """Normalize mixed search result inputs to SearchChunk."""
    if isinstance(r, SearchChunk):
        return r
    return SearchChunk.from_mapping(r)


def aggregate_by_conversation(
    results: list[SearchChunk] | list[dict[str, Any]],
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

    chunks = [_as_chunk(r) for r in results]

    # Group by conversation
    by_conv: dict[str, list[SearchChunk]] = {}
    for r in chunks:
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
                file_refs=best_chunk.file_refs,
            )
        )

    conv_scores.sort(key=lambda x: x.max_score, reverse=True)
    return conv_scores[:limit]


def filter_by_threshold(
    results: list[SearchChunk] | list[dict[str, Any]],
    *,
    threshold: float | None,
) -> list[SearchChunk]:
    """Filter chunk results by score threshold."""
    chunks = [_as_chunk(r) for r in results]
    if threshold is None:
        return chunks
    return [r for r in chunks if r.score >= threshold]


def sort_chunks_by_time(
    results: list[SearchChunk] | list[dict[str, Any]],
) -> list[SearchChunk]:
    """Sort chunks by date then chunk_id (legacy CLI behavior)."""
    chunks = [_as_chunk(r) for r in results]
    return sorted(chunks, key=lambda r: ((r.started_at or "")[:10], r.chunk_id or ""))


def compute_thread_tiers(
    results: list[SearchChunk] | list[dict[str, Any]],
) -> tuple[list[SearchChunk], list[SearchChunk]]:
    """Split chunks into tier1 (expanded) and tier2 (compact) for thread mode."""
    chunks = [_as_chunk(r) for r in results]
    conv_scores: dict[str, float] = {}
    conv_best: dict[str, SearchChunk] = {}
    for r in chunks:
        cid = r.conversation_id
        if cid not in conv_scores or r.score > conv_scores[cid]:
            conv_scores[cid] = r.score
            conv_best[cid] = r

    scores = list(conv_scores.values())
    mean_score = sum(scores) / len(scores) if scores else 0.0

    tier1_ids = [cid for cid, s in conv_scores.items() if s > mean_score]
    tier2_ids = [cid for cid in conv_scores if cid not in set(tier1_ids)]

    tier1_ids.sort(key=lambda cid: (conv_best[cid].started_at or "")[:10])
    tier2_ids.sort(key=lambda cid: conv_scores[cid], reverse=True)

    return [conv_best[cid] for cid in tier1_ids], [conv_best[cid] for cid in tier2_ids]


def _workspace_label(path: str | None) -> str:
    """Mirror output.common.fmt_workspace without importing output layer."""
    if path is None:
        return ""
    if path in {"", "/"}:
        return "(root)"
    return Path(path).name


def enrich_search_metadata(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Enrich chunks with workspace and started_at metadata in-place."""
    conv_ids = list({r.conversation_id for r in results})
    if not conv_ids:
        return

    placeholders = ",".join("?" * len(conv_ids))
    rows = conn.execute(
        f"""
        SELECT c.id, c.started_at, w.path AS workspace
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE c.id IN ({placeholders})
    """,
        conv_ids,
    ).fetchall()
    meta = {row["id"]: dict(row) for row in rows}

    for r in results:
        m = meta.get(r.conversation_id, {})
        r.workspace_path = _workspace_label(m.get("workspace"))
        started = m.get("started_at")
        r.started_at = (started or "")[:10] if started else None


def enrich_file_refs(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Attach file references to each chunk in-place."""
    from siftd.api import fetch_file_refs

    all_source_ids: list[str] = []
    for r in results:
        all_source_ids.extend(r.source_ids or [])
    if not all_source_ids:
        return

    refs_by_prompt = fetch_file_refs(conn, all_source_ids)
    for r in results:
        refs = []
        for sid in (r.source_ids or []):
            refs.extend(refs_by_prompt.get(sid, []))
        r.file_refs = refs


def enrich_exchanges(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Attach full prompt+response exchanges for each chunk in-place."""
    for r in results:
        source_ids = r.source_ids or []
        if source_ids:
            r.exchanges = fetch_prompt_response_texts(conn, source_ids)


def enrich_context_window(conn: sqlite3.Connection, results: list[SearchChunk], n: int) -> None:
    """Attach +/-N context exchanges around each chunk's source prompts."""
    for r in results:
        source_ids = r.source_ids or []
        if not source_ids:
            continue

        all_prompts = conn.execute(
            """
            SELECT p.id FROM prompts p
            WHERE p.conversation_id = ?
            ORDER BY p.timestamp
        """,
            (r.conversation_id,),
        ).fetchall()
        prompt_order = [row[0] for row in all_prompts]
        source_set = set(source_ids)
        source_indices = [i for i, pid in enumerate(prompt_order) if pid in source_set]
        if not source_indices:
            continue

        start = max(0, min(source_indices) - n)
        end = min(len(prompt_order), max(source_indices) + n + 1)
        context_ids = prompt_order[start:end]
        exchanges = fetch_prompt_response_texts(conn, context_ids)
        r.context_window = [(pid, pt, rt, pid in source_set) for pid, pt, rt in exchanges]


def first_mention(
    results: list[SearchChunk] | list[dict[str, Any]],
    *,
    threshold: float = 0.65,
    db_path: Path | None = None,
) -> SearchChunk | dict[str, Any] | None:
    """Find chronologically earliest result above relevance threshold.

    Args:
        results: List of SearchChunk or raw dicts from search.
            Dicts must have 'score', 'conversation_id', and 'source_ids'.
        threshold: Minimum score to consider relevant.
        db_path: Path to database (for timestamp lookup). Uses default if not specified.

    Returns:
        Earliest result above threshold (same type as input), or None if none qualify.
    """
    from siftd.paths import db_path as default_db_path

    def _get(r: SearchChunk | dict[str, Any], key: str):
        """Access attribute or dict key."""
        if isinstance(r, dict):
            return r.get(key)
        return getattr(r, key, None)

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


def search_chunks(
    q: str,
    *,
    db_path: Path,
    embed_db: Path | None = None,
    n: int = 10,
    mode: str = "hybrid",
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
    recall: int = 80,
    rerank: str = "mmr",
    lambda_: float = 0.7,
    recency: bool = False,
    recency_half_life: float = 30.0,
    recency_max_boost: float = 1.15,
    threshold: float = 0.0,
    backend: str | None = None,
    embed_backend: EmbeddingBackend | None = None,
) -> list[SearchChunk]:
    """Canonical entry point for retrieving search chunks."""
    return hybrid_search(
        q,
        db_path=db_path,
        embed_db=embed_db,
        n=n,
        mode=mode,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        tag=tag,
        all_tags=all_tags,
        no_tag=no_tag,
        exclude_active=exclude_active,
        include_derivative=include_derivative,
        owner=owner,
        recall=recall,
        rerank=rerank,
        lambda_=lambda_,
        recency=recency,
        recency_half_life=recency_half_life,
        recency_max_boost=recency_max_boost,
        threshold=threshold,
        backend=backend,
        embed_backend=embed_backend,
    )


def hybrid_search(
    q: str,
    *,
    db_path: Path,
    embed_db: Path | None = None,
    n: int = 10,
    mode: str = "hybrid",
    # Filters
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
    # FTS5 tuning
    recall: int = 80,
    # Reranking
    rerank: str = "mmr",
    lambda_: float = 0.7,
    # Recency
    recency: bool = False,
    recency_half_life: float = 30.0,
    recency_max_boost: float = 1.15,
    # Threshold
    threshold: float = 0.0,
    # Backend
    backend: str | None = None,
    embed_backend: EmbeddingBackend | None = None,
) -> list[SearchChunk]:
    """Unified search pipeline — FTS5, semantic, or hybrid.

    Args:
        q: Search query string.
        db_path: Path to main database.
        embed_db: Path to embeddings database. Required for hybrid/semantic modes.
        n: Desired result count after all processing.
        mode: "hybrid" (FTS5 + semantic), "fts" (keyword only), "semantic" (embeddings only).
        rerank: "mmr" for diversity reranking, "relevance" for pure score order.
        backend: Preferred embedding backend name (ollama, fastembed).
        embed_backend: Injected embedding backend instance. If provided, skips
            get_backend() discovery. Must implement embed_one(text) -> list[float],
            and have .name, .model, .dimension attributes.

    Returns:
        List of SearchChunk results.

    Raises:
        FileNotFoundError: If database doesn't exist.
        ValueError: If query is empty or search fails.
        RuntimeError: If embedding backend unavailable.
    """
    from siftd.search import annotate_fts5_breakdown, mmr_rerank, resolve_candidates
    try:
        from siftd.search import MAX_MMR_CANDIDATES
    except ImportError:  # pragma: no cover
        MAX_MMR_CANDIDATES = 1000
    from siftd.storage.sqlite import open_database

    # --- FTS-only mode ---
    if mode == "fts":
        candidate_ids = resolve_candidates(
            db_path,
            workspace=workspace, model=model, since=since, before=before,
            tag=tag, all_tags=all_tags, no_tag=no_tag,
            exclude_active=exclude_active, include_derivative=include_derivative,
            owner=owner,
        )
        conn = open_database(db_path, read_only=True)
        try:
            raw = fts5_search_content(conn, q, limit=n * 5)
            if candidate_ids is not None:
                raw = [r for r in raw if r["conversation_id"] in candidate_ids]
            raw = raw[:n]
            return [
                SearchChunk(
                    conversation_id=r["conversation_id"],
                    score=abs(r["rank"]),
                    text=r["snippet"],
                    chunk_type=r["side"],
                    source_ids=[],
                    file_refs=[],
                )
                for r in raw
            ]
        finally:
            conn.close()

    # --- Hybrid / semantic modes need embeddings ---
    from siftd.paths import embeddings_db_path as default_embed_db
    from siftd.search import apply_temporal_weight

    effective_embed_db = embed_db or default_embed_db()

    if embed_backend is not None:
        _backend = embed_backend
        # Caller injected a backend — still need SCHEMA_VERSION for compat check
        try:
            from siftd.embeddings import SCHEMA_VERSION
        except ImportError:  # pragma: no cover
            # Allows unit tests to inject a backend without requiring the optional
            # [embed] extra to be installed.
            SCHEMA_VERSION = 1
    else:
        from siftd.embeddings import SCHEMA_VERSION, get_backend
        try:
            from siftd.embeddings import invalidate_backend_cache
        except ImportError:  # pragma: no cover
            def invalidate_backend_cache() -> None:
                return None

        def _resolve_backend() -> EmbeddingBackend:
            return get_backend(preferred=backend, verbose=False)

        _backend = _resolve_backend()

    embeddings_only = mode == "semantic"

    candidate_ids = resolve_candidates(
        db_path,
        workspace=workspace, model=model, since=since, before=before,
        tag=tag, all_tags=all_tags, no_tag=no_tag,
        exclude_active=exclude_active, include_derivative=include_derivative,
        owner=owner,
    )

    # FTS5 recall (hybrid mode only — narrows candidates before embeddings)
    fts5_ids: set[str] | None = None
    fts5_mode: str | None = None
    if not embeddings_only:
        conn = open_database(db_path, read_only=True)
        try:
            fts5_ids, fts5_mode = fts5_recall_conversations(conn, q, limit=recall)
        finally:
            conn.close()

        if fts5_ids:
            if candidate_ids is not None:
                intersected = fts5_ids & candidate_ids
                candidate_ids = intersected if intersected else candidate_ids
            else:
                candidate_ids = fts5_ids

    if candidate_ids is not None and not candidate_ids:
        return []

    # Embed query and search
    use_mmr = rerank == "mmr"
    try:
        query_embedding = _backend.embed_one(q)
    except (RuntimeError, ConnectionError, OSError):
        # Cached backend may have become unavailable (e.g., ollama stopped).
        # Invalidate and retry with fallback chain (production path only).
        if embed_backend is not None:
            raise
        invalidate_backend_cache()
        _backend = _resolve_backend()
        query_embedding = _backend.embed_one(q)
    embed_conn = open_embeddings_db(effective_embed_db, read_only=True)

    try:
        validate_index_compat(
            embed_conn,
            backend_name=_backend.name,
            backend_model=_backend.model,
            backend_dimension=_backend.dimension,
            current_schema_version=SCHEMA_VERSION,
        )

        # Widen for MMR to have candidates to diversify from
        search_limit = max(n * 3, n) if use_mmr else n

        results = search_similar(
            embed_conn,
            query_embedding,
            limit=search_limit,
            conversation_ids=candidate_ids,
            include_embeddings=use_mmr,
        )
    finally:
        embed_conn.close()

    if not results:
        return []

    # Mark FTS5 recall matches in breakdown
    annotate_fts5_breakdown(results, fts5_ids, fts5_mode)

    # Temporal weighting (before MMR so it affects reranking)
    if recency and results:
        conv_ids_for_ts = list({r["conversation_id"] for r in results})
        ts_conn = open_database(db_path, read_only=True)
        try:
            timestamps = fetch_conversation_timestamps(ts_conn, conv_ids_for_ts)
        finally:
            ts_conn.close()
        results = apply_temporal_weight(
            results, timestamps,
            half_life_days=recency_half_life, max_boost=recency_max_boost,
        )
        # Re-sort by weighted score (MMR does its own reranking).
        # Use chunk_id as deterministic tie-breaker (ULIDs sort by creation time).
        if not use_mmr:
            results = sorted(results, key=lambda r: (-r["score"], r.get("chunk_id", "")))

    # MMR diversity reranking
    if use_mmr and results:
        # Cap candidates to prevent unbounded memory usage in np.vstack inside mmr_rerank().
        if len(results) > MAX_MMR_CANDIDATES:
            results = sorted(results, key=lambda r: -r["score"])[:MAX_MMR_CANDIDATES]
        results = mmr_rerank(results, query_embedding, lambda_=lambda_, limit=n)
        # Ensure outward score matches MMR-adjusted final score for display and downstream sorting.
        for r in results:
            breakdown = r.get("breakdown")
            final_score = getattr(breakdown, "final_score", None)
            if final_score is not None:
                r["score"] = float(final_score)

    # Score threshold filtering
    if threshold > 0:
        results = [r for r in results if r.get("score", 0) >= threshold]

    return [SearchChunk.from_mapping(r) for r in results]
