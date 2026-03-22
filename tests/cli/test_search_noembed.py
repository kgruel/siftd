"""Non-embeddings tests for siftd.cli.search command paths."""

import argparse

from siftd.cli.search import _has_explicit_formatter, cmd_search


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
