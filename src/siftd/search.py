"""Public search API for programmatic access by agent harnesses."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from siftd.math import cosine_similarity as _cosine_sim
from siftd.storage.filters import WhereBuilder


@dataclass
class SearchResult:
    """A single search result from hybrid_search."""

    conversation_id: str
    score: float
    text: str
    chunk_type: str
    workspace_path: str | None
    started_at: str | None
    chunk_id: str | None = None
    source_ids: list[str] | None = None


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

    remaining = list(range(len(results)))
    selected: list[int] = []
    selected_convs: set[str] = set()

    while remaining and len(selected) < limit:
        best_idx = -1
        best_score = float("-inf")

        for idx in remaining:
            r = results[idx]
            relevance = r["score"]

            conv_id = r["conversation_id"]
            if conv_id in selected_convs:
                penalty = 1.0
            elif selected:
                penalty = max(
                    _cosine_sim(r["embedding"], results[s]["embedding"])
                    for s in selected
                )
            else:
                penalty = 0.0

            mmr_score = lambda_ * relevance - (1 - lambda_) * penalty
            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = idx

        remaining.remove(best_idx)
        selected.append(best_idx)
        selected_convs.add(results[best_idx]["conversation_id"])

    # Return selected results without embedding key
    reranked = []
    for idx in selected:
        r = dict(results[idx])
        r.pop("embedding", None)
        reranked.append(r)
    return reranked


def hybrid_search(
    query: str,
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
    limit: int = 10,
    recall: int = 80,
    embeddings_only: bool = False,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    backend: str | None = None,
    exclude_active: bool = True,
    rerank: str = "mmr",
    lambda_: float = 0.7,
) -> list[SearchResult]:
    """Run hybrid FTS5+embeddings search, return structured results.

    Args:
        query: The search query string.
        db_path: Path to main SQLite DB. Defaults to XDG data path.
        embed_db_path: Path to embeddings DB. Defaults to XDG data path.
        limit: Maximum number of results to return.
        recall: Number of FTS5 candidate conversations for hybrid recall.
        embeddings_only: Skip FTS5 recall, search all embeddings directly.
        workspace: Filter to conversations from workspaces matching this substring.
        model: Filter to conversations using models matching this substring.
        since: Filter to conversations started at or after this ISO date.
        before: Filter to conversations started before this ISO date.
        backend: Preferred embedding backend name (ollama, fastembed).
        exclude_active: Auto-exclude conversations from active sessions (default True).
        rerank: Reranking strategy — "mmr" for diversity or "relevance" for pure similarity.
        lambda_: MMR balance between relevance (1.0) and diversity (0.0). Default 0.7.

    Returns:
        List of SearchResult ordered by reranking strategy.

    Raises:
        FileNotFoundError: If the database files don't exist.
        RuntimeError: If no embedding backend is available.
        EmbeddingsNotAvailable: If embedding dependencies are not installed.
    """
    from siftd.embeddings import require_embeddings

    require_embeddings("Semantic search")

    from siftd.embeddings import get_backend
    from siftd.paths import db_path as default_db_path
    from siftd.paths import embeddings_db_path as default_embed_path
    from siftd.storage.embeddings import open_embeddings_db, search_similar
    from siftd.storage.fts import fts5_recall_conversations

    db = db_path if db_path is not None else default_db_path()
    embed_db = embed_db_path if embed_db_path is not None else default_embed_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")
    if not embed_db.exists():
        raise FileNotFoundError(f"Embeddings database not found: {embed_db}")

    # Build candidate filter set
    candidate_ids = filter_conversations(db, workspace=workspace, model=model, since=since, before=before)

    # Exclude conversations from active sessions
    if exclude_active:
        excluded = get_active_conversation_ids(db)
        if excluded:
            if candidate_ids is not None:
                candidate_ids = candidate_ids - excluded
            else:
                # Need to get all conversation IDs minus excluded
                conn_tmp = sqlite3.connect(db)
                conn_tmp.row_factory = sqlite3.Row
                all_ids = {
                    row["id"]
                    for row in conn_tmp.execute("SELECT id FROM conversations").fetchall()
                }
                conn_tmp.close()
                candidate_ids = all_ids - excluded

    # Hybrid recall: FTS5 narrows candidates, embeddings rerank
    if not embeddings_only:
        main_conn = sqlite3.connect(db)
        main_conn.row_factory = sqlite3.Row
        fts5_ids, _fts5_mode = fts5_recall_conversations(main_conn, query, limit=recall)
        main_conn.close()

        if fts5_ids:
            if candidate_ids is not None:
                intersected = fts5_ids & candidate_ids
                candidate_ids = intersected if intersected else candidate_ids
            else:
                candidate_ids = fts5_ids

    # Embed query and search
    use_mmr = rerank == "mmr"
    embed_backend = get_backend(preferred=backend, verbose=False)
    query_embedding = embed_backend.embed_one(query)

    # Fetch wider candidate set for MMR to select from
    search_limit = limit * 3 if use_mmr else limit

    embed_conn = open_embeddings_db(embed_db, read_only=True)
    raw_results = search_similar(
        embed_conn,
        query_embedding,
        limit=search_limit,
        conversation_ids=candidate_ids,
        include_embeddings=use_mmr,
    )
    embed_conn.close()

    if not raw_results:
        return []

    # Apply MMR reranking if requested
    if use_mmr:
        raw_results = mmr_rerank(
            raw_results,
            query_embedding,
            lambda_=lambda_,
            limit=limit,
        )

    # Enrich with metadata from main DB
    main_conn = sqlite3.connect(db)
    main_conn.row_factory = sqlite3.Row
    conv_ids = list({r["conversation_id"] for r in raw_results})
    placeholders = ",".join("?" * len(conv_ids))
    meta_rows = main_conn.execute(
        f"SELECT c.id, c.started_at, w.path AS workspace FROM conversations c "
        f"LEFT JOIN workspaces w ON w.id = c.workspace_id "
        f"WHERE c.id IN ({placeholders})",
        conv_ids,
    ).fetchall()
    main_conn.close()
    meta = {row["id"]: dict(row) for row in meta_rows}

    results = []
    for r in raw_results:
        conv_id = r["conversation_id"]
        m = meta.get(conv_id, {})
        results.append(SearchResult(
            conversation_id=conv_id,
            score=r["score"],
            text=r["text"],
            chunk_type=r["chunk_type"],
            workspace_path=m.get("workspace"),
            started_at=m.get("started_at"),
            chunk_id=r.get("chunk_id"),
            source_ids=r.get("source_ids"),
        ))

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

    Returns:
        Set of conversation IDs matching filters, or None if no filters.
    """
    if not any([workspace, model, since, before, tags, all_tags, exclude_tags]):
        return None

    wb = WhereBuilder()
    wb.workspace(workspace)
    wb.model(model)
    wb.since(since)
    wb.before(before)
    wb.tags_any(tags)
    wb.tags_all(all_tags)
    wb.tags_none(exclude_tags)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    sql = f"""
        SELECT DISTINCT c.id
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        LEFT JOIN responses r ON r.conversation_id = c.id
        LEFT JOIN models m ON m.id = r.model_id
        {wb.where_sql()}
    """

    rows = conn.execute(sql, wb.params).fetchall()
    conn.close()
    return {row["id"] for row in rows}


def resolve_role_ids(
    db: Path,
    role: str,
    candidate_ids: set[str] | None = None,
) -> set[str] | None:
    """Resolve source IDs for a given role within optional conversation set.

    For 'user': returns prompt IDs (prompts are user messages).
    For 'assistant': returns prompt IDs whose responses contain assistant content
    (chunks reference the prompt_id that triggered the response).

    Args:
        db: Path to the database.
        role: Either 'user' or 'assistant'.
        candidate_ids: Optional set of conversation IDs to filter within.

    Returns:
        Set of prompt IDs for the role, or None if no matches.
    """
    if candidate_ids is not None and not candidate_ids:
        return None

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    if candidate_ids is not None:
        placeholders = ",".join("?" * len(candidate_ids))
        conv_filter = f"AND p.conversation_id IN ({placeholders})"
        params: list = list(candidate_ids)
    else:
        conv_filter = ""
        params = []

    if role == "user":
        # Prompts are user messages — return their IDs directly
        rows = conn.execute(
            f"SELECT p.id FROM prompts p WHERE 1=1 {conv_filter}", params
        ).fetchall()
    else:
        # 'assistant': return prompt IDs that have responses (assistant replied)
        rows = conn.execute(
            f"""SELECT DISTINCT r.prompt_id AS id
                FROM responses r
                JOIN prompts p ON p.id = r.prompt_id
                WHERE 1=1 {conv_filter}""",
            params,
        ).fetchall()

    conn.close()
    return {row["id"] for row in rows} if rows else None


def get_active_conversation_ids(db: Path) -> set[str]:
    """Get conversation IDs that originated from currently-active session files.

    Uses list_active_sessions() from the peek module to find active JSONL files,
    then looks up which ingested conversations came from those file paths.

    Args:
        db: Path to the main database.

    Returns:
        Set of conversation IDs to exclude (may be empty).
    """
    try:
        from siftd.peek.scanner import list_active_sessions
    except ImportError:
        return set()

    try:
        sessions = list_active_sessions(include_inactive=False)
    except Exception:
        # Filesystem scan failed — don't block search
        return set()

    if not sessions:
        return set()

    file_paths = [str(s.file_path) for s in sessions]

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(file_paths))
    rows = conn.execute(
        f"SELECT conversation_id FROM ingested_files WHERE path IN ({placeholders}) AND conversation_id IS NOT NULL",
        file_paths,
    ).fetchall()
    conn.close()

    return {row["conversation_id"] for row in rows}
