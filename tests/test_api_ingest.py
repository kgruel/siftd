"""Tests for siftd.api.ingest."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from siftd.api import ingest as ingest_api
from siftd.ingestion import IngestStats


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_run_ingest_returns_result_and_writes_stats_cache(tmp_path, monkeypatch):
    db = tmp_path / "ingest.db"
    conn = _FakeConn()
    stats = IngestStats(files_found=2, files_ingested=1)

    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(
        ingest_api,
        "load_all_adapters",
        lambda **_kw: [SimpleNamespace(name="claude_code", module="mod:claude")],
    )
    monkeypatch.setattr(
        ingest_api,
        "ingest_all",
        lambda _conn, adapters, on_event=None, filter_binary=None: (
            stats if adapters == ["mod:claude"] else None
        ),
    )

    cache_calls = []
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda db_path: {"db": str(db_path)})
    # write_stats_cache now takes a db_mtime_ns kwarg (captured before the sweep);
    # the mock must absorb it or the call raises and the cache refresh is swallowed.
    monkeypatch.setattr(
        "siftd.api.stats.write_stats_cache",
        lambda payload, **_kw: cache_calls.append(payload),
    )

    result = ingest_api.run_ingest(db_path=db, adapter_names=["claude_code"])

    assert result.mode == "ingest"
    assert result.db_created is True
    assert result.adapters == ["claude_code"]
    assert result.scan_paths == []
    assert result.stats is stats
    assert result.elapsed_ms >= 0
    assert cache_calls == [{"db": str(db)}]
    assert conn.closed is True


def test_run_ingest_with_scan_paths_wraps_adapters(tmp_path, monkeypatch):
    db = tmp_path / "ingest.db"
    conn = _FakeConn()
    stats = IngestStats(files_found=1)

    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(
        ingest_api,
        "load_all_adapters",
        lambda **_kw: [SimpleNamespace(name="aider", module="mod:aider")],
    )
    monkeypatch.setattr(
        ingest_api,
        "wrap_adapter_paths",
        lambda module, paths: f"wrapped:{module}:{','.join(paths)}",
    )

    seen = {}

    def _fake_ingest(_conn, adapters, on_event=None, filter_binary=None):
        seen["adapters"] = adapters
        return stats

    monkeypatch.setattr(ingest_api, "ingest_all", _fake_ingest)
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda db_path: {})
    monkeypatch.setattr("siftd.api.stats.write_stats_cache", lambda payload: None)

    result = ingest_api.run_ingest(db_path=db, scan_paths=["/logs"])

    assert seen["adapters"] == ["wrapped:mod:aider:/logs"]
    assert result.scan_paths == ["/logs"]


def test_run_ingest_unknown_adapter_raises_and_closes_connection(tmp_path, monkeypatch):
    db = tmp_path / "ingest.db"
    conn = _FakeConn()

    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(
        ingest_api,
        "load_all_adapters",
        lambda **_kw: [SimpleNamespace(name="claude_code", module="mod")],
    )

    with pytest.raises(ingest_api.AdapterSelectionError) as exc:
        ingest_api.run_ingest(db_path=db, adapter_names=["missing"])

    assert str(exc.value) == "No adapters matched: missing"
    assert conn.closed is True


def _fake_load_with_disabled(disabled_names, enabled_plugins):
    def _load(**kw):
        out = kw.get("disabled_out")
        if out is not None:
            out.extend(disabled_names)
        return enabled_plugins

    return _load


def test_run_ingest_skips_disabled_adapter_and_reports_it(tmp_path, monkeypatch):
    db = tmp_path / "ingest.db"
    conn = _FakeConn()
    stats = IngestStats(files_found=0)

    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(
        ingest_api,
        "load_all_adapters",
        _fake_load_with_disabled(
            ["aider"], [SimpleNamespace(name="claude_code", module="mod:claude")]
        ),
    )
    seen = {}

    def _fake_ingest(_conn, adapters, on_event=None, filter_binary=None):
        seen["adapters"] = adapters
        return stats

    monkeypatch.setattr(ingest_api, "ingest_all", _fake_ingest)
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda db_path: {})
    monkeypatch.setattr("siftd.api.stats.write_stats_cache", lambda payload, **_kw: None)

    result = ingest_api.run_ingest(db_path=db)

    assert seen["adapters"] == ["mod:claude"]
    assert result.adapters == ["claude_code"]
    assert result.disabled_adapters == ["aider"]


def test_run_ingest_explicit_disabled_adapter_raises_with_hint(tmp_path, monkeypatch):
    db = tmp_path / "ingest.db"
    conn = _FakeConn()

    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(
        ingest_api,
        "load_all_adapters",
        _fake_load_with_disabled(
            ["aider"], [SimpleNamespace(name="claude_code", module="mod:claude")]
        ),
    )

    with pytest.raises(ingest_api.AdapterSelectionError) as exc:
        ingest_api.run_ingest(db_path=db, adapter_names=["aider"])

    assert exc.value.disabled == ["aider"]
    assert "disabled via config" in str(exc.value)
    assert conn.closed is True


def test_run_rebuild_fts_returns_result(tmp_path, monkeypatch):
    db = tmp_path / "ingest.db"
    conn = _FakeConn()
    calls = []

    monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
    monkeypatch.setattr(ingest_api, "rebuild_fts_index", lambda _conn: calls.append("rebuilt"))

    result = ingest_api.run_rebuild_fts(db_path=db)

    assert result.mode == "rebuild_fts"
    assert result.stats is None
    assert calls == ["rebuilt"]
    assert conn.closed is True
