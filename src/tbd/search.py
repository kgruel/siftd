"""Public search API for programmatic access by agent harnesses."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SearchResult:
    """A single search result from hybrid_search."""

    conversation_id: str
    score: float
    text: str
    chunk_type: str
    workspace_path: str | None
    started_at: str | None


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

    Returns:
        List of SearchResult ordered by descending similarity score.

    Raises:
        FileNotFoundError: If the database files don't exist.
        RuntimeError: If no embedding backend is available.
    """
    from tbd.embeddings import get_backend
    from tbd.paths import db_path as default_db_path
    from tbd.paths import embeddings_db_path as default_embed_path
    from tbd.storage.embeddings import open_embeddings_db, search_similar
    from tbd.storage.sqlite import fts5_recall_conversations

    db = db_path if db_path is not None else default_db_path()
    embed_db = embed_db_path if embed_db_path is not None else default_embed_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")
    if not embed_db.exists():
        raise FileNotFoundError(f"Embeddings database not found: {embed_db}")

    # Build candidate filter set
    candidate_ids = _filter_conversations(db, workspace=workspace, model=model, since=since, before=before)

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
    embed_backend = get_backend(preferred=backend, verbose=False)
    query_embedding = embed_backend.embed_one(query)

    embed_conn = open_embeddings_db(embed_db)
    raw_results = search_similar(
        embed_conn,
        query_embedding,
        limit=limit,
        conversation_ids=candidate_ids,
    )
    embed_conn.close()

    if not raw_results:
        return []

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
        ))

    return results


def _filter_conversations(
    db: Path,
    *,
    workspace: str | None,
    model: str | None,
    since: str | None,
    before: str | None,
) -> set[str] | None:
    """Apply filters and return candidate conversation IDs, or None if no filters."""
    if not any([workspace, model, since, before]):
        return None

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    conditions = []
    params: list[str] = []

    if workspace:
        conditions.append("w.path LIKE ?")
        params.append(f"%{workspace}%")

    if model:
        conditions.append("(m.raw_name LIKE ? OR m.name LIKE ?)")
        params.append(f"%{model}%")
        params.append(f"%{model}%")

    if since:
        conditions.append("c.started_at >= ?")
        params.append(since)

    if before:
        conditions.append("c.started_at < ?")
        params.append(before)

    where = "WHERE " + " AND ".join(conditions)
    sql = f"""
        SELECT DISTINCT c.id
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        LEFT JOIN responses r ON r.conversation_id = c.id
        LEFT JOIN models m ON m.id = r.model_id
        {where}
    """

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {row["id"] for row in rows}
