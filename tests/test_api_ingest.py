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


class TestIngestLock:
    """Concurrent ingests corrupt each other's bookkeeping, so they serialize.

    Two processes that both parse the same changed transcript both insert the
    same conversation; one loses the UNIQUE race (kgruel/siftd#29). The lock
    removes the race at the source, and a locked-out run is a quiet no-op —
    skipping is correct when an ingest of the same sources is already running.
    """

    @staticmethod
    def _wire(monkeypatch, stats):
        """Make run_ingest's body cheap: fake connection, one fake adapter."""
        conn = _FakeConn()
        monkeypatch.setattr(ingest_api, "create_database", lambda path: conn)
        monkeypatch.setattr(
            ingest_api,
            "load_all_adapters",
            lambda **_kw: [SimpleNamespace(name="claude_code", module="mod:claude")],
        )
        monkeypatch.setattr(
            ingest_api,
            "ingest_all",
            lambda _conn, adapters, on_event=None, filter_binary=None: stats,
        )
        return conn

    def test_second_invocation_skips_quietly(self, tmp_path, monkeypatch):
        db = tmp_path / "locked.db"
        self._wire(monkeypatch, IngestStats(files_found=1))

        # flock conflicts across file descriptors, so an in-process holder is
        # indistinguishable from another process here.
        with ingest_api._ingest_lock(db) as held:
            assert held is True
            result = ingest_api.run_ingest(db_path=db)

        assert result.skipped_locked is True
        assert result.stats is None
        assert result.db_created is False
        assert not db.exists(), "a locked-out run must not create the database"

    def test_lock_is_released_for_the_next_run(self, tmp_path, monkeypatch):
        db = tmp_path / "locked.db"
        stats = IngestStats(files_found=1)
        self._wire(monkeypatch, stats)

        with ingest_api._ingest_lock(db) as held:
            assert held is True
        result = ingest_api.run_ingest(db_path=db)

        assert result.skipped_locked is False
        assert result.stats is stats

    def test_different_databases_do_not_block_each_other(self, tmp_path, monkeypatch):
        stats = IngestStats(files_found=1)
        self._wire(monkeypatch, stats)

        with ingest_api._ingest_lock(tmp_path / "one.db") as held:
            assert held is True
            result = ingest_api.run_ingest(db_path=tmp_path / "two.db")

        assert result.skipped_locked is False
        assert result.stats is stats

    def test_same_database_spelled_differently_contends(self, tmp_path):
        db = tmp_path / "sub" / "same.db"
        db.parent.mkdir()
        alias = tmp_path / "sub" / ".." / "sub" / "same.db"

        with ingest_api._ingest_lock(db) as held:
            assert held is True
            with ingest_api._ingest_lock(alias) as second:
                assert second is False

    def test_lock_is_held_for_the_whole_ingest(self, tmp_path, monkeypatch):
        """The lock must cover the work, not just the entry.

        Every other test here takes the lock itself and calls run_ingest as the
        loser, which only pins "run_ingest respects someone else's lock". Hoist
        the ingest out of the ``with`` block and all of them still pass while
        the race is fully back — so this one runs as the winner and has a second
        acquirer try mid-ingest. flock contends across file descriptors, so no
        threads, subprocesses or sleeps are involved.
        """
        db = tmp_path / "held.db"
        stats = IngestStats(files_found=1)
        self._wire(monkeypatch, stats)
        seen = {}

        def _ingest_all(_conn, adapters, on_event=None, filter_binary=None):
            with ingest_api._ingest_lock(db) as second:
                seen["during"] = second
            return stats

        monkeypatch.setattr(ingest_api, "ingest_all", _ingest_all)

        result = ingest_api.run_ingest(db_path=db)

        assert result.skipped_locked is False
        assert seen["during"] is False, "the lock was not held during the ingest"
        # ...and released afterwards.
        with ingest_api._ingest_lock(db) as after:
            assert after is True

    def test_lock_is_released_before_the_auto_index(self, tmp_path, monkeypatch):
        """The lock covers the database phase, not the embedding that follows it.

        Auto-indexing a stale set against a rate-limited remote backend runs for
        minutes after every database write has landed. Holding the lock across it
        made every overlapping run — a cron tick, a scoped `--path` request — a
        silent `skipped_locked`, which is the missed-ingest shape this release
        exists to remove. So: a second run must proceed *while* the auto-index is
        running, and must actually ingest rather than skip.
        """
        db = tmp_path / "phased.db"
        stats = IngestStats(files_found=1)
        self._wire(monkeypatch, stats)
        seen = {}

        def _auto_index(path, embed_db_path=None, *, on_notice=None):
            with ingest_api._ingest_lock(db) as during:
                seen["lock_free"] = during
            # A real overlapping invocation, not just the lock probe: it must
            # reach ingest_all rather than return skipped_locked.
            seen["nested"] = ingest_api.run_ingest(db_path=db)
            return ingest_api.AutoIndexReport(ran=True, chunks_added=3)

        monkeypatch.setattr(ingest_api, "_maybe_auto_index", _auto_index)

        result = ingest_api.run_ingest(db_path=db)

        assert seen["lock_free"] is True, "the lock was still held during auto-index"
        assert seen["nested"].skipped_locked is False
        assert seen["nested"].stats is stats
        # The outer run still reports its own auto-index, and the elapsed clock
        # still covers the embedding step it waited for.
        assert result.skipped_locked is False
        assert result.auto_index.chunks_added == 3
        assert result.elapsed_ms >= 0

    def test_database_phase_still_locks_out_an_overlapping_run(self, tmp_path, monkeypatch):
        """Narrowing the scope must not narrow it past the writes it protects.

        The sibling above would also pass if the lock were removed entirely, so
        pin the other end: during `ingest_all` itself, a second run is still a
        quiet no-op.
        """
        db = tmp_path / "phased2.db"
        stats = IngestStats(files_found=1)
        self._wire(monkeypatch, stats)
        seen = {}

        def _ingest_all(_conn, adapters, on_event=None, filter_binary=None):
            seen["nested"] = ingest_api.run_ingest(db_path=db)
            return stats

        monkeypatch.setattr(ingest_api, "ingest_all", _ingest_all)
        monkeypatch.setattr(
            ingest_api, "_maybe_auto_index", lambda *_a, **_kw: None
        )

        result = ingest_api.run_ingest(db_path=db)

        assert result.skipped_locked is False
        assert seen["nested"].skipped_locked is True
        assert seen["nested"].stats is None

    def test_unlockable_filesystem_degrades_with_a_warning(self, tmp_path, monkeypatch, caplog):
        """Degrading is deliberate; degrading silently is not.

        An NFS/CIFS mount that refuses flock leaves both ingests racing exactly
        as before the lock existed. Nothing else — not --json, not the summary,
        not doctor — says so, so the log line is the only signal a user has that
        their ingests are not serialized.
        """
        import errno
        import fcntl
        import logging

        def _refuse(*_args, **_kwargs):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(fcntl, "flock", _refuse)

        db = tmp_path / "nfs.db"
        with caplog.at_level(logging.WARNING, logger="siftd.api.ingest"):
            with ingest_api._ingest_lock(db) as held:
                assert held is True
                with ingest_api._ingest_lock(db) as second:
                    assert second is True, "unlockable means unserialized, by design"

        assert "unserialized" in caplog.text
        assert str(db) in caplog.text

    def test_missing_fcntl_degrades_to_unlocked(self, tmp_path, monkeypatch):
        """No advisory lock on a platform without flock — never a hard failure."""
        import builtins

        real_import = builtins.__import__

        def _no_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("no fcntl here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_fcntl)

        with ingest_api._ingest_lock(tmp_path / "x.db") as held:
            assert held is True
            with ingest_api._ingest_lock(tmp_path / "x.db") as second:
                assert second is True
