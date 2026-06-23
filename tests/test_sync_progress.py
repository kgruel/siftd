"""Tests for push/pull progress emission (api/sync.py).

The transfer bar is best-effort decoration over the wire, so these pin the
*event stream* the producer emits (the renderer is tested in
test_progress_view.py): the windows count-up, the bisection → indeterminate
sweep flip, the count-up tally, and that a faulty sink never breaks the
transfer. The live rendering itself needs a real TTY + remote — see the slice's
handoff notes.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from siftd.api.sync import (
    SyncError,
    _post_window_with_bisect,
    _PushProgress,
    sync_pull,
    sync_push,
)
from siftd.domain.progress import ProgressEvent
from siftd.domain.sync import SyncRemote
from siftd.storage.sqlite import open_database


def _remote(path="http://srv", name="t", host=None, **kw):
    return SyncRemote(name=name, path=path, host=host, **kw)


def _db(tmp_path, name="l.db"):
    p = tmp_path / name
    open_database(p).close()
    return p


# --- _PushProgress (the accumulator) ---------------------------------------


class TestPushProgressAccumulator:
    def test_start_paints_the_empty_bar(self):
        seen: list[ProgressEvent] = []
        p = _PushProgress(seen.append, windows_total=3)
        p.start()
        assert len(seen) == 1
        assert seen[0].group == "windows"
        assert seen[0].index == 0 and seen[0].total == 3
        assert seen[0].terminal is True

    def test_window_done_advances_index_and_tally(self):
        seen: list[ProgressEvent] = []
        p = _PushProgress(seen.append, windows_total=2)
        p.window_done(5, 100)
        p.window_done(3, 50)
        assert [e.index for e in seen] == [1, 2]
        assert seen[-1].total == 2
        assert seen[-1].tally == {"conversations": 8, "bytes": 150}

    def test_bisecting_flips_total_to_none_for_the_rest(self):
        seen: list[ProgressEvent] = []
        p = _PushProgress(seen.append, windows_total=4)
        p.window_done(2, 20)
        assert seen[-1].total == 4  # determinate before bisection
        p.bisecting()
        assert seen[-1].total is None  # the sweep flip
        p.window_done(1, 10)
        assert seen[-1].total is None  # stays indeterminate after a split
        assert seen[-1].tally == {"conversations": 3, "bytes": 30}

    def test_none_sink_is_a_noop(self):
        p = _PushProgress(None, windows_total=1)
        p.start()
        p.window_done(1, 1)  # must not raise

    def test_faulty_sink_never_propagates(self):
        def boom(_ev):
            raise RuntimeError("render exploded")

        p = _PushProgress(boom, windows_total=1)
        p.start()          # swallows, disables the sink
        p.window_done(1, 1)  # no further calls, no raise

    def test_done_emits_a_terminal_done_matching_the_final_window(self):
        # finding #2: push must emit a resolved (done) frame so the bar deposited
        # into scrollback shows ✓, not a spinner. The fields match the last
        # window_done so the renderer only swaps the glyph.
        seen: list[ProgressEvent] = []
        p = _PushProgress(seen.append, windows_total=2)
        p.window_done(5, 100)
        p.window_done(3, 50)
        p.done()
        assert seen[-1].status == "done"
        assert seen[-1].terminal is True
        assert seen[-1].index == 2  # the last window's index
        assert seen[-1].tally == {"conversations": 8, "bytes": 150}

    def test_done_after_bisection_stays_indeterminate(self):
        seen: list[ProgressEvent] = []
        p = _PushProgress(seen.append, windows_total=3)
        p.bisecting()
        p.done()
        assert seen[-1].status == "done" and seen[-1].total is None


# --- sync_push loop-level emission -----------------------------------------


class TestSyncPushEmitsProgress:
    def test_single_window_emits_start_and_window_done(self, tmp_path, monkeypatch):
        seen: list[ProgressEvent] = []
        monkeypatch.setattr("siftd.api.sync._preflight_http", lambda *a: None)
        monkeypatch.setattr(
            "siftd.api.sync._post_window_with_bisect",
            lambda *a, **kw: (True, 7, 140, None),
        )
        monkeypatch.setattr("siftd.config_sync.get_sync_remote", lambda n: {})
        monkeypatch.setattr("siftd.config_sync.get_sync_timeouts", lambda n, k: (5, 30))
        monkeypatch.setattr("siftd.config_sync.update_last_push", lambda *a, **k: None)
        from siftd.api.auth import AuthError
        monkeypatch.setattr(
            "siftd.api.auth.acquire_token",
            lambda *a: (_ for _ in ()).throw(AuthError("no-auth")),
        )
        _patch_httpx(monkeypatch)

        result = sync_push(_db(tmp_path), _remote(), on_progress=seen.append)
        assert result.conversations == 7
        # start (index 0) → window completing (index 1) → done (the resolved ✓).
        assert seen[0].index == 0
        assert seen[-1].status == "done"  # finding #2: push resolves the bar
        assert seen[-1].index == 1
        assert seen[-1].tally == {"conversations": 7, "bytes": 140}
        assert all(e.total == 1 for e in seen)  # no bisection → determinate


# --- _post_window_with_bisect: 413 → bisecting() ---------------------------


class TestBisectionFlipsTheBar:
    def test_413_calls_progress_bisecting(self, tmp_path, monkeypatch):
        # First POST 413s (forcing a split); the two halves succeed. The window
        # machinery must call progress.bisecting() once, flipping to the sweep.
        # _push_slice_http is mocked directly so we exercise the bisection control
        # flow + the progress wiring without real slice files / httpx.
        seen: list[ProgressEvent] = []
        progress = _PushProgress(seen.append, windows_total=1)

        responses = iter([
            _Resp(status=413),                          # parent window: too big
            _Resp(status=200, body={"status": "ok"}),   # lower half
            _Resp(status=200, body={"status": "ok"}),   # upper half
        ])
        monkeypatch.setattr(
            "siftd.api.sync._push_slice_http",
            lambda client, url, headers, slice_path: next(responses),
        )

        # Two conversations so the parent can split; halves slice to 1 each.
        convs = [
            SimpleNamespace(started_at="2024-01-01T00:00:00Z"),
            SimpleNamespace(started_at="2024-02-01T00:00:00Z"),
        ]
        monkeypatch.setattr(
            "siftd.api.conversations.list_conversations", lambda **kw: iter(convs)
        )

        sizes = iter([
            {"conversations": 2, "size_bytes": 999},  # parent slice
            {"conversations": 1, "size_bytes": 100},  # lower half
            {"conversations": 1, "size_bytes": 100},  # upper half
        ])
        monkeypatch.setattr(
            "siftd.api.slice.slice_database", lambda **kw: next(sizes)
        )
        _patch_httpx(monkeypatch)

        existed, total_convs, total_bytes, _owned = _post_window_with_bisect(
            url="http://srv/api/v1/push", headers={}, client=object(),
            db_path=_db(tmp_path), filters={}, since=None, before=None,
            connect_timeout=5, command_timeout=30, progress=progress,
        )
        assert total_convs == 2 and total_bytes == 200
        # bisecting() fired → at least one event with total=None (the sweep).
        assert any(e.total is None for e in seen)


# --- sync_pull single sweep bracket ----------------------------------------


class TestSyncPullEmitsProgress:
    def test_emits_progress_then_done(self, tmp_path, monkeypatch):
        seen: list[ProgressEvent] = []
        monkeypatch.setattr(
            "siftd.api.sync._pull_http", lambda *a, **k: (4, 200)
        )
        monkeypatch.setattr("siftd.config_sync.update_last_pull", lambda *a, **k: None)
        result = sync_pull(_db(tmp_path), _remote(), on_progress=seen.append)
        assert result.conversations == 4
        assert seen[0].status == "progress" and seen[0].total is None  # always a sweep
        assert seen[-1].status == "done"
        assert seen[-1].tally == {"conversations": 4, "bytes": 200}

    def test_error_emits_error_event_and_reraises(self, tmp_path, monkeypatch):
        seen: list[ProgressEvent] = []
        monkeypatch.setattr(
            "siftd.api.sync._pull_http",
            lambda *a, **k: (_ for _ in ()).throw(SyncError("boom")),
        )
        with pytest.raises(SyncError):
            sync_pull(_db(tmp_path), _remote(), on_progress=seen.append)
        assert seen[-1].status == "error"


# --- httpx mock harness (mirrors test_sync.py) -----------------------------


class _Resp:
    def __init__(self, *, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body or {}
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPStatusError(self)

    def json(self):
        return self._body


class _HTTPStatusError(Exception):
    def __init__(self, response):
        self.response = response


class _ConnectError(Exception):
    pass


class _Client:
    """Context-manager stand-in — push tests mock the window poster, so the
    client's own methods are never reached; it only needs to enter/exit."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Timeout:
    def __init__(self, connect=None, read=None, write=None, pool=None):
        pass


def _patch_httpx(monkeypatch):
    fake = SimpleNamespace(
        Client=lambda **kw: _Client(),
        Timeout=_Timeout,
        HTTPStatusError=_HTTPStatusError,
        ConnectError=_ConnectError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake)
