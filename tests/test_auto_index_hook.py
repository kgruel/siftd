"""Tests for the post-ingest auto-index hook (base lane — collaborators stubbed).

Covers the gate (configured + auto_index + built), the backlog/unbuilt skip, failure
isolation, the one-time remote first-egress notice, and run_ingest never propagating a hook
failure. No real embedding backend is loaded — embed_status/build_index are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from siftd.api import ingest as ingest_api
from siftd.embeddings.availability import EmbedStatus
from siftd.embeddings.indexer import EmbedIndexStatus


def _index_status(**over) -> EmbedIndexStatus:
    base = dict(
        configured_backend="fastembed", configured_usable=True, configured_reason="ok",
        index_exists=True, needs_rebuild=False, stored_backend="fastembed",
        stored_model="bge", stored_dimension=384, schema_version=2, strategy="exchange-window",
        built_at="2026-07-04T00:00:00Z", total_chunks=100, backend_mismatch=False,
        stored_backend_config="fastembed", chunk_counts={"exchange": 100},
        conversations_indexed=50, conversations_total=50, conversations_stale=0, db_size_bytes=4096,
    )
    base.update(over)
    return EmbedIndexStatus(**base)


@pytest.fixture
def wired(monkeypatch):
    """Default happy wiring: auto on, fastembed usable, index built, stale=3."""
    calls = {}
    monkeypatch.setattr("siftd.config.get_embed_auto_index", lambda: True)
    monkeypatch.setattr(
        "siftd.embeddings.availability.embedding_status",
        lambda: EmbedStatus("fastembed", True, "local", model="bge"),
    )
    monkeypatch.setattr(
        "siftd.api.search.embed_status",
        lambda **_k: _index_status(total_chunks=100, conversations_stale=3),
    )

    def _build(**k):
        calls["build"] = k
        return {"chunks_added": 7, "conversations_indexed": 3, "total_chunks": 107}

    monkeypatch.setattr("siftd.api.search.build_index", _build)
    return calls


def test_auto_index_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.config.get_embed_auto_index", lambda: False)
    assert ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db") is None


def test_no_usable_backend_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.config.get_embed_auto_index", lambda: True)
    monkeypatch.setattr(
        "siftd.embeddings.availability.embedding_status",
        lambda: EmbedStatus(None, False, "no backend"),
    )
    assert ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db") is None


def test_no_stale_returns_none(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "siftd.api.search.embed_status",
        lambda **_k: _index_status(total_chunks=100, conversations_stale=0),
    )
    assert ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db") is None


def test_unbuilt_index_defers_to_explicit_embed(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "siftd.api.search.embed_status",
        lambda **_k: _index_status(total_chunks=0, conversations_stale=5),
    )
    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db")
    assert rep is not None
    assert rep.skipped_reason == "unbuilt" and rep.awaiting == 5 and rep.ran is False
    assert "build" not in wired  # inline indexing must not run


def test_large_backlog_defers(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "siftd.api.search.embed_status",
        lambda **_k: _index_status(total_chunks=500, conversations_stale=250),
    )
    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db")
    assert rep.skipped_reason == "backlog" and rep.awaiting == 250
    assert "build" not in wired


def test_runs_incremental_when_built_and_small(wired, tmp_path):
    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db")
    assert rep.ran is True
    assert rep.chunks_added == 7 and rep.conversations_indexed == 3
    assert rep.skipped_reason is None and rep.error is None
    assert wired["build"]["verbose"] is False  # inline: silent, incremental (no rebuild)


def test_failure_is_isolated_to_report_error(wired, monkeypatch, tmp_path):
    from siftd.embeddings.base import EmbeddingTransientError

    def _raise(**_k):
        raise EmbeddingTransientError("remote:voyage: rate limited (429)")

    monkeypatch.setattr("siftd.api.search.build_index", _raise)
    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db")
    assert rep is not None and rep.ran is False
    assert "rate limited" in rep.error


def test_non_embedding_failure_is_also_isolated(wired, monkeypatch, tmp_path):
    """Isolation is total (finding 2): a non-embedding error (locked DB, malformed body, ONNX
    fault — here a stand-in ValueError) still yields a reported error, never a raise."""
    def _raise(**_k):
        raise ValueError("database is locked")

    monkeypatch.setattr("siftd.api.search.build_index", _raise)
    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db")
    assert rep is not None and rep.ran is False
    assert "database is locked" in rep.error


@pytest.fixture
def remote_wired(wired, monkeypatch, tmp_path):
    """wired, but the backend is a remote provider and a real embed DB exists (for the flag)."""
    from siftd.storage.embeddings import open_embeddings_db

    edb = tmp_path / "e.db"
    open_embeddings_db(edb).close()
    monkeypatch.setattr(
        "siftd.embeddings.availability.embedding_status",
        lambda: EmbedStatus("remote:voyage", True, "remote backend (voyage-4)", model="voyage-4"),
    )
    return edb


def test_first_egress_notice_precedes_the_embed_call(remote_wired, monkeypatch, tmp_path):
    """The disclosure fires through on_notice BEFORE build_index runs (finding 1a)."""
    order = []
    monkeypatch.setattr(
        "siftd.api.search.build_index",
        lambda **_k: order.append("embed") or {"chunks_added": 1, "conversations_indexed": 1, "total_chunks": 2},
    )
    notices = []

    def _on_notice(text):
        order.append("notice")
        notices.append(text)

    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", remote_wired, on_notice=_on_notice)
    assert order == ["notice", "embed"]  # notice strictly before egress
    assert "voyage" in notices[0]
    assert "siftd config set embed.auto_index false" in notices[0]
    assert rep.ran is True


def test_pending_notice_without_callback_skips_auto_index(remote_wired, wired, tmp_path):
    """No callback (programmatic caller) → auto-index is SKIPPED so content never leaves
    before the disclosure has been surfaced live: skipped_reason="notice", the disclosure
    rides the report, the shown-flag is NOT burned, and no embed call happens."""
    from siftd.storage.embeddings import get_meta, open_embeddings_db

    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", remote_wired)  # no on_notice
    assert rep.skipped_reason == "notice" and rep.ran is False and rep.awaiting == 3
    assert rep.notice is not None and "voyage" in rep.notice
    assert "build" not in wired  # nothing egressed

    conn = open_embeddings_db(remote_wired, read_only=True)
    try:
        assert get_meta(conn, "auto_index_egress_notified") is None  # flag not burned
    finally:
        conn.close()


def test_burned_flag_unblocks_callback_less_auto_index(remote_wired, wired, tmp_path):
    """Once ANY surface has shown the notice, a callback-less programmatic run proceeds."""
    ingest_api._maybe_auto_index(tmp_path / "m.db", remote_wired, on_notice=lambda _t: None)
    assert wired.pop("build", None) is not None  # first (disclosed) run embedded

    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", remote_wired)  # no on_notice
    assert rep.ran is True and rep.skipped_reason is None
    assert "build" in wired


def test_first_egress_notice_shown_exactly_once_with_callback(remote_wired, tmp_path):
    """Two runs WITH a callback → emitted+persisted on run 1, silent on run 2 (finding 1c)."""
    seen = []
    ingest_api._maybe_auto_index(tmp_path / "m.db", remote_wired, on_notice=seen.append)
    ingest_api._maybe_auto_index(tmp_path / "m.db", remote_wired, on_notice=seen.append)
    assert len(seen) == 1  # exactly once


def test_local_backend_gets_no_notice(wired, tmp_path):
    rep = ingest_api._maybe_auto_index(tmp_path / "m.db", tmp_path / "e.db", on_notice=lambda _t: None)
    assert rep.notice is None


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _wire_run_ingest(monkeypatch, stats):
    conn = _FakeConn()
    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(
        ingest_api, "load_all_adapters",
        lambda **_kw: [SimpleNamespace(name="claude_code", module="mod")],
    )
    monkeypatch.setattr(
        ingest_api, "ingest_all",
        lambda _c, adapters, on_event=None, filter_binary=None: stats,
    )
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda db_path: {})
    monkeypatch.setattr("siftd.api.stats.write_stats_cache", lambda payload, **_kw: None)
    return conn


def test_run_ingest_attaches_auto_index_report(tmp_path, monkeypatch):
    from siftd.ingestion import IngestStats

    _wire_run_ingest(monkeypatch, IngestStats(files_found=1, files_ingested=1))
    report = ingest_api.AutoIndexReport(ran=True, chunks_added=5, conversations_indexed=2)
    monkeypatch.setattr(ingest_api, "_maybe_auto_index", lambda *_a, **_k: report)

    result = ingest_api.run_ingest(db_path=tmp_path / "m.db", adapter_names=["claude_code"])
    assert result.auto_index is report


def test_run_ingest_isolates_hook_bug(tmp_path, monkeypatch):
    from siftd.ingestion import IngestStats

    _wire_run_ingest(monkeypatch, IngestStats(files_found=1))

    def _boom(*_a, **_k):
        raise RuntimeError("unexpected hook bug")

    monkeypatch.setattr(ingest_api, "_maybe_auto_index", _boom)

    # A hook bug must never undo a completed ingest.
    result = ingest_api.run_ingest(db_path=tmp_path / "m.db", adapter_names=["claude_code"])
    assert result.auto_index is None
    assert result.stats.files_found == 1


def test_auto_index_config_description_is_active(monkeypatch):
    from siftd import config

    entry = next(e for e in config._CONFIG_SCHEMA if e.pattern == "embed.auto_index")
    assert "not yet active" not in entry.description
    assert "steady-state" in entry.description


def test_get_embed_auto_index_reads_config(monkeypatch):
    from siftd import config

    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.get_embed_auto_index() is True  # default
    monkeypatch.setattr(config, "load_config", lambda: {"embed": {"auto_index": False}})
    assert config.get_embed_auto_index() is False
    monkeypatch.setattr(config, "load_config", lambda: {"embed": {"auto_index": "no"}})
    assert config.get_embed_auto_index() is False


def test_render_auto_index_human_states(capsys):
    from siftd.cli.data import _render_auto_index

    _render_auto_index(
        ingest_api.AutoIndexReport(awaiting=12, skipped_reason="unbuilt"),
        json_mode=False, quiet=False, renderer=None,
    )
    err = capsys.readouterr().err
    assert "awaiting embedding" in err and "siftd embed" in err

    _render_auto_index(
        ingest_api.AutoIndexReport(ran=True, chunks_added=9, conversations_indexed=4),
        json_mode=False, quiet=False, renderer=None,
    )
    assert "Embedded" in capsys.readouterr().err

    _render_auto_index(
        ingest_api.AutoIndexReport(error="429 rate limited"),
        json_mode=False, quiet=False, renderer=None,
    )
    err = capsys.readouterr().err
    assert "Auto-index skipped" in err and "429 rate limited" in err


def test_render_auto_index_quiet_and_none_are_silent(capsys):
    from siftd.cli.data import _render_auto_index

    _render_auto_index(None, json_mode=False, quiet=False, renderer=None)
    _render_auto_index(
        ingest_api.AutoIndexReport(ran=True, chunks_added=3),
        json_mode=False, quiet=True, renderer=None,
    )
    assert capsys.readouterr().err == ""


def test_render_auto_index_zero_chunks_still_prints(capsys):
    from siftd.cli.data import _render_auto_index

    # ran=True with chunks_added=0 (new content all filtered away, old chunks swept) must
    # still print an outcome line — not silently vanish (finding 3).
    _render_auto_index(
        ingest_api.AutoIndexReport(ran=True, chunks_added=0, conversations_indexed=2),
        json_mode=False, quiet=False, renderer=None,
    )
    err = capsys.readouterr().err
    assert "Embedded" in err and "2 new conversation" in err and "0 chunks" in err


def test_render_auto_index_does_not_render_notice(capsys):
    from siftd.cli.data import _render_auto_index

    # The notice is surfaced live via on_notice (pre-embed), NOT re-rendered here (finding 1).
    _render_auto_index(
        ingest_api.AutoIndexReport(ran=True, chunks_added=3, notice="auto-indexing sends new conversation content to voyage; disable with 'siftd config set embed.auto_index false'"),
        json_mode=False, quiet=False, renderer=None,
    )
    err = capsys.readouterr().err
    assert "voyage" not in err  # notice not double-rendered
    assert "Embedded" in err


def test_render_auto_index_json_event():
    from siftd.cli.data import _render_auto_index

    emitted = []
    renderer = SimpleNamespace(_emit=lambda d: emitted.append(d))
    _render_auto_index(
        ingest_api.AutoIndexReport(ran=True, chunks_added=3, conversations_indexed=1),
        json_mode=True, quiet=False, renderer=renderer,
    )
    assert emitted[0]["type"] == "auto_index"
    assert emitted[0]["chunks_added"] == 3 and emitted[0]["conversations_indexed"] == 1
