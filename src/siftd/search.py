"""Shared search primitives — candidate resolution, MMR reranking, temporal
weighting, and FTS5 breakdown annotation — consumed by api/search.py, which owns
the engine entrypoint. No search Operation lives here."""

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

    import numpy as np  # base dependency

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
