# ruff: noqa: E402
"""Tests for the embeddings subsystem.

Covers:
- chunker: extract_exchange_window_chunks()
- indexer: build_embeddings_index()
- backend: get_backend() and embed_documents()/embed_query() contract
"""

import sqlite3

import pytest

pytestmark = pytest.mark.embeddings

pytest.importorskip("fastembed")

from siftd.embeddings.base import get_backend
from siftd.embeddings.chunker import extract_exchange_window_chunks
from siftd.embeddings.indexer import IndexStats, build_embeddings_index
from siftd.storage.embeddings import (
    chunk_count,
    get_indexed_conversation_ids,
    open_embeddings_db,
    search_similar,
)
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_tool_call,
)

# Uses semantic_search_db fixture from conftest.py


@pytest.fixture
def main_db_with_conversations(semantic_search_db):
    """Alias to shared semantic_search_db fixture for embeddings tests."""
    return semantic_search_db


@pytest.fixture
def main_db_with_tool_calls(tmp_path):
    """Database with a conversation that includes a tool_call event."""
    db_path = tmp_path / "main_tool.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    model_id = get_or_create_model(conn, "test-model")
    ws_id = get_or_create_workspace(conn, "/projects/test-tool", "2024-01-01T10:00:00Z")
    tool_id = get_or_create_tool(conn, "bash")

    conv_id = insert_conversation(
        conn, external_id="conv-tool", harness_id=harness_id,
        workspace_id=ws_id, started_at="2024-01-15T10:00:00Z",
    )
    p_id = insert_prompt(conn, conv_id, "p-tool", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p_id, 0, "text", '{"text": "Run the tests for me."}')
    r_id = insert_response(
        conn, conv_id, p_id, model_id, None, "r-tool", "2024-01-15T10:00:01Z",
        input_tokens=10, output_tokens=50,
    )
    insert_tool_call(
        conn, r_id, conv_id, tool_id, "tc-tool-1",
        '{"command": "pytest tests/"}', '{"output": "All tests passed."}',
        "success", "2024-01-15T10:00:02Z",
    )

    conn.commit()
    conn.close()
    return {"db_path": db_path, "conv_id": conv_id}


class TestExtractExchangeWindowChunks:
    """Tests for the exchange-window chunking strategy."""

    def test_extracts_chunks_from_conversations(self, main_db_with_conversations):
        """extract_exchange_window_chunks returns chunks from all conversations."""
        db_path = main_db_with_conversations["db_path"]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        chunks = extract_exchange_window_chunks(conn)
        conn.close()

        assert len(chunks) >= 2, "Expected at least one chunk per conversation"

        # Each chunk has required fields
        for chunk in chunks:
            assert "conversation_id" in chunk
            assert "chunk_type" in chunk
            assert "text" in chunk
            assert "token_count" in chunk
            assert "source_ids" in chunk
            assert chunk["chunk_type"] == "exchange"
            assert chunk["token_count"] > 0
            assert len(chunk["source_ids"]) > 0

    def test_filters_by_conversation_id(self, main_db_with_conversations):
        """Can filter to a specific conversation."""
        db_path = main_db_with_conversations["db_path"]
        conv1_id = main_db_with_conversations["conv1_id"]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        chunks = extract_exchange_window_chunks(conn, conversation_id=conv1_id)
        conn.close()

        assert len(chunks) >= 1
        assert all(c["conversation_id"] == conv1_id for c in chunks)

    def test_excludes_conversation_ids(self, main_db_with_conversations):
        """exclude_conversation_ids filters out specified conversations."""
        db_path = main_db_with_conversations["db_path"]
        conv1_id = main_db_with_conversations["conv1_id"]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        chunks = extract_exchange_window_chunks(
            conn, exclude_conversation_ids={conv1_id}
        )
        conn.close()

        assert all(c["conversation_id"] != conv1_id for c in chunks)

    def test_empty_db_returns_empty(self, tmp_path):
        """Empty database returns no chunks."""
        db_path = tmp_path / "empty.db"
        conn = create_database(db_path)
        conn.row_factory = sqlite3.Row

        chunks = extract_exchange_window_chunks(conn)
        conn.close()

        assert chunks == []

    def test_source_ids_widen_to_prompt_and_response_events(self, main_db_with_conversations):
        """source_ids carry every constituent event id (prompt AND response) — the bridge."""
        db_path = main_db_with_conversations["db_path"]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        chunks = extract_exchange_window_chunks(conn)

        # Which event ids are prompts vs responses in the main DB?
        prompt_ids = {r[0] for r in conn.execute("SELECT id FROM events WHERE kind='prompt'").fetchall()}
        response_ids = {r[0] for r in conn.execute("SELECT id FROM events WHERE kind='response'").fetchall()}
        conn.close()

        all_source_ids: set[str] = set()
        for chunk in chunks:
            all_source_ids.update(chunk["source_ids"])
            # prompt id anchors the chunk (source_ids[0]), so turn positioning is stable
            assert chunk["source_ids"][0] in prompt_ids

        assert len(all_source_ids) >= 2
        # The widening: response event ids appear in source_ids, not just prompts.
        assert all_source_ids & response_ids, "response event ids missing from source_ids"


class TestBuildEmbeddingsIndex:
    """Integration tests for the embeddings indexer."""

    def test_builds_index_from_scratch(self, main_db_with_conversations, tmp_path):
        """build_embeddings_index creates chunks and stores embeddings."""
        db_path = main_db_with_conversations["db_path"]
        embed_db_path = tmp_path / "embeddings.db"

        stats = build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        assert isinstance(stats, IndexStats)
        assert stats.chunks_added > 0
        assert stats.total_chunks == stats.chunks_added
        assert stats.backend_name == "fastembed"
        assert stats.dimension > 0

        # Verify database has chunks
        conn = open_embeddings_db(embed_db_path)
        assert chunk_count(conn) == stats.total_chunks
        conn.close()

    def test_incremental_indexing(self, main_db_with_conversations, tmp_path):
        """Second run doesn't re-index existing conversations."""
        db_path = main_db_with_conversations["db_path"]
        embed_db_path = tmp_path / "embeddings.db"

        # First build
        stats1 = build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        # Second build (incremental)
        stats2 = build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        assert stats2.chunks_added == 0, "Should not add new chunks on second run"
        assert stats2.total_chunks == stats1.total_chunks

    def test_rebuild_clears_and_reindexes(self, main_db_with_conversations, tmp_path):
        """rebuild=True clears existing index and rebuilds."""
        db_path = main_db_with_conversations["db_path"]
        embed_db_path = tmp_path / "embeddings.db"

        # First build
        stats1 = build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        # Rebuild
        stats2 = build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            rebuild=True,
            verbose=False,
        )

        # Should have same number of chunks (rebuilt from same data)
        assert stats2.chunks_added == stats1.total_chunks
        assert stats2.total_chunks == stats1.total_chunks

    def test_raises_on_missing_db(self, tmp_path):
        """Raises FileNotFoundError if main database doesn't exist."""
        with pytest.raises(FileNotFoundError):
            build_embeddings_index(
                db_path=tmp_path / "nonexistent.db",
                embed_db_path=tmp_path / "embed.db",
            )

    def test_v2_lifecycle_end_to_end(self, main_db_with_tool_calls, tmp_path):
        """Real-backend v2 build: identity meta, indexed_state, append re-index, status."""
        from siftd.embeddings.indexer import embed_index_status
        from siftd.storage.embeddings import get_indexed_state, get_meta

        db_path = main_db_with_tool_calls["db_path"]
        conv_id = main_db_with_tool_calls["conv_id"]
        embed_db_path = tmp_path / "embeddings.db"

        build_embeddings_index(db_path=db_path, embed_db_path=embed_db_path, verbose=False)

        conn = open_embeddings_db(embed_db_path, read_only=True)
        try:
            assert get_meta(conn, "schema_version") == "2"
            assert get_meta(conn, "backend") == "fastembed"
            assert conv_id in get_indexed_state(conn)
        finally:
            conn.close()

        rep = embed_index_status(db_path=db_path, embed_db_path=embed_db_path)
        assert rep.index_exists and not rep.needs_rebuild
        assert rep.conversations_stale == 0

        # Append a new prompt/response → fingerprint changes → conversation re-indexes.
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_model,
            insert_prompt,
            insert_prompt_content,
            insert_response,
            insert_response_content,
        )

        conn = create_database(db_path)
        m = get_or_create_model(conn, "test-model")
        pid = insert_prompt(conn, conv_id, "p-appended", "2024-01-15T11:00:00Z")
        insert_prompt_content(conn, pid, 0, "text", '{"text": "a brand new follow-up question"}')
        rid = insert_response(conn, conv_id, pid, m, None, "r-appended", "2024-01-15T11:00:01Z", input_tokens=5, output_tokens=10)
        insert_response_content(conn, rid, 0, "text", '{"text": "a brand new answer to it"}')
        conn.commit()
        conn.close()

        stats = build_embeddings_index(db_path=db_path, embed_db_path=embed_db_path, verbose=False)
        assert stats.chunks_added >= 1 and stats.conversations_indexed == 1

    def test_search_finds_indexed_content(self, main_db_with_conversations, tmp_path):
        """Indexed content is searchable via search_similar."""
        db_path = main_db_with_conversations["db_path"]
        embed_db_path = tmp_path / "embeddings.db"

        # Build index
        build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        # Search for Python content
        backend = get_backend()
        query_embedding = backend.embed_query("Python programming language")

        conn = open_embeddings_db(embed_db_path, read_only=True)
        results = search_similar(conn, query_embedding, limit=5)
        conn.close()

        assert len(results) > 0
        # The Python conversation should rank highly
        top_result = results[0]
        assert "Python" in top_result["text"] or "programming" in top_result["text"]
        assert top_result["score"] > 0

    def test_tool_summary_chunk_indexed(self, main_db_with_tool_calls, tmp_path):
        """build_embeddings_index produces tool_summary chunks from tool_call events."""
        db_path = main_db_with_tool_calls["db_path"]
        embed_db_path = tmp_path / "embeddings.db"

        stats = build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        assert stats.chunks_added > 0

        embed_conn = open_embeddings_db(embed_db_path, read_only=True)
        tool_chunk_count = embed_conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE chunk_type = 'tool_summary'"
        ).fetchone()[0]
        embed_conn.close()

        assert tool_chunk_count > 0


class TestEmbeddingBackend:
    """Tests for the embedding backend interface."""

    def test_get_backend_returns_backend(self):
        """get_backend() returns a working backend."""
        backend = get_backend()

        assert hasattr(backend, "name")
        assert hasattr(backend, "dimension")
        assert hasattr(backend, "embed_documents")
        assert hasattr(backend, "embed_query")
        assert backend.dimension > 0

    def test_embed_documents_batch(self):
        """embed_documents() handles batches of texts."""
        backend = get_backend()

        texts = ["Hello world", "Python programming", "Machine learning"]
        embeddings = backend.embed_documents(texts)

        assert len(embeddings) == len(texts)
        for emb in embeddings:
            assert len(emb) == backend.dimension
            assert all(isinstance(v, float) for v in emb)

    def test_embed_query(self):
        """embed_query() returns a single embedding."""
        backend = get_backend()

        embedding = backend.embed_query("Test sentence")

        assert len(embedding) == backend.dimension
        assert all(isinstance(v, float) for v in embedding)

    def test_embed_documents_empty_batch(self):
        """embed_documents() handles empty batch."""
        backend = get_backend()

        embeddings = backend.embed_documents([])

        assert embeddings == []

    def test_get_backend_is_config_driven_no_override(self):
        """get_backend takes no per-call override — the backend is config (embed.backend)."""
        import inspect

        params = inspect.signature(get_backend).parameters
        assert "preferred" not in params
        # Unset config resolves to the local fastembed backend (installed in this lane).
        assert get_backend().name == "fastembed"

    def test_unknown_backend_raises(self, monkeypatch):
        """An unknown embed.backend value is a config error, not a silent fallthrough."""
        import siftd.config
        from siftd.embeddings import base
        from siftd.embeddings.base import EmbeddingConfigError

        base.invalidate_backend_cache()
        # _read_embed_config reads via siftd.config.get_config (local import each call).
        monkeypatch.setattr(
            siftd.config, "get_config",
            lambda key: "nonexistent_backend" if key == "embed.backend" else None,
        )
        try:
            with pytest.raises(EmbeddingConfigError, match="not a known backend"):
                get_backend()
        finally:
            base.invalidate_backend_cache()


class TestIndexedConversationTracking:
    """Tests for conversation tracking in the embeddings DB."""

    def test_get_indexed_conversation_ids(self, main_db_with_conversations, tmp_path):
        """get_indexed_conversation_ids returns IDs after indexing."""
        db_path = main_db_with_conversations["db_path"]
        conv1_id = main_db_with_conversations["conv1_id"]
        conv2_id = main_db_with_conversations["conv2_id"]
        embed_db_path = tmp_path / "embeddings.db"

        # Build index
        build_embeddings_index(
            db_path=db_path,
            embed_db_path=embed_db_path,
            verbose=False,
        )

        conn = open_embeddings_db(embed_db_path, read_only=True)
        indexed_ids = get_indexed_conversation_ids(conn)
        conn.close()

        assert conv1_id in indexed_ids
        assert conv2_id in indexed_ids

    def test_empty_db_returns_empty_set(self, tmp_path):
        """Empty embeddings DB returns empty set."""
        embed_db_path = tmp_path / "embeddings.db"
        conn = open_embeddings_db(embed_db_path)
        indexed_ids = get_indexed_conversation_ids(conn)
        conn.close()

        assert indexed_ids == set()
