"""Benchmark vector search performance with varying chunk counts."""

import sqlite3
import struct
import time
import random

import numpy as np

# Dimension for embeddings (typical for small models)
EMBED_DIM = 384


def generate_random_embedding(dim: int = EMBED_DIM) -> list[float]:
    """Generate a random unit-normalized embedding."""
    vec = [random.gauss(0, 1) for _ in range(dim)]
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


def encode_embedding(embedding: list[float]) -> bytes:
    """Encode embedding as packed float32 blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def decode_embedding(blob: bytes) -> list[float]:
    """Decode packed float32 blob to list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def decode_embedding_numpy(blob: bytes) -> np.ndarray:
    """Decode packed float32 blob to numpy array (zero-copy)."""
    return np.frombuffer(blob, dtype=np.float32)


def cosine_similarity_python(a: list[float], b: list[float]) -> float:
    """Pure Python cosine similarity (old implementation)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cosine_similarity_batch(query: np.ndarray, embeddings: np.ndarray) -> np.ndarray:
    """Vectorized cosine similarity (new implementation)."""
    query_norm = np.linalg.norm(query)
    if query_norm == 0:
        return np.zeros(embeddings.shape[0], dtype=np.float32)
    query_normalized = query / query_norm

    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    embeddings_normalized = embeddings / norms

    return embeddings_normalized @ query_normalized


def setup_test_db(num_chunks: int) -> tuple[sqlite3.Connection, list[bytes]]:
    """Create in-memory DB with test chunks."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            chunk_type TEXT NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB,
            token_count INTEGER,
            source_ids TEXT,
            created_at TEXT NOT NULL
        );
    """)

    embeddings = []
    for i in range(num_chunks):
        emb = generate_random_embedding()
        emb_blob = encode_embedding(emb)
        embeddings.append(emb_blob)
        conn.execute(
            "INSERT INTO chunks (id, conversation_id, chunk_type, text, embedding, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"chunk_{i}", f"conv_{i % 100}", "exchange", f"Text chunk {i}", emb_blob, "2024-01-01T00:00:00Z")
        )
    conn.commit()

    return conn, embeddings


def benchmark_search_python(conn: sqlite3.Connection, query_embedding: list[float], limit: int = 10) -> tuple[list[dict], float]:
    """Benchmark old Python-based search."""
    start = time.perf_counter()

    cur = conn.execute("SELECT id, conversation_id, chunk_type, text, embedding FROM chunks")

    results = []
    for row in cur:
        stored_embedding = decode_embedding(row["embedding"])
        score = cosine_similarity_python(query_embedding, stored_embedding)
        results.append({
            "chunk_id": row["id"],
            "conversation_id": row["conversation_id"],
            "text": row["text"],
            "score": score,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    top_results = results[:limit]

    elapsed = time.perf_counter() - start
    return top_results, elapsed


def benchmark_search_numpy(conn: sqlite3.Connection, query_embedding: list[float], limit: int = 10) -> tuple[list[dict], float]:
    """Benchmark new numpy-vectorized search."""
    start = time.perf_counter()

    cur = conn.execute("SELECT id, conversation_id, chunk_type, text, embedding FROM chunks")
    rows = cur.fetchall()

    if not rows:
        return [], time.perf_counter() - start

    # Batch decode embeddings into numpy array
    embedding_dim = len(rows[0]["embedding"]) // 4
    embeddings_array = np.empty((len(rows), embedding_dim), dtype=np.float32)
    for i, row in enumerate(rows):
        embeddings_array[i] = decode_embedding_numpy(row["embedding"])

    # Compute all similarities at once
    query_array = np.asarray(query_embedding, dtype=np.float32)
    scores = cosine_similarity_batch(query_array, embeddings_array)

    # Build results
    results = []
    for i, row in enumerate(rows):
        results.append({
            "chunk_id": row["id"],
            "conversation_id": row["conversation_id"],
            "text": row["text"],
            "score": float(scores[i]),
        })

    # Use numpy argsort
    score_indices = np.argsort(scores)[::-1][:limit]
    top_results = [results[i] for i in score_indices]

    elapsed = time.perf_counter() - start
    return top_results, elapsed


def run_benchmarks():
    """Run benchmarks at different chunk counts."""
    chunk_counts = [1000, 5000, 10000]
    query_embedding = generate_random_embedding()

    print("=" * 70)
    print("Vector Search Performance Benchmark: Python vs NumPy")
    print("=" * 70)
    print(f"Embedding dimension: {EMBED_DIM}")
    print()

    for num_chunks in chunk_counts:
        print(f"\n{'='*70}")
        print(f"  {num_chunks:,} chunks")
        print(f"{'='*70}")

        # Setup
        conn, _ = setup_test_db(num_chunks)

        # Benchmark Python (old)
        times_python = []
        for _ in range(3):
            _, elapsed = benchmark_search_python(conn, query_embedding)
            times_python.append(elapsed)
        min_python = min(times_python)

        # Benchmark NumPy (new)
        times_numpy = []
        for _ in range(3):
            _, elapsed = benchmark_search_numpy(conn, query_embedding)
            times_numpy.append(elapsed)
        min_numpy = min(times_numpy)

        speedup = min_python / min_numpy

        print(f"\nPython (old):  {min_python*1000:7.1f}ms  ({num_chunks / min_python:,.0f} chunks/sec)")
        print(f"NumPy (new):   {min_numpy*1000:7.1f}ms  ({num_chunks / min_numpy:,.0f} chunks/sec)")
        print(f"Speedup:       {speedup:7.1f}x")

        conn.close()

    print("\n" + "=" * 70)


if __name__ == "__main__":
    run_benchmarks()
