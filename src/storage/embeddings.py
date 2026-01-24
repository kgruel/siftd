"""Embeddings storage for semantic search.

Separate SQLite DB from the main tbd.db — embeddings are derived data
that can be rebuilt from the main DB at any time.
"""

import json
import sqlite3
import struct
import time
import os
from pathlib import Path


# ULID generation (same as sqlite.py, inline to avoid circular imports)
_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODING_LEN = len(_ENCODING)


def _ulid() -> str:
    timestamp_ms = int(time.time() * 1000)
    ts_chars = []
    for _ in range(10):
        ts_chars.append(_ENCODING[timestamp_ms % _ENCODING_LEN])
        timestamp_ms //= _ENCODING_LEN
    ts_part = "".join(reversed(ts_chars))

    rand_bytes = os.urandom(10)
    rand_int = int.from_bytes(rand_bytes, "big")
    rand_chars = []
    for _ in range(16):
        rand_chars.append(_ENCODING[rand_int % _ENCODING_LEN])
        rand_int //= _ENCODING_LEN
    rand_part = "".join(reversed(rand_chars))

    return ts_part + rand_part


def open_embeddings_db(db_path: Path) -> sqlite3.Connection:
    """Open embeddings database, creating schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    if is_new:
        _create_schema(conn)

    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the embeddings schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            chunk_type TEXT NOT NULL,  -- 'prompt' or 'response'
            text TEXT NOT NULL,
            embedding BLOB,
            token_count INTEGER,
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


def store_chunk(
    conn: sqlite3.Connection,
    conversation_id: str,
    chunk_type: str,
    text: str,
    embedding: list[float],
    *,
    token_count: int | None = None,
    commit: bool = False,
) -> str:
    """Store a text chunk with its embedding vector."""
    chunk_id = _ulid()
    embedding_blob = _encode_embedding(embedding)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    actual_token_count = token_count if token_count is not None else len(text.split())

    conn.execute(
        """INSERT INTO chunks (id, conversation_id, chunk_type, text, embedding, token_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, conversation_id, chunk_type, text, embedding_blob, actual_token_count, created_at),
    )
    if commit:
        conn.commit()
    return chunk_id


def get_indexed_conversation_ids(conn: sqlite3.Connection) -> set[str]:
    """Return set of conversation IDs that already have embeddings."""
    cur = conn.execute("SELECT DISTINCT conversation_id FROM chunks")
    return {row["conversation_id"] for row in cur.fetchall()}


def clear_all(conn: sqlite3.Connection) -> None:
    """Delete all chunks (for full rebuild)."""
    conn.execute("DELETE FROM chunks")
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
) -> list[dict]:
    """Find chunks most similar to the query embedding (cosine similarity).

    If conversation_ids is provided, only search within those conversations.
    Returns list of dicts: conversation_id, chunk_type, text, score.
    """
    if conversation_ids is not None:
        placeholders = ",".join("?" * len(conversation_ids))
        cur = conn.execute(
            f"SELECT id, conversation_id, chunk_type, text, embedding FROM chunks WHERE conversation_id IN ({placeholders})",
            list(conversation_ids),
        )
    else:
        cur = conn.execute("SELECT id, conversation_id, chunk_type, text, embedding FROM chunks")

    results = []
    for row in cur:
        stored_embedding = _decode_embedding(row["embedding"])
        score = _cosine_similarity(query_embedding, stored_embedding)
        results.append({
            "chunk_id": row["id"],
            "conversation_id": row["conversation_id"],
            "chunk_type": row["chunk_type"],
            "text": row["text"],
            "score": score,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


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


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
