"""Tests for embeddings storage."""

import math

from strata.storage.embeddings import open_embeddings_db, search_similar, store_chunk


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

