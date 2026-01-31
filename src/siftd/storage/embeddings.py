"""Embeddings storage for semantic search.

Separate SQLite DB from the main siftd.db — embeddings are derived data
that can be rebuilt from the main DB at any time.
"""

import json
import sqlite3
import struct
import time
from pathlib import Path

import numpy as np

from siftd.ids import ulid as _ulid
from siftd.math import cosine_similarity_batch
from siftd.storage.sql_helpers import batched_execute, batched_in_query


def open_embeddings_db(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open embeddings database.

    Args:
        db_path: Path to embeddings DB.
        read_only: If True, open without forcing WAL or creating/migrating schema.
            This allows read-only operations in restricted environments.
    """
    if read_only and not db_path.exists():
        raise FileNotFoundError(f"Embeddings database not found: {db_path}")

    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        # Use immutable=1 to avoid creating WAL/SHM sidecars when the DB lives on
        # read-only media (or in sandboxed environments).
        uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")

        _create_schema(conn)
        _migrate(conn)

    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the embeddings schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            chunk_type TEXT NOT NULL,  -- 'exchange'
            text TEXT NOT NULL,
            embedding BLOB,
            token_count INTEGER,
            source_ids TEXT,  -- JSON array of prompt IDs in this chunk
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_conversation
            ON chunks(conversation_id);

        CREATE INDEX IF NOT EXISTS idx_chunks_type
            ON chunks(chunk_type);

        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "source_ids" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN source_ids TEXT")
        conn.commit()


def store_chunk(
    conn: sqlite3.Connection,
    conversation_id: str,
    chunk_type: str,
    text: str,
    embedding: list[float],
    *,
    token_count: int | None = None,
    source_ids: list[str] | None = None,
    commit: bool = False,
) -> str:
    """Store a text chunk with its embedding vector."""
    chunk_id = _ulid()
    embedding_blob = _encode_embedding(embedding)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    actual_token_count = token_count if token_count is not None else len(text.split())
    source_ids_json = json.dumps(source_ids) if source_ids else None

    conn.execute(
        """INSERT INTO chunks (id, conversation_id, chunk_type, text, embedding, token_count, source_ids, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, conversation_id, chunk_type, text, embedding_blob, actual_token_count, source_ids_json, created_at),
    )
    if commit:
        conn.commit()
    return chunk_id


def get_indexed_conversation_ids(conn: sqlite3.Connection) -> set[str]:
    """Return set of conversation IDs that already have embeddings."""
    cur = conn.execute("SELECT DISTINCT conversation_id FROM chunks")
    return {row["conversation_id"] for row in cur.fetchall()}


def clear_all(conn: sqlite3.Connection) -> None:
    """Drop and recreate chunks table (for full rebuild)."""
    conn.execute("DROP TABLE IF EXISTS chunks")
    _create_schema(conn)
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata key-value pair."""
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a metadata value by key."""
    cur = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


def search_similar(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    limit: int = 10,
    conversation_ids: set[str] | None = None,
    include_embeddings: bool = False,
) -> list[dict]:
    """Find chunks most similar to the query embedding (cosine similarity).

    If conversation_ids is provided, only search within those conversations.
    If include_embeddings is True, each result dict includes an 'embedding' key
    with the decoded float list (used by MMR reranking).
    Returns list of dicts: conversation_id, chunk_type, text, score, source_ids.
    """
    if conversation_ids is not None and not conversation_ids:
        return []

    if conversation_ids is not None:
        rows = batched_in_query(
            conn,
            "SELECT id, conversation_id, chunk_type, text, embedding, source_ids FROM chunks WHERE conversation_id IN ({placeholders})",
            conversation_ids,
        )
    else:
        rows = conn.execute("SELECT id, conversation_id, chunk_type, text, embedding, source_ids FROM chunks").fetchall()

    if not rows:
        return []

    # Batch decode embeddings into numpy array
    embedding_dim = len(rows[0]["embedding"]) // 4  # float32 = 4 bytes
    embeddings_array = np.empty((len(rows), embedding_dim), dtype=np.float32)

    for i, row in enumerate(rows):
        embeddings_array[i] = _decode_embedding_numpy(row["embedding"])

    # Validate query embedding dimension matches index
    if len(query_embedding) != embedding_dim:
        raise ValueError(
            f"Query embedding dimension ({len(query_embedding)}) does not match index dimension ({embedding_dim}). "
            f"Rebuild the index with 'siftd ask --rebuild' using the same embedding backend."
        )

    # Compute all similarities at once
    query_array = np.asarray(query_embedding, dtype=np.float32)
    scores = cosine_similarity_batch(query_array, embeddings_array)

    # Build results with scores
    results = []
    for i, row in enumerate(rows):
        source_ids_val = json.loads(row["source_ids"]) if row["source_ids"] else []
        result = {
            "chunk_id": row["id"],
            "conversation_id": row["conversation_id"],
            "chunk_type": row["chunk_type"],
            "text": row["text"],
            "score": float(scores[i]),
            "source_ids": source_ids_val,
        }
        if include_embeddings:
            result["embedding"] = embeddings_array[i]
        results.append(result)

    # Use numpy argsort for faster sorting
    score_indices = np.argsort(scores)[::-1][:limit]
    return [results[i] for i in score_indices]


def chunk_count(conn: sqlite3.Connection) -> int:
    """Return total number of chunks in the index."""
    cur = conn.execute("SELECT COUNT(*) as cnt FROM chunks")
    return cur.fetchone()["cnt"]


def _encode_embedding(embedding: list[float]) -> bytes:
    """Encode embedding as packed float32 blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _decode_embedding(blob: bytes) -> list[float]:
    """Decode packed float32 blob to list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _decode_embedding_numpy(blob: bytes) -> np.ndarray:
    """Decode packed float32 blob to numpy array (zero-copy)."""
    return np.frombuffer(blob, dtype=np.float32)


def prune_orphaned_chunks(
    main_conn: sqlite3.Connection,
    embeddings_conn: sqlite3.Connection,
) -> int:
    """Delete chunks whose conversation_id no longer exists in the main DB.

    Cross-database: no FK between embeddings DB and main DB, so orphans
    accumulate when conversations are deleted from main.

    Returns count of pruned chunks.
    """
    # Conversation IDs present in main DB
    main_ids = {
        row[0]
        for row in main_conn.execute("SELECT id FROM conversations").fetchall()
    }

    # Conversation IDs referenced by chunks in embeddings DB
    embed_ids = get_indexed_conversation_ids(embeddings_conn)

    orphaned_ids = embed_ids - main_ids
    if not orphaned_ids:
        return 0

    deleted = batched_execute(
        embeddings_conn,
        "DELETE FROM chunks WHERE conversation_id IN ({placeholders})",
        orphaned_ids,
    )
    embeddings_conn.commit()
    return deleted
