import importlib
import sqlite3
import types
from types import SimpleNamespace

import pytest


def _load_indexer(monkeypatch):
    import siftd.embeddings as emb

    if not hasattr(emb, "get_backend"):
        monkeypatch.setattr(emb, "get_backend", lambda *a, **k: None, raising=False)
    np_stub = types.ModuleType("numpy")
    np_stub.ndarray = object
    monkeypatch.setitem(__import__("sys").modules, "numpy", np_stub)
    return importlib.import_module("siftd.embeddings.indexer")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _EmbedConn:
    def __init__(self):
        self.closed = False
        self.commits = 0
        self.stored = []

    def execute(self, _sql, _params=()):
        return _Rows([])

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class _MainConn:
    def execute(self, _sql, _params=()):
        return _Rows([])

    def close(self):
        return None


def test_build_embeddings_index_missing_db_raises(monkeypatch, tmp_path):
    indexer = _load_indexer(monkeypatch)
    with pytest.raises(FileNotFoundError):
        indexer.build_embeddings_index(db_path=tmp_path / "missing.db", embed_db_path=tmp_path / "e.db")


def test_build_embeddings_index_no_chunks_returns_zero(monkeypatch, tmp_path):
    indexer = _load_indexer(monkeypatch)
    db = tmp_path / "main.db"
    db.write_text("x")
    embed_conn = _EmbedConn()

    monkeypatch.setattr(indexer, "get_backend", lambda **_k: SimpleNamespace(name="b", model="m", dimension=3))
    monkeypatch.setattr(indexer, "open_embeddings_db", lambda _p: embed_conn)
    monkeypatch.setattr(indexer, "_validate_incremental_compat", lambda *_a, **_k: None)
    monkeypatch.setattr(indexer, "get_indexed_conversation_ids", lambda _c: set())
    monkeypatch.setattr(indexer, "open_database", lambda *_a, **_k: _MainConn())
    monkeypatch.setattr(indexer, "_get_tokenizer", lambda: object())
    monkeypatch.setattr(indexer, "extract_exchange_window_chunks", lambda *_a, **_k: [])
    monkeypatch.setattr(indexer, "_all_conversation_ids_with_tool_calls", lambda _c: set())
    monkeypatch.setattr(indexer, "_filter_conversations_with_tool_calls", lambda _c, ids: ids)
    monkeypatch.setattr(indexer, "chunk_count", lambda _c: 7)

    out = indexer.build_embeddings_index(db_path=db, embed_db_path=tmp_path / "emb.db")
    assert out.chunks_added == 0 and out.total_chunks == 7 and embed_conn.closed


def test_build_embeddings_index_rebuild_verbose_and_batch_progress(monkeypatch, tmp_path, capsys):
    indexer = _load_indexer(monkeypatch)
    db = tmp_path / "main.db"
    db.write_text("x")
    embed_conn = _EmbedConn()
    backend = SimpleNamespace(name="fastembed", model="m", dimension=2)
    backend.embed = lambda batch: [[1.0, 2.0] for _ in batch]

    monkeypatch.setattr(indexer, "get_backend", lambda **_k: backend)
    monkeypatch.setattr(indexer, "open_embeddings_db", lambda _p: embed_conn)
    monkeypatch.setattr(indexer, "clear_all", lambda _c: None)
    monkeypatch.setattr(indexer, "get_indexed_conversation_ids", lambda _c: set())
    monkeypatch.setattr(indexer, "open_database", lambda *_a, **_k: _MainConn())
    monkeypatch.setattr(indexer, "_get_tokenizer", lambda: object())
    chunks = [{"conversation_id": f"c{i}", "chunk_type": "exchange", "text": f"t {i}", "token_count": 1, "source_ids": ["p"]} for i in range(70)]
    monkeypatch.setattr(indexer, "extract_exchange_window_chunks", lambda *_a, **_k: chunks)
    monkeypatch.setattr(indexer, "_all_conversation_ids_with_tool_calls", lambda _c: set())
    monkeypatch.setattr(indexer, "_filter_conversations_with_tool_calls", lambda _c, ids: ids)
    monkeypatch.setattr(indexer, "extract_tool_summary_chunks", lambda *_a, **_k: [{"conversation_id": "c0", "chunk_type": "tool_summary", "text": "tool summary", "source_ids": []}])
    monkeypatch.setattr(indexer, "_count_tokens", lambda _t, _s: 3)
    monkeypatch.setattr(indexer, "store_chunk", lambda conn, **kw: conn.stored.append(kw))
    monkeypatch.setattr(indexer, "set_meta", lambda *_a, **_k: None)
    monkeypatch.setattr(indexer, "chunk_count", lambda c: len(c.stored))

    out = indexer.build_embeddings_index(db_path=db, embed_db_path=tmp_path / "emb.db", rebuild=True, verbose=True)
    txt = capsys.readouterr().out
    assert "Clearing existing index" in txt and "Embedding" in txt and "Done. Index has" in txt
    assert out.chunks_added == 71 and out.total_chunks == 71 and embed_conn.commits == 1


def test_filter_and_tool_call_helpers_cover_batching(monkeypatch, tmp_path):
    indexer = _load_indexer(monkeypatch)
    conn = sqlite3.connect(tmp_path / "main.db")
    conn.execute("CREATE TABLE tool_calls (conversation_id TEXT)")
    conn.executemany("INSERT INTO tool_calls (conversation_id) VALUES (?)", [(f"c{i}",) for i in range(1005)])
    conn.commit()
    try:
        all_ids = indexer._all_conversation_ids_with_tool_calls(conn)
        keep = indexer._filter_conversations_with_tool_calls(conn, {f"c{i}" for i in range(1200)})
        assert "c0" in all_ids and len(keep) == 1005 and indexer._filter_conversations_with_tool_calls(conn, set()) == set()
    finally:
        conn.close()


def test_validate_incremental_compat_mismatch_paths(monkeypatch):
    indexer = _load_indexer(monkeypatch)
    backend = SimpleNamespace(name="fastembed", model="m1")
    monkeypatch.setattr(indexer, "chunk_count", lambda _c: 1)

    monkeypatch.setattr(indexer, "get_meta", lambda _c, key: {"backend": "ollama", "model": None}[key])
    with pytest.raises(indexer.IncrementalCompatError, match="different backend"):
        indexer._validate_incremental_compat(object(), backend)

    monkeypatch.setattr(indexer, "get_meta", lambda _c, key: {"backend": "fastembed", "model": "m0"}[key])
    with pytest.raises(indexer.IncrementalCompatError, match="different model"):
        indexer._validate_incremental_compat(object(), backend)

    monkeypatch.setattr(indexer, "chunk_count", lambda _c: 0)
    indexer._validate_incremental_compat(object(), backend)


def test_tokenizer_and_count_tokens_helpers(monkeypatch):
    indexer = _load_indexer(monkeypatch)

    class _Tok:
        def encode(self, text):
            return SimpleNamespace(ids=[0] * (len(text.split()) + 2))

    class _Emb:
        def __init__(self, _name):
            self.model = SimpleNamespace(tokenizer=_Tok())

    monkeypatch.setitem(__import__("sys").modules, "fastembed", types.SimpleNamespace(TextEmbedding=_Emb))
    tok = indexer._get_tokenizer()
    assert indexer._count_tokens(tok, "a b c") == 3 and indexer._count_tokens(tok, "") == 0
