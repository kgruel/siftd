"""Tests for embeddings storage helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tbd.storage.embeddings import open_embeddings_db, search_similar, store_chunk


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

