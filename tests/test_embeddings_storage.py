"""Tests for embeddings storage."""

import sqlite3

from siftd.storage.embeddings import (
    open_embeddings_db,
    prune_orphaned_chunks,
    search_similar,
    store_chunk,
)
from siftd.storage.sql_helpers import (
    DEFAULT_BATCH_SIZE,
    batched_execute,
    batched_in_query,
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


# =============================================================================
# Batched IN() query tests
# =============================================================================


def test_batched_in_query_over_1000_ids(tmp_path):
    """batched_in_query handles >1000 IDs without SQLite variable limit error."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, value INTEGER)")

    # Insert 1500 items (exceeds SQLite's 999 limit)
    num_items = 1500
    for i in range(num_items):
        conn.execute("INSERT INTO items (id, value) VALUES (?, ?)", (f"id_{i}", i))
    conn.commit()

    # Query all IDs using batched helper
    all_ids = [f"id_{i}" for i in range(num_items)]
    rows = batched_in_query(
        conn,
        "SELECT id, value FROM items WHERE id IN ({placeholders})",
        all_ids,
    )

    assert len(rows) == num_items
    # Verify we got all items
    returned_ids = {row["id"] for row in rows}
    assert returned_ids == set(all_ids)
    conn.close()


def test_batched_in_query_with_prefix_params(tmp_path):
    """batched_in_query correctly handles prefix params before IN clause."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE items (category TEXT, id TEXT, PRIMARY KEY (category, id))")

    # Insert items in two categories
    for cat in ["a", "b"]:
        for i in range(600):
            conn.execute("INSERT INTO items (category, id) VALUES (?, ?)", (cat, f"id_{i}"))
    conn.commit()

    # Query with category filter + large IN clause
    ids = [f"id_{i}" for i in range(600)]
    rows = batched_in_query(
        conn,
        "SELECT category, id FROM items WHERE category = ? AND id IN ({placeholders})",
        ids,
        prefix_params=("a",),
    )

    assert len(rows) == 600
    assert all(row["category"] == "a" for row in rows)
    conn.close()


def test_batched_execute_over_1000_ids(tmp_path):
    """batched_execute handles DELETE with >1000 IDs."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")

    num_items = 1200
    for i in range(num_items):
        conn.execute("INSERT INTO items (id) VALUES (?)", (f"id_{i}",))
    conn.commit()

    # Delete 1100 items (exceeds limit)
    ids_to_delete = [f"id_{i}" for i in range(1100)]
    deleted = batched_execute(
        conn,
        "DELETE FROM items WHERE id IN ({placeholders})",
        ids_to_delete,
    )

    assert deleted == 1100
    remaining = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert remaining == 100
    conn.close()


def test_batched_in_query_empty_ids():
    """batched_in_query with empty IDs returns empty list without error."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id TEXT)")

    rows = batched_in_query(conn, "SELECT id FROM items WHERE id IN ({placeholders})", [])
    assert rows == []
    conn.close()


def test_search_similar_over_1000_conversation_ids(tmp_path):
    """search_similar handles >1000 conversation IDs filter."""
    db_path = tmp_path / "embeddings.db"
    conn = open_embeddings_db(db_path)
    try:
        # Store chunks for 50 conversations
        for i in range(50):
            store_chunk(
                conn,
                conversation_id=f"conv_{i}",
                chunk_type="exchange",
                text=f"text {i}",
                embedding=[1.0, 0.0, 0.0],
                token_count=2,
                commit=False,
            )
        conn.commit()

        # Create a filter set with >1000 IDs (only 50 exist)
        large_filter = {f"conv_{i}" for i in range(1500)}

        # This should not raise a SQLite error
        results = search_similar(
            conn,
            query_embedding=[1.0, 0.0, 0.0],
            limit=100,
            conversation_ids=large_filter,
        )

        # Should find all 50 that exist
        assert len(results) == 50
    finally:
        conn.close()


def test_prune_orphaned_chunks_over_1000_orphans(tmp_path):
    """prune_orphaned_chunks handles >1000 orphaned conversation IDs."""
    main_conn = _make_main_db(tmp_path / "main.db", ["kept_1", "kept_2"])
    embed_conn = open_embeddings_db(tmp_path / "embed.db")
    try:
        # Store chunks for 1100 orphaned conversations + 2 kept
        store_chunk(embed_conn, "kept_1", "exchange", "text", [1.0], token_count=1, commit=False)
        store_chunk(embed_conn, "kept_2", "exchange", "text", [1.0], token_count=1, commit=False)
        for i in range(1100):
            store_chunk(
                embed_conn,
                f"orphan_{i}",
                "exchange",
                "orphan text",
                [0.0, 1.0],
                token_count=1,
                commit=False,
            )
        embed_conn.commit()

        # Prune should handle >1000 orphans without SQLite error
        pruned = prune_orphaned_chunks(main_conn, embed_conn)

        assert pruned == 1100
        remaining = embed_conn.execute("SELECT DISTINCT conversation_id FROM chunks").fetchall()
        assert {r[0] for r in remaining} == {"kept_1", "kept_2"}
    finally:
        embed_conn.close()
        main_conn.close()

