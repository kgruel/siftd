"""Non-embeddings tests for siftd.cli.search command paths."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from siftd.cli.search import (
    _can_delegate_to_serve,
    _print_empty_json_results,
    _search_fts_only,
    cmd_search,
)
from siftd.domain.search_types import SearchView


@pytest.fixture(autouse=True)
def _route_embed_db(monkeypatch, tmp_path):
    """Point embeddings_db_path() at tmp_path/embed.db for the whole module.

    `siftd search` no longer takes `--embed-db`, so tests toggle "has embeddings" by
    creating (or not) tmp_path/"embed.db" — the path this fixture resolves to.
    """
    p = tmp_path / "embed.db"
    monkeypatch.setattr("siftd.paths.embeddings_db_path", lambda: p)
    monkeypatch.setattr("siftd.cli.search.embeddings_db_path", lambda: p)


def make_args(**kwargs):
    defaults = {
        "query": [],
        "db": None,
        "embed_db": None,
        "limit": 10,
        "verbose": False,
        "full": False,
        "select": "all",
        "sort": "score",
        "mode": "auto",
        "view": "chunks",
        "workspace": None,
        "model": None,
        "since": None,
        "before": None,
        "index": False,
        "rebuild": False,
        "backend": None,
        "recall": 80,
        "role": None,
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
        "debug_ids": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)



def test_search_missing_db(tmp_path, capsys):
    args = make_args(query=["hi"], db=str(tmp_path / "missing.db"))
    assert cmd_search(args) == 1
    assert "Database not found" in capsys.readouterr().err


def test_search_empty_query_shows_usage(test_db, capsys):
    args = make_args(query=[], db=str(test_db))
    assert cmd_search(args) == 1
    assert "A search query or tag filter is required" in capsys.readouterr().err


def test_search_json_refs_rejected(test_db, capsys):
    args = make_args(query=["x"], db=str(test_db), json=True, refs=True)
    assert cmd_search(args) == 1
    assert "--refs is not supported with --json" in capsys.readouterr().err


def test_search_removed_index_flags_redirect_to_embed(capsys):
    """--index/--rebuild/--backend/--embed-db were removed from search → redirect hint."""
    from siftd.cli import main

    for flag in ("--index", "--rebuild", "--backend", "--embed-db"):
        with pytest.raises(SystemExit) as exc:
            main(["search", flag] if flag in ("--index", "--rebuild") else ["search", flag, "x"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "siftd embed" in err


def test_search_semantic_requires_embeddings(test_db, monkeypatch, capsys):
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)
    args = make_args(query=["x"], db=str(test_db), mode="semantic")
    assert cmd_search(args) == 1
    assert "requires an embedding backend" in capsys.readouterr().err


def test_search_falls_back_to_fts_mode(test_db, monkeypatch, capsys):
    # FTS5 mode fallback is now surfaced via the search-mode-degraded caveat producer
    # (channel="text", only fires on non-empty results). Empty-result path: silent fallback.
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)
    args = make_args(query=["needle"], db=str(test_db))
    assert cmd_search(args) == 0


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
    # Delegation now hinges only on can_delegate(db=...); the embed-db override guard
    # is gone (no `--embed-db` on search).
    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: True)
    assert _can_delegate_to_serve(make_args(), db=db)
    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: False)
    assert not _can_delegate_to_serve(make_args(), db=db)


def test_api_metadata_aggregate_and_tiers():
    """The post-processing recipe's primitives now live in api.search; exercise
    them directly (the CLI composes them via process_search_view)."""
    from siftd.api.search import (
        aggregate_by_conversation,
        compute_thread_tiers,
        enrich_search_metadata,
    )
    from siftd.domain.search_types import SearchChunk

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Cur([{"id": "c1", "started_at": "2024-01-01", "workspace": "/w"}])

    chunks = [SearchChunk(conversation_id="c1", score=0.9, text="x", chunk_type="prompt", source_ids=[])]
    enrich_search_metadata(_Conn(), chunks)
    assert chunks[0]["_started_at"] == "2024-01-01"

    convs = aggregate_by_conversation(chunks, limit=5)
    assert convs[0].conversation_id == "c1"

    t1, t2 = compute_thread_tiers([
        SearchChunk(conversation_id="a", score=0.9, text="", chunk_type="prompt", started_at="2024-01-01"),
        SearchChunk(conversation_id="b", score=0.1, text="", chunk_type="prompt", started_at="2024-01-02"),
    ])
    assert len(t1) == 1 and len(t2) == 1


def test_enrich_exchanges(monkeypatch):
    from siftd.api.search import enrich_exchanges
    from siftd.domain.search_types import SearchChunk

    monkeypatch.setattr("siftd.api.search.fetch_prompt_response_texts", lambda conn, ids: [(i, "p", "r") for i in ids])

    chunks = [
        SearchChunk(conversation_id="c1", score=0.0, text="", chunk_type="exchange", source_ids=["p2"]),
        SearchChunk(conversation_id="c2", score=0.0, text="", chunk_type="exchange", source_ids=[]),
    ]
    sentinel = [("untouched", "", "")]
    chunks[1].exchanges = sentinel  # source_ids empty → must be left as-is, not the None default
    enrich_exchanges(None, chunks)
    assert chunks[0].exchanges == [("p2", "p", "r")]
    assert chunks[1].exchanges is sentinel



def test_search_fts_only_branches(monkeypatch, tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    db.write_text("x")
    args = make_args(
        query=["q"], db=str(db), json=True, view="thread",
        full=True, verbose=True, select="first",
        refs=True, sort="time",
    )

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.search.resolve_candidates", lambda *a, **k: None)

    # invalid --format fails fast (before any search), matching cmd_search
    assert _search_fts_only(make_args(query=["q"], db=str(db), format="bogus"), db, "q") == 1
    assert "Unknown format" in capsys.readouterr().err

    # error path: fts table missing
    import sqlite3

    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: content_fts")))
    assert _search_fts_only(args, db, "q") == 1

    # empty result path with warnings in json
    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: [])
    assert _search_fts_only(args, db, "q") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "fts"
    assert out["warnings"]

    # non-empty path
    monkeypatch.setattr(
        "siftd.api.search.fts5_search_content",
        lambda *a, **k: [{"conversation_id": "c1", "rank": -0.3, "snippet": "x", "kind": "prompt"}],
    )
    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_search=lambda *a, **k2: {"results": [1]}))
    monkeypatch.setattr("siftd.api.search.enrich_search_metadata", lambda conn, chunks: None)
    assert _search_fts_only(make_args(query=["q"], db=str(db), json=True), db, "q") == 0
    assert "results" in capsys.readouterr().out


def test_cmd_search_execute_error_paths(test_db, tmp_path, monkeypatch, capsys):
    embed = tmp_path / "embed.db"
    embed.write_text("x")
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed))) == 1
    assert "boom" in capsys.readouterr().err

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(ValueError("bad")))
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed))) == 1
    assert "bad" in capsys.readouterr().err


def test_cmd_search_threshold_and_first_json_empty(test_db, tmp_path, monkeypatch):
    embed = tmp_path / "embed.db"
    embed.write_text("x")
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)

    called = []
    monkeypatch.setattr("siftd.cli.search._print_empty_json_results", lambda *a, **k: called.append(True))

    # threshold empties the recipe → search_view returns empty_reason="threshold"
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: SearchView(results=[], view="chunks", empty_reason="threshold"))
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), json=True, threshold=0.9)) == 0
    assert called

    # select=first with no match → empty_reason="first"
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: SearchView(results=[], view="chunks", empty_reason="first"))
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), json=True, select="first")) == 0


def test_cmd_search_mode_processing_and_refs(test_db, tmp_path, monkeypatch):
    embed = tmp_path / "embed.db"
    embed.write_text("x")
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    monkeypatch.setattr(
        "siftd.api.dispatch.execute",
        lambda op: SearchView(
            results=[{"conversation_id": "c1", "score": 0.9, "source_ids": ["p1"], "chunk_id": "ch1", "text": "hello", "display_label": "USER", "file_refs": [{"basename": "a.py"}]}],
            view="chunks",
        ),
    )

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.api.fetch_file_refs", lambda conn, ids: {"p1": [{"basename": "a.py"}]})
    monkeypatch.setattr("siftd.api.search.enrich_search_metadata", lambda conn, chunks: None)
    monkeypatch.setattr("siftd.api.search.enrich_exchanges", lambda conn, chunks: None)
    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_search=lambda *a, **k2: "OUT"))
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
    refs_called = []
    monkeypatch.setattr("siftd.output.common.print_refs_content", lambda refs, filt: refs_called.append((refs, filt)))

    # chunks mode: sort=time + full + refs path
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), sort="time", full=True, refs="a.py,b.py")) == 0
    assert refs_called

    # thread mode branch
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), view="thread")) == 0

    # conversations view branch
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), view="conversations")) == 0


def test_cmd_search_threads_tool_filters(test_db, monkeypatch):
    # --tool / --tool-tag ride the search Operation onto search_view (3b-1).
    captured = {}

    def _capture(op):
        captured.update(op.params)
        return SearchView(results=[], view="chunks"), []

    monkeypatch.setattr("siftd.api.dispatch.execute_for_render", _capture)
    monkeypatch.setattr(
        "siftd.cli.search._can_delegate_to_serve", lambda *a, **k: False
    )

    # fts mode takes the _search_fts_only path; auto resolves to fts without
    # embeddings — both must thread the tool filters.
    assert cmd_search(
        make_args(query=["x"], db=str(test_db), mode="fts",
                  tool="shell.execute", tool_tag="shell:test")
    ) == 0
    assert captured["tool"] == "shell.execute"
    assert captured["tool_tag"] == "shell:test"

    captured.clear()
    assert cmd_search(
        make_args(query=["x"], db=str(test_db), mode="auto",
                  tool="shell.execute", tool_tag="shell:test")
    ) == 0
    assert captured["tool"] == "shell.execute"
    assert captured["tool_tag"] == "shell:test"


def test_misc_remaining_helper_branches(monkeypatch):
    # _can_delegate_to_serve: can_delegate False
    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: False)
    assert not _can_delegate_to_serve(make_args(), db=Path("/x"))

    # enrich_search_metadata: empty conv list early return (no query issued)
    from siftd.api.search import enrich_search_metadata

    class _Conn:
        def execute(self, *_a, **_k):
            raise AssertionError("should not query for empty results")

    enrich_search_metadata(_Conn(), [])



def test_cmd_search_semantic_and_output_edges(test_db, tmp_path, monkeypatch, capsys):
    embed = tmp_path / "embed.db"

    # semantic requested but embed db missing — human text on stderr so stdout
    # stays clean for --json | jq (I15).
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    args = make_args(query=["x"], db=str(test_db), mode="semantic", embed_db=str(embed))
    assert cmd_search(args) == 1
    assert "No embeddings index found" in capsys.readouterr().err

    # I15: with --json, stdout carries a parseable error envelope (not prose),
    # so `siftd search --semantic q --json | jq` does not abort.
    import json as _json
    args = make_args(query=["x"], db=str(test_db), mode="semantic", embed_db=str(embed), json=True)
    assert cmd_search(args) == 1
    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert "error" in payload and "embeddings index" in payload["error"]

    # json+mode=thread warning
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: SearchView(results=[], view="chunks"))
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: False)
    assert cmd_search(make_args(query=["x"], db=str(test_db), json=True, view="thread")) == 0
    assert "ignored with --json output" in capsys.readouterr().err

    # line 342: empty json result helper in cmd_search
    called = []
    monkeypatch.setattr("siftd.cli.search._print_empty_json_results", lambda *a, **k: called.append(True))
    assert cmd_search(make_args(query=["x"], db=str(test_db), json=True)) == 0
    assert called

    # lines 354/366: non-json threshold/first no-match messages
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    embed.write_text("x")
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: SearchView(results=[], view="chunks", empty_reason="threshold"))
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), threshold=0.9)) == 0
    assert "No results above threshold" in capsys.readouterr().err

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: SearchView(results=[], view="chunks", empty_reason="first"))
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), select="first")) == 0
    assert "No results above relevance threshold" in capsys.readouterr().err


def test_cmd_search_delegate_and_format_error(test_db, tmp_path, monkeypatch, capsys):
    embed = tmp_path / "embed.db"
    embed.write_text("x")
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    monkeypatch.setattr("siftd.cli.search._can_delegate_to_serve", lambda *a, **k: True)
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: [{"conversation_id": "c1", "score": 1.0, "source_ids": [], "chunk_id": "c", "text": "t"}])

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: (_ for _ in ()).throw(ValueError("bad fmt")))

    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed))) == 1
    assert "bad fmt" in capsys.readouterr().err


def test_search_fts_only_sort_time_orders_results(monkeypatch, tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    db.write_text("x")

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.search.resolve_candidates", lambda *a, **k: None)
    monkeypatch.setattr(
        "siftd.api.search.fts5_search_content",
        lambda *a, **k: [
            {"conversation_id": "c2", "rank": -0.9, "snippet": "later", "kind": "prompt"},
            {"conversation_id": "c1", "rank": -0.8, "snippet": "earlier", "kind": "prompt"},
        ],
    )
    monkeypatch.setattr(
        "siftd.api.search.enrich_search_metadata",
        lambda conn, chunks: [
            setattr(c, "started_at", "2024-01-02" if c.conversation_id == "c2" else "2024-01-01")
            for c in chunks
        ],
    )
    monkeypatch.setattr(
        "siftd.output.format_registry.select_format",
        lambda **k: SimpleNamespace(render_search=lambda sv, *_a, **_k: {"results": sv.results}),
    )

    args = make_args(query=["q"], db=str(db), json=True, sort="time")
    assert _search_fts_only(args, db, "q") == 0
    out = json.loads(capsys.readouterr().out)
    # --sort=time is newest-first (9c8a35f8): c2 (2024-01-02) leads c1 (2024-01-01).
    assert [r["conversation_id"] for r in out["results"]] == ["c2", "c1"]


def test_search_fts_only_additional_error_and_warning_branches(monkeypatch, tmp_path, capsys):
    db = tmp_path / "db.sqlite"
    db.write_text("x")

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.search.resolve_candidates", lambda *a, **k: None)

    import sqlite3

    # invalid syntax branch (541-543)
    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("fts5 syntax error")))
    assert _search_fts_only(make_args(query=["q"], db=str(db)), db, "q") == 1
    assert "Invalid search query" in capsys.readouterr().err

    # generic db error branch (545)
    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("other fail")))
    assert _search_fts_only(make_args(query=["q"], db=str(db)), db, "q") == 1
    assert "Database error" in capsys.readouterr().err

    # warning injection in dict output branch (607)
    args = make_args(query=["q"], db=str(db), json=True, view="thread")
    monkeypatch.setattr(
        "siftd.api.search.fts5_search_content",
        lambda *a, **k: [{"conversation_id": "c1", "rank": -0.3, "snippet": "x", "kind": "prompt"}],
    )
    monkeypatch.setattr("siftd.api.search.enrich_search_metadata", lambda conn, chunks: None)
    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_search=lambda *a, **k2: {"results": []}))
    assert _search_fts_only(args, db, "q") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["warnings"]


def test_cmd_search_semantic_mode_path(test_db, tmp_path, monkeypatch):
    embed = tmp_path / "embed.db"
    embed.write_text("x")
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: SearchView(results=[], view="chunks"))
    # mode=semantic with existing embed db reaches the search_mode='semantic' resolution
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), mode="semantic")) == 0


def test_cmd_search_first_result_kept_branch(test_db, tmp_path, monkeypatch):
    embed = tmp_path / "embed.db"
    embed.write_text("x")
    monkeypatch.setattr("siftd.embeddings.embeddings_available", lambda: True)
    monkeypatch.setattr(
        "siftd.api.dispatch.execute",
        lambda op: SearchView(
            results=[{"conversation_id": "c1", "score": 0.9, "source_ids": [], "chunk_id": "ch1", "text": "hello", "display_label": "USER"}],
            view="chunks",
        ),
    )

    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.api.search.enrich_search_metadata", lambda conn, chunks: None)
    monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_search=lambda *a, **k2: "OUT"))
    monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
    assert cmd_search(make_args(query=["x"], db=str(test_db), embed_db=str(embed), select="first")) == 0


def test_empty_search_json_results_with_caveats(test_db, monkeypatch, capsys):
    """Empty JSON results include caveats key in output."""
    from siftd.doctor.checks import Finding

    stub_caveat = Finding(
        check="test-caveat",
        severity="info",
        message="Test caveat message",
        fix_available=False,
    )

    monkeypatch.setattr("siftd.api.dispatch.execute_for_render", lambda op: (SearchView(results=[], view="chunks"), [stub_caveat]))

    args = make_args(query=["nonexistent"], db=str(test_db), json=True)
    assert cmd_search(args) == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "caveats" in data
    assert len(data["caveats"]) == 1
    assert data["caveats"][0]["message"] == "Test caveat message"


def test_empty_search_text_results_with_caveats(test_db, monkeypatch, capsys):
    """Empty text results append caveats as 'note:' lines."""
    from siftd.doctor.checks import Finding

    stub_caveat = Finding(
        check="test-caveat",
        severity="warning",
        message="Index is stale",
        fix_available=False,
    )

    monkeypatch.setattr("siftd.api.dispatch.execute_for_render", lambda op: (SearchView(results=[], view="chunks"), [stub_caveat]))

    args = make_args(query=["nonexistent"], db=str(test_db), json=False)
    assert cmd_search(args) == 0

    captured = capsys.readouterr()
    assert "No results for: nonexistent" in captured.err
    assert "Index is stale" in captured.err


def test_fts_only_empty_results_with_caveats(test_db, monkeypatch, capsys):
    """FTS-only empty results include caveats in JSON output."""
    from siftd.doctor.checks import Finding

    stub_caveat = Finding(
        check="fts-stale",
        severity="warning",
        message="FTS index out of sync",
        fix_available=False,
    )

    monkeypatch.setattr("siftd.api.dispatch.execute_for_render", lambda op: (SearchView(results=[], view="chunks"), [stub_caveat]))
    monkeypatch.setattr("siftd.api.search.fts5_search_content", lambda *a, **k: [])

    args = make_args(query=["nonexistent"], db=str(test_db), json=True, mode="fts")
    assert _search_fts_only(args, Path(test_db), "nonexistent") == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["mode"] == "fts"
    assert data["results"] == []
    assert "caveats" in data
    assert len(data["caveats"]) == 1
    assert data["caveats"][0]["message"] == "FTS index out of sync"
