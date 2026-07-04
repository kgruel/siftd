"""Public search API for programmatic access by agent harnesses."""

import sys
from datetime import UTC, datetime
from pathlib import Path

from siftd.domain.search_types import ScoreBreakdown, SearchChunk
from siftd.storage.filters import WhereBuilder
from siftd.storage.sql_helpers import batched_in_query, has_conversation_owners_table
from siftd.storage.sqlite import open_database

# Hard cap on MMR candidates to prevent unbounded memory usage.
# 1000 vectors * 1536 dims * 4 bytes = ~6MB — safe for all systems.
MAX_MMR_CANDIDATES = 1000


def apply_temporal_weight(
    results: list[dict],
    timestamps: dict[str, str],
    *,
    half_life_days: float = 30.0,
    max_boost: float = 1.15,
) -> list[dict]:
    """Apply temporal weighting to boost recent results.

    Uses a mild exponential decay that:
    - Boosts recent results (up to max_boost for today's results)
    - Gently decays older results (half-life controls decay rate)
    - Never penalizes old results below their original score

    The decay formula: weight = 1 + (max_boost - 1) * exp(-days_ago * ln(2) / half_life)

    At days_ago=0: weight = max_boost (e.g., 1.15 = 15% boost)
    At days_ago=half_life: weight ≈ 1 + (max_boost-1)/2 (half the boost)
    As days_ago→∞: weight → 1.0 (no penalty, just no boost)

    Args:
        results: List of result dicts with 'conversation_id' and 'score'.
            If results have 'breakdown' (ScoreBreakdown), it will be updated.
        timestamps: Dict mapping conversation_id to ISO timestamp string.
        half_life_days: Days until boost decays to half. Default 30.
        max_boost: Maximum boost multiplier for today's results. Default 1.15.

    Returns:
        Results with adjusted 'score' values (original list is not modified).
        ScoreBreakdown.recency_boost is updated if present.
    """
    if not results or max_boost <= 1.0:
        return results

    import math

    now = datetime.now(UTC)
    if half_life_days <= 0:
        return results
    decay_constant = math.log(2) / half_life_days

    weighted = []
    for r in results:
        r_copy = dict(r)
        conv_id = r["conversation_id"]
        ts_str = timestamps.get(conv_id, "")
        weight = 1.0

        if ts_str:
            try:
                # Parse ISO timestamp (with or without timezone)
                ts_str_clean = ts_str.replace("Z", "+00:00")
                if "+" not in ts_str_clean and ts_str_clean.count("-") <= 2:
                    # No timezone, assume UTC (handles legacy data from pre-fix versions)
                    ts = datetime.fromisoformat(ts_str_clean).replace(tzinfo=UTC)
                else:
                    ts = datetime.fromisoformat(ts_str_clean)
                days_ago = max(0, (now - ts).total_seconds() / 86400)
                # Exponential decay: starts at max_boost, decays to 1.0
                weight = 1.0 + (max_boost - 1.0) * math.exp(-decay_constant * days_ago)
                r_copy["score"] = r["score"] * weight
            except (ValueError, TypeError):
                pass  # Keep original score if timestamp parsing fails

        # Update breakdown if present
        if "breakdown" in r_copy and isinstance(r_copy["breakdown"], ScoreBreakdown):
            breakdown = r_copy["breakdown"]
            breakdown.recency_boost = float(weight)
            breakdown.pre_mmr_score = breakdown.embedding_sim * breakdown.recency_boost
            breakdown.final_score = breakdown.pre_mmr_score

        weighted.append(r_copy)

    return weighted

# Backward-compatible alias for callers importing SearchResult from siftd.search.
SearchResult = SearchChunk


def annotate_fts5_breakdown(
    results: list[dict],
    fts5_ids: set[str] | None,
    fts5_mode: str | None,
) -> None:
    """Annotate ScoreBreakdown objects with FTS5 recall match info.

    Mutates results in-place. Each result with a ScoreBreakdown gets
    fts5_matched set to True/False and fts5_mode set accordingly.

    Args:
        results: List of result dicts with optional 'breakdown' key.
        fts5_ids: Set of conversation IDs that matched FTS5 recall, or None.
        fts5_mode: FTS5 match mode ("and", "or"), or None.
    """
    if fts5_ids is None:
        return

    for r in results:
        breakdown = r.get("breakdown")
        if breakdown and isinstance(breakdown, ScoreBreakdown):
            matched = r["conversation_id"] in fts5_ids
            breakdown.fts5_matched = matched
            breakdown.fts5_mode = fts5_mode if matched else None


def mmr_rerank(
    results: list[dict],
    query_embedding: list[float],
    *,
    lambda_: float = 0.7,
    limit: int = 10,
) -> list[dict]:
    """Rerank results using Maximal Marginal Relevance with conversation-level penalty.

    Two-tier penalty:
    1. If a chunk's conversation is already in the selected set, penalty = 1.0
       (hard suppress same-conversation duplicates).
    2. Otherwise, penalty = max cosine similarity between this chunk's embedding
       and any already-selected chunk's embedding (standard MMR diversity).

    Each result dict must include 'embedding' and 'score' keys.

    Args:
        results: Candidate chunks with 'embedding', 'score', 'conversation_id'.
        query_embedding: The query's embedding vector.
        lambda_: Balance between relevance (1.0) and diversity (0.0). Default 0.7.
        limit: Number of results to select.

    Returns:
        Selected results in MMR rank order (without 'embedding' key).
    """
    if not results:
        return []

    try:
        import numpy as np
    except ImportError:
        return _mmr_rerank_python(results, lambda_=lambda_, limit=limit)

    n = len(results)

    # Pre-compute embeddings matrix for vectorized similarity
    # Embeddings may be numpy arrays or lists; stack them
    embeddings = np.vstack([
        r["embedding"] if isinstance(r["embedding"], np.ndarray) else np.asarray(r["embedding"], dtype=np.float32)
        for r in results
    ])

    # Normalize all embeddings once for faster dot products
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    embeddings_normalized = embeddings / norms

    # Pre-compute relevance scores
    relevances = np.array([r["score"] for r in results], dtype=np.float32)
    conv_ids = [r["conversation_id"] for r in results]

    remaining = set(range(n))
    selected: list[int] = []
    selected_convs: set[str] = set()
    chunk_ids = [r.get("chunk_id", "") for r in results]

    # Track max similarity to selected set for each candidate
    max_sim_to_selected = np.zeros(n, dtype=np.float32)

    while remaining and len(selected) < limit:
        best_idx = -1
        best_score = float("-inf")
        best_chunk_id = ""

        for idx in remaining:
            conv_id = conv_ids[idx]
            if conv_id in selected_convs:
                penalty = 1.0
            elif selected:
                penalty = float(max_sim_to_selected[idx])
            else:
                penalty = 0.0

            mmr_score = lambda_ * relevances[idx] - (1 - lambda_) * penalty
            chunk_id = chunk_ids[idx]
            # Deterministic tie-breaker: use chunk_id (ULIDs sort by creation time)
            if (mmr_score, chunk_id) > (best_score, best_chunk_id):
                best_score = mmr_score
                best_idx = idx
                best_chunk_id = chunk_id

        remaining.remove(best_idx)
        selected.append(best_idx)
        selected_convs.add(conv_ids[best_idx])

        # Update max similarities: compute similarity of all remaining to newly selected
        if remaining:
            new_vec = embeddings_normalized[best_idx]
            for idx in remaining:
                sim = float(np.dot(embeddings_normalized[idx], new_vec))
                if sim > max_sim_to_selected[idx]:
                    max_sim_to_selected[idx] = sim

    # Track penalty at selection time for breakdown
    penalties: dict[int, float] = {}
    for rank, idx in enumerate(selected):
        # Recompute penalty for this result at selection time
        conv_id = conv_ids[idx]
        convs_before = set(conv_ids[i] for i in selected[:rank])
        if conv_id in convs_before:
            penalties[idx] = 1.0
        elif rank == 0:
            penalties[idx] = 0.0
        else:
            # Approximate: use current max_sim value (slightly conservative)
            penalties[idx] = float(max_sim_to_selected[idx])

    # Return selected results without embedding key, with MMR breakdown
    reranked = []
    for rank, idx in enumerate(selected):
        r = dict(results[idx])
        r.pop("embedding", None)

        # Update breakdown if present
        if "breakdown" in r and isinstance(r["breakdown"], ScoreBreakdown):
            breakdown = r["breakdown"]
            breakdown.mmr_penalty = penalties.get(idx, 0.0)
            breakdown.mmr_rank = rank + 1  # 1-indexed rank
            # Final score after MMR: pre_mmr - (1-lambda)*penalty
            breakdown.final_score = (
                lambda_ * (breakdown.pre_mmr_score or breakdown.embedding_sim)
                - (1 - lambda_) * breakdown.mmr_penalty
            )

        reranked.append(r)
    return reranked


def _mmr_rerank_python(
    results: list[dict],
    *,
    lambda_: float,
    limit: int,
) -> list[dict]:
    """Pure-Python MMR fallback when numpy cannot be imported."""
    import math

    def _vec(raw):
        if hasattr(raw, "tolist"):
            raw = raw.tolist()
        return [float(x) for x in raw]

    def _norm(vec):
        return math.sqrt(sum(x * x for x in vec))

    def _cos(a, b):
        na = _norm(a)
        nb = _norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return sum(x * y for x, y in zip(a, b)) / (na * nb)

    embeddings = [_vec(r["embedding"]) for r in results]
    relevances = [float(r["score"]) for r in results]
    conv_ids = [r["conversation_id"] for r in results]
    chunk_ids = [r.get("chunk_id", "") for r in results]

    remaining = set(range(len(results)))
    selected: list[int] = []
    selected_convs: set[str] = set()

    while remaining and len(selected) < limit:
        best_idx = -1
        best_score = float("-inf")
        best_chunk_id = ""

        for idx in remaining:
            conv_id = conv_ids[idx]
            if conv_id in selected_convs:
                penalty = 1.0
            elif selected:
                penalty = max(_cos(embeddings[idx], embeddings[j]) for j in selected)
            else:
                penalty = 0.0

            mmr_score = lambda_ * relevances[idx] - (1 - lambda_) * penalty
            cid = chunk_ids[idx]
            if (mmr_score, cid) > (best_score, best_chunk_id):
                best_score = mmr_score
                best_idx = idx
                best_chunk_id = cid

        remaining.remove(best_idx)
        selected.append(best_idx)
        selected_convs.add(conv_ids[best_idx])

    reranked: list[dict] = []
    for rank, idx in enumerate(selected):
        r = dict(results[idx])
        r.pop("embedding", None)

        breakdown = r.get("breakdown")
        if breakdown and isinstance(breakdown, ScoreBreakdown):
            conv_id = conv_ids[idx]
            convs_before = {conv_ids[i] for i in selected[:rank]}
            if conv_id in convs_before:
                penalty = 1.0
            elif rank == 0:
                penalty = 0.0
            else:
                penalty = max(_cos(embeddings[idx], embeddings[i]) for i in selected[:rank])
            breakdown.mmr_penalty = penalty
            breakdown.mmr_rank = rank + 1
            breakdown.final_score = (
                lambda_ * (breakdown.pre_mmr_score or breakdown.embedding_sim)
                - (1 - lambda_) * penalty
            )

        reranked.append(r)

    return reranked


def hybrid_search(
    q: str,
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
    n: int = 10,
    recall: int = 80,
    embeddings_only: bool = False,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    include_derivative: bool = False,
    backend: str | None = None,
    exclude_active: bool = True,
    rerank: str = "mmr",
    lambda_: float = 0.7,
    recency: bool = False,
    recency_half_life: float = 30.0,
    recency_max_boost: float = 1.15,
    threshold: float = 0.0,
    fts5_passthrough: bool = False,
) -> list[SearchResult]:
    """Run hybrid FTS5+embeddings search, return structured results.

    Args:
        q: The search query string.
        db_path: Path to main SQLite DB. Defaults to XDG data path.
        embed_db_path: Path to embeddings DB. Defaults to XDG data path.
        n: Maximum number of results to return.
        recall: Number of FTS5 candidate conversations for hybrid recall.
        embeddings_only: Skip FTS5 recall, search all embeddings directly.
        workspace: Filter to conversations from workspaces matching this substring.
        model: Filter to conversations using models matching this substring.
        since: Filter to conversations started at or after this ISO date.
        before: Filter to conversations started before this ISO date.
        tag: OR filter — conversations with any of these tags.
        all_tags: AND filter — conversations with all of these tags.
        no_tag: NOT filter — exclude conversations with any of these tags.
        include_derivative: Include derivative conversations (default False).
        backend: Preferred embedding backend name (transitional --backend override).
        exclude_active: Auto-exclude conversations from active sessions (default True).
        rerank: Reranking strategy — "mmr" for diversity or "relevance" for pure similarity.
        lambda_: MMR balance between relevance (1.0) and diversity (0.0). Default 0.7.
        recency: Enable temporal weighting to boost recent results. Default False.
        recency_half_life: Days until recency boost decays to half. Default 30.
        recency_max_boost: Maximum boost for today's results (e.g., 1.15 = 15%). Default 1.15.
        fts5_passthrough: If True, allow an FTS5-only fast-path when FTS5 can
            supply at least `n` results (after filters). This is an opt-in
            escape hatch for structured identifier queries where semantic reranking
            may hurt exact-match relevance. Default False.

    Returns:
        List of SearchResult ordered by reranking strategy.

    Raises:
        FileNotFoundError: If the database files don't exist.
        RuntimeError: If no embedding backend is available.
        EmbeddingsNotAvailable: If embedding dependencies are not installed.
    """
    from siftd.embeddings import require_embeddings

    require_embeddings("Semantic search")

    from siftd.embeddings.base import get_backend
    from siftd.embeddings.indexer import SCHEMA_VERSION
    from siftd.paths import db_path as default_db_path
    from siftd.paths import embeddings_db_path as default_embed_path
    from siftd.storage.embeddings import (
        IndexCompatError,
        open_embeddings_db,
        search_similar,
        validate_index_compat,
    )
    from siftd.storage.fts import fts5_best_hit_for_conversation, fts5_recall_details
    from siftd.storage.tags import DERIVATIVE_TAG

    db = db_path if db_path is not None else default_db_path()
    embed_db = embed_db_path if embed_db_path is not None else default_embed_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")
    if not embed_db.exists():
        raise FileNotFoundError(f"Embeddings database not found: {embed_db}")

    # Build candidate filter set and active exclusion set
    excluded: set[str] = set()
    candidate_ids: set[str] | None = None

    exclude_tags_final = list(no_tag or [])
    if not include_derivative and DERIVATIVE_TAG not in exclude_tags_final:
        exclude_tags_final.append(DERIVATIVE_TAG)

    if exclude_active:
        excluded = get_active_conversation_ids(db)

    main_conn = open_database(db, read_only=True)
    fts5_ids: set[str] | None = None
    fts5_mode: str | None = None
    fts5_query: str | None = None
    fts5_ordered: list[str] | None = None
    try:
        candidate_ids = _filter_conversations_conn(
            main_conn,
            workspace=workspace,
            model=model,
            since=since,
            before=before,
            tags=tag,
            all_tags=all_tags,
            exclude_tags=exclude_tags_final or None,
            tag_kind=tag_kind,
        )

        # Pre-filter active sessions when we have an explicit candidate set
        if excluded and candidate_ids is not None:
            candidate_ids = candidate_ids - excluded

        # Hybrid recall: FTS5 narrows candidates, embeddings rerank
        if not embeddings_only:
            recall_details = fts5_recall_details(main_conn, q, limit=recall)
            fts5_ordered = recall_details.conversation_ids
            fts5_ids = set(fts5_ordered)
            fts5_mode = recall_details.mode
            fts5_query = recall_details.fts_query

            if fts5_ids:
                # Remove active sessions from FTS5 recall set
                if excluded:
                    fts5_ids = fts5_ids - excluded
                if candidate_ids is not None:
                    intersected = fts5_ids & candidate_ids
                    candidate_ids = intersected if intersected else candidate_ids
                else:
                    candidate_ids = fts5_ids
    finally:
        main_conn.close()

    # Optional FTS5-only passthrough for exact-match structured queries.
    # Default remains pure hybrid (FTS5 recall -> embeddings scoring -> reranking).
    if (
        fts5_passthrough
        and not embeddings_only
        and fts5_query
        and fts5_ordered
        and fts5_mode in {"and", "or"}
    ):
        passthrough_ids: list[str] = []
        for conv_id in fts5_ordered:
            if excluded and conv_id in excluded:
                continue
            if candidate_ids is not None and conv_id not in candidate_ids:
                continue
            passthrough_ids.append(conv_id)
            if len(passthrough_ids) >= n:
                break

        if len(passthrough_ids) >= n:
            main_conn_fts = open_database(db, read_only=True)
            try:
                raw_results: list[dict] = []
                for conv_id in passthrough_ids[:n]:
                    hit = fts5_best_hit_for_conversation(main_conn_fts, fts5_query, conversation_id=conv_id)
                    snippet = hit["snippet"] if hit else ""
                    rank = float(hit["rank"]) if hit and hit.get("rank") is not None else 0.0
                    score = -rank
                    raw_results.append(
                        {
                            "conversation_id": conv_id,
                            "score": score,
                            "text": snippet,
                            "chunk_type": "fts5",
                            "chunk_id": None,
                            "source_ids": [],
                            "breakdown": ScoreBreakdown(
                                embedding_sim=0.0,
                                pre_mmr_score=score,
                                final_score=score,
                                fts5_matched=True,
                                fts5_mode=fts5_mode,
                            ),
                        }
                    )

                meta_rows = batched_in_query(
                    main_conn_fts,
                    "SELECT c.id, c.started_at, w.path AS workspace FROM conversations c "
                    "LEFT JOIN workspaces w ON w.id = c.workspace_id "
                    "WHERE c.id IN ({placeholders})",
                    passthrough_ids[:n],
                )
                meta = {row["id"]: dict(row) for row in meta_rows}
            finally:
                main_conn_fts.close()

            results: list[SearchResult] = []
            for r in raw_results:
                conv_id = r["conversation_id"]
                m = meta.get(conv_id, {})
                breakdown = r.get("breakdown")
                breakdown_dict = breakdown.to_dict() if breakdown and isinstance(breakdown, ScoreBreakdown) else None
                results.append(
                    SearchResult(
                        conversation_id=conv_id,
                        score=r["score"],
                        text=r["text"],
                        chunk_type=r["chunk_type"],
                        workspace_path=m.get("workspace"),
                        started_at=m.get("started_at"),
                        chunk_id=None,
                        source_ids=[],
                        breakdown=breakdown_dict,
                    )
                )

            return results

    # Embed query and search
    use_mmr = rerank == "mmr"
    embed_backend = get_backend(preferred=backend, verbose=False)
    try:
        query_embedding = embed_backend.embed_query(q)
    except (RuntimeError, ConnectionError, OSError):
        # Cached backend may have become unavailable (e.g., a remote endpoint went down).
        # Invalidate and re-resolve, then retry once.
        from siftd.embeddings.base import invalidate_backend_cache

        invalidate_backend_cache()
        embed_backend = get_backend(preferred=backend, verbose=False)
        query_embedding = embed_backend.embed_query(q)

    # Fetch wider candidate set for MMR to select from
    search_limit = n * 3 if use_mmr else n

    # Pass excluded conversations to search_similar for score masking —
    # this guarantees they never appear in results regardless of chunk count.
    exclude_from_search = excluded if (excluded and candidate_ids is None) else None

    embed_conn = open_embeddings_db(embed_db, read_only=True)
    try:
        try:
            validate_index_compat(
                embed_conn,
                backend_name=embed_backend.name,
                backend_model=embed_backend.model,
                backend_dimension=embed_backend.dimension,
                current_schema_version=SCHEMA_VERSION,
            )
        except IndexCompatError:
            # Match CLI behavior: actionable error instead of silently returning wrong results.
            raise

        raw_results = search_similar(
            embed_conn,
            query_embedding,
            limit=search_limit,
            conversation_ids=candidate_ids,
            include_embeddings=use_mmr,
            exclude_conversation_ids=exclude_from_search,
        )
    finally:
        embed_conn.close()

    if not raw_results:
        return []

    # Annotate breakdown with FTS5 recall info (used by JSON output explainability)
    annotate_fts5_breakdown(raw_results, fts5_ids, fts5_mode)

    # Apply temporal weighting if requested (before MMR so it affects reranking)
    if recency:
        from siftd.storage.queries import fetch_conversation_timestamps

        conv_ids_for_ts = list({r["conversation_id"] for r in raw_results})
        main_conn_ts = open_database(db, read_only=True)
        try:
            timestamps = fetch_conversation_timestamps(main_conn_ts, conv_ids_for_ts)
        finally:
            main_conn_ts.close()

        raw_results = apply_temporal_weight(
            raw_results,
            timestamps,
            half_life_days=recency_half_life,
            max_boost=recency_max_boost,
        )
        # Re-sort by weighted score (MMR does its own reranking)
        # Use chunk_id as deterministic tie-breaker (ULIDs sort by creation time)
        if not use_mmr:
            raw_results = sorted(raw_results, key=lambda r: (-r["score"], r.get("chunk_id", "")))

    # Apply MMR reranking if requested
    if use_mmr:
        # Cap candidates to prevent unbounded memory in np.vstack
        if len(raw_results) > MAX_MMR_CANDIDATES:
            print(
                f"Warning: Capping MMR candidates from {len(raw_results)} to {MAX_MMR_CANDIDATES}",
                file=sys.stderr,
            )
            # Keep top candidates by score
            raw_results = sorted(raw_results, key=lambda r: -r["score"])[:MAX_MMR_CANDIDATES]

        raw_results = mmr_rerank(
            raw_results,
            query_embedding,
            lambda_=lambda_,
            limit=n,
        )
        # Ensure outward score matches MMR-adjusted final score for display and downstream sorting.
        for r in raw_results:
            breakdown = r.get("breakdown")
            final_score = getattr(breakdown, "final_score", None)
            if final_score is not None:
                r["score"] = float(final_score)

    # Enrich with metadata from main DB
    main_conn_meta = open_database(db, read_only=True)
    try:
        conv_ids = list({r["conversation_id"] for r in raw_results})
        meta_rows = batched_in_query(
            main_conn_meta,
            "SELECT c.id, c.started_at, w.path AS workspace FROM conversations c "
            "LEFT JOIN workspaces w ON w.id = c.workspace_id "
            "WHERE c.id IN ({placeholders})",
            conv_ids,
        )
    finally:
        main_conn_meta.close()
    meta = {row["id"]: dict(row) for row in meta_rows}

    results = []
    for r in raw_results:
        conv_id = r["conversation_id"]
        m = meta.get(conv_id, {})
        breakdown = r.get("breakdown")
        breakdown_dict = breakdown.to_dict() if breakdown and isinstance(breakdown, ScoreBreakdown) else None
        results.append(SearchResult(
            conversation_id=conv_id,
            score=r["score"],
            text=r["text"],
            chunk_type=r["chunk_type"],
            workspace_path=m.get("workspace"),
            started_at=m.get("started_at"),
            chunk_id=r.get("chunk_id"),
            source_ids=r.get("source_ids") or [],
            breakdown=breakdown_dict,
        ))

    if threshold > 0:
        results = [r for r in results if r.score >= threshold]

    return results


def filter_conversations(
    db: Path,
    *,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tags: list[str] | None = None,
    all_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    owner: str | None = None,
    tag_kind: list[str] | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
) -> set[str] | None:
    """Apply filters and return candidate conversation IDs.

    Returns None if no filters are applied (search all conversations).

    Args:
        db: Path to the database.
        workspace: Filter by workspace path substring.
        model: Filter by model name substring.
        since: Filter conversations started at or after this date.
        before: Filter conversations started before this date.
        tags: OR filter — conversations with any of these tags.
        all_tags: AND filter — conversations with all of these tags.
        exclude_tags: NOT filter — exclude conversations with any of these tags.
        owner: Filter to conversations owned by this user_id.
        tag_kind: Scope tag matching to specific target_kinds. Defaults to all.
        tool: Filter to conversations with a tool_call of this canonical name.
        tool_tag: Filter to conversations with a tool_call carrying this tag.

    Returns:
        Set of conversation IDs matching filters, or None if no filters.
    """
    if not any(
        [workspace, model, since, before, tags, all_tags, exclude_tags, owner,
         tool, tool_tag]
    ):
        return None

    conn = open_database(db, read_only=True)
    try:
        return _filter_conversations_conn(
            conn, workspace=workspace, model=model, since=since, before=before,
            tags=tags, all_tags=all_tags, exclude_tags=exclude_tags, owner=owner,
            tag_kind=tag_kind, tool=tool, tool_tag=tool_tag,
        )
    finally:
        conn.close()


def _filter_conversations_conn(
    conn,
    *,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tags: list[str] | None = None,
    all_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    owner: str | None = None,
    tag_kind: list[str] | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
) -> set[str] | None:
    """Internal: filter conversations using an existing connection."""
    if not any(
        [workspace, model, since, before, tags, all_tags, exclude_tags, owner,
         tool, tool_tag]
    ):
        return None

    if owner and not has_conversation_owners_table(conn):
        return set()

    wb = WhereBuilder()
    wb.workspace(workspace)
    wb.model(model)
    wb.since(since)
    wb.before(before)
    wb.owner(owner)
    wb.tool(tool)
    wb.tool_tag(tool_tag)
    wb.tags_any(tags, kinds=tag_kind)
    wb.tags_all(all_tags, kinds=tag_kind)
    wb.tags_none(exclude_tags, kinds=tag_kind)

    joins = wb.joins_sql()
    joins_clause = f"\n        {joins}" if joins else ""

    sql = f"""
        SELECT c.id
        FROM conversations c{joins_clause}
        {wb.where_sql()}
    """

    rows = conn.execute(sql, wb.params).fetchall()
    return {row["id"] for row in rows}


_active_ids_cache: tuple[float, Path, set[str]] | None = None
_ACTIVE_IDS_TTL = 5.0  # seconds — short enough to track session changes, long enough to help batch use


def get_active_conversation_ids(db: Path) -> set[str]:
    """Get conversation IDs that originated from currently-active session files.

    Uses list_active_sessions() from the peek module to find active JSONL files,
    then looks up which ingested conversations came from those file paths.

    Results are cached for 5 seconds to avoid repeated filesystem scans
    when called in tight loops (e.g., batch search).

    Args:
        db: Path to the main database.

    Returns:
        Set of conversation IDs to exclude (may be empty).
    """
    import time as _time

    global _active_ids_cache
    now = _time.monotonic()
    if _active_ids_cache is not None:
        cached_time, cached_db, cached_ids = _active_ids_cache
        if cached_db == db and (now - cached_time) < _ACTIVE_IDS_TTL:
            return cached_ids

    try:
        from siftd.peek.scanner import list_active_sessions
    except ImportError:
        _active_ids_cache = (now, db, set())
        return set()

    try:
        sessions = list_active_sessions(include_inactive=False)
    except Exception:
        # Filesystem scan failed — don't block search
        _active_ids_cache = (now, db, set())
        return set()

    if not sessions:
        _active_ids_cache = (now, db, set())
        return set()

    file_paths = [str(s.file_path) for s in sessions]

    conn = open_database(db, read_only=True)
    try:
        rows = batched_in_query(
            conn,
            "SELECT conversation_id FROM ingested_files WHERE path IN ({placeholders}) AND conversation_id IS NOT NULL",
            file_paths,
        )
    finally:
        conn.close()

    result = {row["conversation_id"] for row in rows}
    _active_ids_cache = (now, db, result)
    return result


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
    tag_kind: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
) -> set[str] | None:
    """Resolve candidate conversation IDs from filters + scope options.

    Composes filter_conversations() with active-session exclusion and
    the derivative-tag default. Returns None if no constraints apply
    (search all conversations).
    """
    from siftd.storage.tags import DERIVATIVE_TAG

    effective_exclude = list(no_tag or [])
    if not include_derivative:
        effective_exclude.append(DERIVATIVE_TAG)

    candidate_ids = filter_conversations(
        db,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        tags=tag,
        all_tags=all_tags,
        exclude_tags=effective_exclude or None,
        owner=owner,
        tag_kind=tag_kind,
        tool=tool,
        tool_tag=tool_tag,
    )

    if exclude_active:
        active_ids = get_active_conversation_ids(db)
        if active_ids:
            if candidate_ids is not None:
                candidate_ids = candidate_ids - active_ids
            else:
                from siftd.storage.queries import fetch_all_conversation_ids

                conn = open_database(db, read_only=True)
                all_ids = set(fetch_all_conversation_ids(conn))
                conn.close()
                candidate_ids = all_ids - active_ids

    return candidate_ids
