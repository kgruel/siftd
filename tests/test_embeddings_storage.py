"""Tests for embeddings storage."""

import math
import sqlite3

from siftd.storage.embeddings import (
    open_embeddings_db,
    prune_orphaned_chunks,
    search_similar,
    store_chunk,
)


def test_search_similar_empty_conversation_ids(tmp_path):
    """Empty conversation_ids should return no results (not a SQL error)."""
    db_path = tmp_path / "embeddings.db"
    conn = open_embeddings_db(db_path)
    try:
        store_chunk(
            conn,
            conversation_id="c1",
            chunk_type="exchange",
            text="hello world",
            embedding=[1.0, 0.0, 0.0],
            token_count=2,
            commit=True,
        )

        results = search_similar(
            conn,
            query_embedding=[1.0, 0.0, 0.0],
            conversation_ids=set(),
        )
        assert results == []
    finally:
        conn.close()


def test_store_and_search_round_trip(tmp_path):
    """Store chunks, search by embedding, verify ranking and fields."""
    db_path = tmp_path / "embeddings.db"
    conn = open_embeddings_db(db_path)
    try:
        # Store three chunks with known embeddings
        store_chunk(conn, conversation_id="c1", chunk_type="prompt", text="about caching",
                    embedding=[1.0, 0.0, 0.0], token_count=2, commit=False)
        store_chunk(conn, conversation_id="c1", chunk_type="response", text="use redis",
                    embedding=[0.9, 0.1, 0.0], token_count=2, commit=False)
        store_chunk(conn, conversation_id="c2", chunk_type="prompt", text="about testing",
                    embedding=[0.0, 1.0, 0.0], token_count=2, commit=True)

        # Query close to [1, 0, 0] — should rank c1 chunks first
        results = search_similar(conn, query_embedding=[1.0, 0.0, 0.0], limit=10)

        assert len(results) == 3
        assert results[0]["text"] == "about caching"
        assert results[0]["score"] > results[2]["score"]

        # Verify fields
        r = results[0]
        assert "chunk_id" in r
        assert r["conversation_id"] == "c1"
        assert r["chunk_type"] == "prompt"
    finally:
        conn.close()


def test_search_filters_by_conversation_id(tmp_path):
    """conversation_ids parameter restricts results."""
    db_path = tmp_path / "embeddings.db"
    conn = open_embeddings_db(db_path)
    try:
        store_chunk(conn, conversation_id="c1", chunk_type="prompt", text="hello",
                    embedding=[1.0, 0.0, 0.0], token_count=1, commit=False)
        store_chunk(conn, conversation_id="c2", chunk_type="prompt", text="world",
                    embedding=[0.0, 1.0, 0.0], token_count=1, commit=True)

        results = search_similar(conn, query_embedding=[1.0, 0.0, 0.0], conversation_ids={"c2"})

        assert len(results) == 1
        assert results[0]["conversation_id"] == "c2"
    finally:
        conn.close()


def test_open_embeddings_db_creates_schema(tmp_path):
    """open_embeddings_db creates the chunks table on a new database."""
    db_path = tmp_path / "new_embed.db"
    conn = open_embeddings_db(db_path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "chunks" in tables
    finally:
        conn.close()


def _make_main_db(path, conversation_ids):
    """Create a minimal main DB with given conversation IDs."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
    for cid in conversation_ids:
        conn.execute("INSERT INTO conversations (id) VALUES (?)", (cid,))
    conn.commit()
    return conn


def test_prune_orphaned_chunks_removes_orphans(tmp_path):
    """Chunks for deleted conversations are pruned."""
    main_conn = _make_main_db(tmp_path / "main.db", ["c1"])
    embed_conn = open_embeddings_db(tmp_path / "embed.db")
    try:
        store_chunk(embed_conn, "c1", "exchange", "kept", [1.0, 0.0], token_count=1, commit=False)
        store_chunk(embed_conn, "c2", "exchange", "orphan1", [0.0, 1.0], token_count=1, commit=False)
        store_chunk(embed_conn, "c2", "exchange", "orphan2", [0.0, 1.0], token_count=1, commit=True)

        pruned = prune_orphaned_chunks(main_conn, embed_conn)

        assert pruned == 2
        remaining = embed_conn.execute("SELECT conversation_id FROM chunks").fetchall()
        assert [r[0] for r in remaining] == ["c1"]
    finally:
        embed_conn.close()
        main_conn.close()


def test_prune_orphaned_chunks_no_orphans(tmp_path):
    """Returns 0 when all chunks have matching conversations."""
    main_conn = _make_main_db(tmp_path / "main.db", ["c1", "c2"])
    embed_conn = open_embeddings_db(tmp_path / "embed.db")
    try:
        store_chunk(embed_conn, "c1", "exchange", "text1", [1.0], token_count=1, commit=False)
        store_chunk(embed_conn, "c2", "exchange", "text2", [0.0], token_count=1, commit=True)

        pruned = prune_orphaned_chunks(main_conn, embed_conn)
        assert pruned == 0
    finally:
        embed_conn.close()
        main_conn.close()


def test_prune_orphaned_chunks_empty_embeddings(tmp_path):
    """Returns 0 when embeddings DB has no chunks."""
    main_conn = _make_main_db(tmp_path / "main.db", ["c1"])
    embed_conn = open_embeddings_db(tmp_path / "embed.db")
    try:
        pruned = prune_orphaned_chunks(main_conn, embed_conn)
        assert pruned == 0
    finally:
        embed_conn.close()
        main_conn.close()

