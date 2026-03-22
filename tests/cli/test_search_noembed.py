"""Non-embeddings tests for siftd.cli.search command paths."""

import argparse
import json
from types import SimpleNamespace

from siftd.cli.search import (
    _aggregate_conversations,
    _can_delegate_to_serve,
    _compute_thread_tiers,
    _enrich_context,
    _enrich_exchanges,
    _fetch_search_metadata,
    _has_explicit_formatter,
    _print_empty_json_results,
    _search_build_index,
    _search_fts_only,
    cmd_search,
)


def make_args(**kwargs):
    defaults = {
        "query": [],
        "db": None,
        "embed_db": None,
        "limit": 10,
        "verbose": False,
        "full": False,
        "context": None,
        "by_time": False,
        "workspace": None,
        "model": None,
        "since": None,
        "before": None,
        "index": False,
        "rebuild": False,
        "backend": None,
        "thread": False,
        "embeddings_only": False,
        "recall": 80,
        "role": None,
        "first": False,
        "conversations": False,
        "refs": None,
        "threshold": None,
        "json": False,
        "format": None,
        "no_exclude_active": True,
        "include_derivative": True,
        "no_diversity": True,
        "lambda_": 0.7,
        "recency": False,
        "recency_half_life": 30.0,
        "recency_max_boost": 1.15,
        "tag": None,
        "all_tags": None,
        "no_tag": None,
        "tool": None,
        "tool_tag": None,
        "owner": None,
        "fts": False,
        "semantic": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_has_explicit_formatter():
    assert _has_explicit_formatter(make_args(json=True))
    assert not _has_explicit_formatter(make_args())


def test_search_missing_db(tmp_path, capsys):
    args = make_args(query=["hi"], db=str(tmp_path / "missing.db"))
    assert cmd_search(args) == 1
    assert "Database not found" in capsys.readouterr().out


def test_search_empty_query_shows_usage(test_db, capsys):
    args = make_args(query=[], db=str(test_db))
    assert cmd_search(args) == 1
    assert "Usage:" in capsys.readouterr().out


def test_search_json_refs_rejected(test_db, capsys):
    args = make_args(query=["x"], db=str(test_db), json=True, refs=True)
    assert cmd_search(args) == 1
    assert "--refs is not supported with --json" in capsys.readouterr().err


def test_search_mutually_exclusive_modes(test_db, capsys):
    args = make_args(query=["x"], db=str(test_db), fts=True, semantic=True)
    assert cmd_search(args) == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_search_index_requires_embeddings(test_db, monkeypatch, capsys):
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)
    args = make_args(db=str(test_db), index=True)
    assert cmd_search(args) == 1
    assert "requires the [embed] extra" in capsys.readouterr().err


def test_search_semantic_requires_embeddings(test_db, monkeypatch, capsys):
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)
    args = make_args(query=["x"], db=str(test_db), semantic=True)
    assert cmd_search(args) == 1
    assert "requires the [embed] extra" in capsys.readouterr().err


def test_search_falls_back_to_fts_mode(test_db, monkeypatch, capsys):
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)
    args = make_args(query=["needle"], db=str(test_db))
    assert cmd_search(args) == 0
    out = capsys.readouterr()
    assert "[FTS5 mode" in out.err


def test_print_empty_json_results_dict_and_str(monkeypatch, capsys, tmp_path):
    class _Fmt:
        def __init__(self, out):
            self._out = out

        def render_search(self, *_a, **_k):
            return self._out

    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: _Fmt({"results": []}))
    _print_empty_json_results(make_args(json=True), "q", tmp_path / "x.db")
    assert json.loads(capsys.readouterr().out)["results"] == []

    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: _Fmt("EMPTY"))
    _print_empty_json_results(make_args(json=True), "q", tmp_path / "x.db")
    assert "EMPTY" in capsys.readouterr().out


def test_can_delegate_to_serve(monkeypatch, tmp_path):
    db = tmp_path / "db.sqlite"
    embed = tmp_path / "embed.sqlite"
    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: True)
    monkeypatch.setattr("siftd.cli.search.embeddings_db_path", lambda: embed)
    assert _can_delegate_to_serve(make_args(embed_db=None), db=db, embed_db=embed)
    assert not _can_delegate_to_serve(
        make_args(embed_db=str(tmp_path / "other.db")),
        db=db,
        embed_db=tmp_path / "actual-other-embed.db",
    )


def test_fetch_metadata_aggregate_and_tiers(monkeypatch):
    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Cur([{"id": "c1", "started_at": "2024-01-01", "workspace": "/w"}])

    results = [{"conversation_id": "c1", "score": 0.9, "text": "x", "source_ids": []}]
    _fetch_search_metadata(_Conn(), results)
    assert results[0]["_started_at"] == "2024-01-01"

    conv = _aggregate_conversations(results, limit=5)
    assert conv[0]["conversation_id"] == "c1"

    t1, t2 = _compute_thread_tiers([
        {"conversation_id": "a", "score": 0.9, "_started_at": "2024-01-01"},
        {"conversation_id": "b", "score": 0.1, "_started_at": "2024-01-02"},
    ])
    assert len(t1) == 1 and len(t2) == 1


def test_enrich_exchanges_and_context(monkeypatch):
    monkeypatch.setattr("siftd.api.search.fetch_prompt_response_texts", lambda conn, ids: [(i, "p", "r") for i in ids])

    rows = [("p1",), ("p2",), ("p3",)]

    class _Cur:
        def fetchall(self):
            return rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Cur()

    rs = [{"conversation_id": "c1", "source_ids": ["p2"]}, {"conversation_id": "c2", "source_ids": []}]
    _enrich_exchanges(_Conn(), rs)
    assert rs[0]["_exchanges"]

    _enrich_context(_Conn(), rs, 1)
    assert rs[0]["_context"]


def test_search_build_index_error_paths(monkeypatch, tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    embed = tmp_path / "embed.sqlite"

    class _IncErr(RuntimeError):
        pass

    # Avoid importing real embeddings.indexer in this no-embed test
    monkeypatch.setitem(
        __import__("sys").modules,
        "siftd.embeddings.indexer",
        SimpleNamespace(IncrementalCompatError=_IncErr),
    )

    monkeypatch.setattr("siftd.api.build_index", lambda **k: (_ for _ in ()).throw(FileNotFoundError("missing")))
    assert _search_build_index(db, embed, rebuild=False, backend_name=None, verbose=True) == 1

    monkeypatch.setattr("siftd.api.build_index", lambda **k: (_ for _ in ()).throw(_IncErr("bad")))
    assert _search_build_index(db, embed, rebuild=False, backend_name=None, verbose=True) == 1

    monkeypatch.setattr("siftd.api.build_index", lambda **k: (_ for _ in ()).throw(RuntimeError("oops")))
    assert _search_build_index(db, embed, rebuild=False, backend_name=None, verbose=True) == 1

    monkeypatch.setattr("siftd.api.build_index", lambda **k: {"chunks_added": 0, "total_chunks": 7})
    assert _search_build_index(db, embed, rebuild=False, backend_name=None, verbose=True) == 0
    assert "up to date" in capsys.readouterr().out


def test_search_fts_only_branches(monkeypatch, tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    db.write_text("x")
    args = make_args(query=["q"], db=str(db), json=True, thread=True)

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.search.resolve_candidates", lambda *a, **k: None)

    # error path: fts table missing
    class _OpErr(Exception):
        pass

    import sqlite3

    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: content_fts")))
    assert _search_fts_only(args, db, "q") == 1

    # empty result path with warnings in json
    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: [])
    assert _search_fts_only(args, db, "q") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "fts5"

    # non-empty path
    monkeypatch.setattr(
        "siftd.api.search.fts5_search_content",
        lambda *a, **k: [{"conversation_id": "c1", "rank": -0.3, "snippet": "x", "side": "prompt"}],
    )
    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_search=lambda *a, **k2: {"results": [1]}))
    monkeypatch.setattr("siftd.cli.search._fetch_search_metadata", lambda conn, results: None)
    assert _search_fts_only(make_args(query=["q"], db=str(db), json=True), db, "q") == 0
    assert "fts5" in capsys.readouterr().out.lower()
