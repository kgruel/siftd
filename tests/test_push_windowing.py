"""Tests for push windowing (feat/push-windowing).

Covers:
- _derive_date_windows: correct split / single-window / empty / boundary-dup filter
- sync_push HTTP path: windowed split with low injected cap
- sync_push HTTP path: steady-state single POST when estimate fits
- sync_push HTTP path: 413 bisection triggers on 413 only (not 401/403/422/500)
- sync_push HTTP path: 413 bisection on [A,A,A,B] advances past duplicate timestamps
- sync_push HTTP path: 413 all-same-timestamp raises SyncError
- sync_push HTTP path: resume after partial seed — cursor advance + rerun from cursor
- sync_push HTTP path: cap discovery from sync/status
- sync_status_route: max_body_size field present in response (e2e smoke)
- CLI cmd_db_push: multi-window print + resume hint
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")

# Import real httpx exceptions before any patching so tests can use them.
import httpx as _real_httpx

from conftest import make_db

from siftd.api.sync import (
    SyncError,
    _derive_date_windows,
)
from siftd.domain.sync import PushResult, SyncRemote, SyncStatus


# ---------------------------------------------------------------------------
# Pure function unit tests (no I/O)
# ---------------------------------------------------------------------------


class TestDeriveDateWindows:
    def _make_convs(self, timestamps):
        return [SimpleNamespace(started_at=ts) for ts in timestamps]

    def test_empty_returns_empty(self):
        assert _derive_date_windows([], 1_000_000, 1000.0) == []

    def test_single_window_when_estimate_fits(self):
        convs = self._make_convs([
            "2024-01-01T00:00:00",
            "2024-01-02T00:00:00",
            "2024-01-03T00:00:00",
        ])
        # 1MB cap, 100 bytes/conv → chunk_size = 10000 ≥ 3 convs → single window
        windows = _derive_date_windows(convs, 1_000_000, 100.0)
        assert windows == [(None, None)]

    def test_splits_into_two_windows(self):
        convs = self._make_convs([
            "2024-01-01T00:00:00",
            "2024-01-02T00:00:00",
            "2024-01-03T00:00:00",
            "2024-01-04T00:00:00",
        ])
        # chunk_size = int(2 / 1.0) = 2 → 2 windows of 2 convs each
        windows = _derive_date_windows(convs, 2, 1.0)
        assert len(windows) == 2
        assert windows[0] == (None, "2024-01-03T00:00:00")
        assert windows[1] == ("2024-01-03T00:00:00", None)

    def test_first_window_since_is_none(self):
        convs = self._make_convs(["2024-01-01T00:00:00", "2024-01-02T00:00:00"])
        windows = _derive_date_windows(convs, 1, 1.0)
        assert windows[0][0] is None

    def test_last_window_before_is_none(self):
        convs = self._make_convs([
            "2024-01-01T00:00:00",
            "2024-01-02T00:00:00",
            "2024-01-03T00:00:00",
        ])
        windows = _derive_date_windows(convs, 1, 1.0)
        assert windows[-1][1] is None

    def test_boundary_duplicate_timestamps_skips_empty_windows(self):
        """[A,A,A,B] with chunk_size=2: boundary falls on A, first window (None,A) is empty.

        The filter must drop it so reported window count is accurate.
        """
        convs = self._make_convs([
            "2024-01-01T00:00:00",  # A
            "2024-01-01T00:00:00",  # A
            "2024-01-01T00:00:00",  # A
            "2024-01-02T00:00:00",  # B
        ])
        # chunk_size = int(2 / 1.0) = 2
        # Naively: [(None, A), (A, None)] but (None, A) is empty → filtered out
        windows = _derive_date_windows(convs, 2, 1.0)
        assert len(windows) == 1
        assert windows[0] == ("2024-01-01T00:00:00", None)


# ---------------------------------------------------------------------------
# HTTP push integration tests
# ---------------------------------------------------------------------------


class _PostResp:
    """Fake httpx response for push POST calls."""

    def __init__(self, *, status=200, body=None):
        self.status_code = status
        self._body = body or {"status": "ok"}

    def raise_for_status(self):
        if self.status_code >= 400:
            # Use real httpx exception so _post_window_with_bisect's
            # `except httpx.HTTPStatusError` catches it correctly.
            req = _real_httpx.Request("POST", "http://srv/api/v1/push")
            resp = _real_httpx.Response(self.status_code, request=req)
            raise _real_httpx.HTTPStatusError("err", request=req, response=resp)

    def json(self):
        return self._body


class _TrackingClient:
    """Fake httpx.Client that records every post() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._call_idx = 0
        self.post_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, *, content=None, headers=None):
        body = b"".join(content) if hasattr(content, "__iter__") else (content or b"")
        self.post_calls.append({"url": url, "headers": headers, "size": len(body)})
        resp = self._responses[min(self._call_idx, len(self._responses) - 1)]
        self._call_idx += 1
        return resp


def _make_push_db(tmp_path, *, n_convs=3):
    """DB with n_convs at distinct timestamps one day apart."""
    conversations = [
        {
            "external_id": f"c-{i}",
            "started_at": f"2024-01-{i+1:02d}T10:00:00Z",
        }
        for i in range(n_convs)
    ]
    return make_db(tmp_path / "push.db", conversations=conversations)


def _make_same_ts_push_db(tmp_path, *, n_convs=2):
    """DB with n_convs all sharing the same timestamp."""
    conversations = [
        {"external_id": f"c-{i}", "started_at": "2024-01-15T10:00:00Z"}
        for i in range(n_convs)
    ]
    return make_db(tmp_path / "same_ts.db", conversations=conversations)


def _remote(path="http://srv", name="t"):
    return SyncRemote(name=name, path=path, host=None)


def _patch_push_deps(monkeypatch, *, max_body_size, responses, update_fn=None):
    """Patch external deps for _sync_push_http.

    Returns the _TrackingClient so callers can inspect post_calls.
    update_fn: optional callable to patch update_last_push with.
    """
    from siftd.domain.sync import SYNC_CAPABILITIES

    status = SyncStatus(
        capabilities=SYNC_CAPABILITIES,
        max_body_size=max_body_size,
    )
    monkeypatch.setattr("siftd.api.sync._preflight_http", lambda remote: status)
    monkeypatch.setattr("siftd.config_sync.get_sync_remote", lambda n: {})
    monkeypatch.setattr("siftd.config_sync.get_sync_timeouts", lambda n, k: (5, 30))
    # No client credential in these windowing tests: isolate from any real
    # [auth] device-code credential on the host (the resolver would otherwise
    # pick it up and tag it "device-code", firing the gated 401-retry).
    monkeypatch.setattr("siftd.api.auth.resolve_sync_bearer", lambda auth: (None, None))
    monkeypatch.setattr(
        "siftd.config_sync.update_last_push",
        update_fn if update_fn is not None else (lambda *a, **kw: None),
    )

    client = _TrackingClient(responses)
    fake_httpx = SimpleNamespace(
        Client=lambda **kw: client,
        Timeout=lambda **kw: None,
        # Keep real exception classes so except clauses in src still work
        HTTPStatusError=_real_httpx.HTTPStatusError,
        ConnectError=_real_httpx.ConnectError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    return client


class TestSyncPushHttpWindowing:
    """sync_push HTTP path: windowed vs single-POST behavior."""

    def test_push_attaches_auth_bearer_from_auth_namespace(self, tmp_path, monkeypatch):
        """Regression: device-code push must attach the [auth] credential.

        The push path used to resolve only the per-remote
        ``[sync.remotes.<name>.auth]`` block; with none configured it sent NO
        Authorization header (acquire_token(None) → AuthError → swallowed) and
        the server returned 401. The resolver is now shared with the read path,
        so an ``[auth]`` device-code credential is attached even without a
        per-remote auth block. Fails without the shared-resolver fix.
        """
        from siftd.domain.sync import SYNC_CAPABILITIES

        db = _make_push_db(tmp_path, n_convs=2)

        status = SyncStatus(capabilities=SYNC_CAPABILITIES, max_body_size=500_000_000)
        monkeypatch.setattr("siftd.api.sync._preflight_http", lambda remote: status)
        monkeypatch.setattr("siftd.config_sync.get_sync_remote", lambda n: {})  # no per-remote auth
        monkeypatch.setattr("siftd.config_sync.get_sync_timeouts", lambda n, k: (5, 30))
        monkeypatch.setattr("siftd.config_sync.update_last_push", lambda *a, **kw: None)
        # The [auth] device-code credential is the only token source.
        monkeypatch.delenv("SIFTD_SERVE_TOKEN", raising=False)
        monkeypatch.delenv("SIFTD_SERVE_DELEGATION_TOKEN", raising=False)
        monkeypatch.setattr("siftd.api.auth.configured_issuer", lambda: "https://idp.example/")
        monkeypatch.setattr("siftd.credentials.resolve_live_bearer", lambda issuer: "DEVTOKEN")

        client = _TrackingClient([_PostResp(body={"status": "created"})])
        fake_httpx = SimpleNamespace(
            Client=lambda **kw: client,
            Timeout=lambda **kw: None,
            HTTPStatusError=_real_httpx.HTTPStatusError,
            ConnectError=_real_httpx.ConnectError,
        )
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        from siftd.api.sync import sync_push
        sync_push(db_path=db, remote=_remote())

        assert len(client.post_calls) == 1
        assert client.post_calls[0]["headers"].get("Authorization") == "Bearer DEVTOKEN"

    def test_steady_state_single_post(self, tmp_path, monkeypatch):
        """When full DB fits within the advertised cap, exactly 1 POST is made."""
        db = _make_push_db(tmp_path, n_convs=3)
        # Large cap: tiny test DB always fits in one window
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[_PostResp(body={"status": "ok"})])

        from siftd.api.sync import sync_push
        result = sync_push(db_path=db, remote=_remote())

        assert result.conversations == 3
        assert result.windows == 1
        assert len(client.post_calls) == 1

    def test_windowed_split_multiple_posts(self, tmp_path, monkeypatch):
        """With a tiny max_body_size, the push is split into multiple POSTs."""
        db = _make_push_db(tmp_path, n_convs=4)
        # Tiny cap forces windowing: chunk_size = max(1, int(1/bytes_per_conv)) = 1
        client = _patch_push_deps(monkeypatch, max_body_size=1,
                                   responses=[_PostResp(body={"status": "ok"})] * 10)

        from siftd.api.sync import sync_push
        result = sync_push(db_path=db, remote=_remote())

        assert result.conversations == 4
        assert result.windows > 1, "Expected multiple windows for tiny cap"
        assert len(client.post_calls) == result.windows

    def test_413_bisects_window(self, tmp_path, monkeypatch):
        """413 on a multi-conv window triggers bisection into sub-windows."""
        db = _make_push_db(tmp_path, n_convs=2)

        # Large cap so _sync_push_http won't pre-split; bisection is triggered by 413.
        # First call 413s (full window), then both halves succeed.
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[
                                       _PostResp(status=413),
                                       _PostResp(body={"status": "ok"}),
                                       _PostResp(body={"status": "ok"}),
                                   ])

        from siftd.api.sync import sync_push
        # Should NOT raise — bisection handles the 413
        result = sync_push(db_path=db, remote=_remote())
        assert result.conversations == 2
        # 1 initial 413 + 2 bisected POSTs
        assert len(client.post_calls) == 3

    def test_413_same_timestamp_raises_sync_error(self, tmp_path, monkeypatch):
        """413 on a window where all convs share a timestamp raises SyncError immediately."""
        db = _make_same_ts_push_db(tmp_path, n_convs=2)

        # Large cap so no pre-splitting; server always 413s to trigger bisection.
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[_PostResp(status=413)] * 10)

        from siftd.api.sync import sync_push
        with pytest.raises(SyncError, match="all conversations share timestamp"):
            sync_push(db_path=db, remote=_remote())

    def test_non_413_raises_sync_error_no_bisect(self, tmp_path, monkeypatch):
        """A 500 response must NOT trigger bisection — raises SyncError immediately."""
        db = _make_push_db(tmp_path, n_convs=2)
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[_PostResp(status=500)])

        from siftd.api.sync import sync_push
        with pytest.raises(SyncError, match="Push failed: HTTP 500"):
            sync_push(db_path=db, remote=_remote())
        # Only 1 POST — no bisection
        assert len(client.post_calls) == 1

    def test_401_raises_sync_error_no_bisect(self, tmp_path, monkeypatch):
        """A 401 response must NOT trigger bisection."""
        db = _make_push_db(tmp_path, n_convs=2)
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[_PostResp(status=401)])

        from siftd.api.sync import sync_push
        with pytest.raises(SyncError, match="Push failed: HTTP 401"):
            sync_push(db_path=db, remote=_remote())
        assert len(client.post_calls) == 1

    def test_403_raises_sync_error_no_bisect(self, tmp_path, monkeypatch):
        """A 403 response must NOT trigger bisection."""
        db = _make_push_db(tmp_path, n_convs=2)
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[_PostResp(status=403)])

        from siftd.api.sync import sync_push
        with pytest.raises(SyncError, match="Push failed: HTTP 403"):
            sync_push(db_path=db, remote=_remote())
        assert len(client.post_calls) == 1

    def test_422_raises_sync_error_no_bisect(self, tmp_path, monkeypatch):
        """A 422 response must NOT trigger bisection."""
        db = _make_push_db(tmp_path, n_convs=2)
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[_PostResp(status=422)])

        from siftd.api.sync import sync_push
        with pytest.raises(SyncError, match="Push failed: HTTP 422"):
            sync_push(db_path=db, remote=_remote())
        assert len(client.post_calls) == 1

    def test_413_straddling_timestamps_bisects_successfully(self, tmp_path, monkeypatch):
        """[A,A,A,B]: bisection advances past repeated A-timestamps to split at B."""
        # 3 convs at same timestamp (A) + 1 at a later timestamp (B)
        db = make_db(tmp_path / "aaab.db", conversations=[
            {"external_id": "c0", "started_at": "2024-01-01T10:00:00Z"},
            {"external_id": "c1", "started_at": "2024-01-01T10:00:00Z"},
            {"external_id": "c2", "started_at": "2024-01-01T10:00:00Z"},
            {"external_id": "c3", "started_at": "2024-01-02T10:00:00Z"},
        ])
        # Large cap → single initial window (None, None); 413 triggers bisection.
        # Bisection: mid_idx=2, mid_ts=A, advanced to B.
        # Lower [None, B): 3 A-convs → ok
        # Upper [B, None): 1 B-conv → ok
        client = _patch_push_deps(monkeypatch, max_body_size=500_000_000,
                                   responses=[
                                       _PostResp(status=413),
                                       _PostResp(body={"status": "ok"}),
                                       _PostResp(body={"status": "ok"}),
                                   ])

        from siftd.api.sync import sync_push
        result = sync_push(db_path=db, remote=_remote())

        assert result.conversations == 4
        assert len(client.post_calls) == 3  # 1 failed + 2 bisected

    def test_resume_after_partial_seed(self, tmp_path, monkeypatch):
        """A connection error mid-seed leaves the cursor at the last completed window;
        a rerun picks up from the cursor without re-pushing confirmed windows."""
        from siftd.domain.sync import SYNC_CAPABILITIES

        db = _make_push_db(tmp_path, n_convs=4)  # 4 convs at day-apart timestamps

        # Common patches that persist across both runs
        monkeypatch.setattr("siftd.config_sync.get_sync_remote", lambda n: {})
        monkeypatch.setattr("siftd.config_sync.get_sync_timeouts", lambda n, k: (5, 30))
        monkeypatch.setattr("siftd.api.auth.resolve_sync_bearer", lambda auth: (None, None))

        class _PartialClient:
            """Succeeds for `succeed` POSTs then raises ConnectError."""
            def __init__(self, succeed):
                self.calls = 0
                self.succeed = succeed
                self.post_calls: list[dict] = []
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def post(self, url, *, content=None, headers=None):
                body = b"".join(content) if hasattr(content, "__iter__") else b""
                self.calls += 1
                self.post_calls.append({"url": url, "size": len(body)})
                if self.calls <= self.succeed:
                    return _PostResp(body={"status": "ok"})
                raise _real_httpx.ConnectError("simulated failure")

        # --- Run 1: tiny cap → 4 windows; window 0 ok, window 1 raises ConnectError ---
        cursor_state: dict = {}

        def _track_cursor(name, ts, *, filter_signature=""):
            cursor_state["ts"] = ts

        client1 = _PartialClient(succeed=1)
        monkeypatch.setattr(
            "siftd.api.sync._preflight_http",
            lambda remote: SyncStatus(capabilities=SYNC_CAPABILITIES, max_body_size=1),
        )
        monkeypatch.setattr("siftd.config_sync.update_last_push", _track_cursor)
        monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(
            Client=lambda **kw: client1,
            Timeout=lambda **kw: None,
            HTTPStatusError=_real_httpx.HTTPStatusError,
            ConnectError=_real_httpx.ConnectError,
        ))

        from siftd.api.sync import sync_push
        with pytest.raises(SyncError, match="Cannot connect"):
            sync_push(db_path=db, remote=_remote())

        assert "ts" in cursor_state, "Cursor must advance after window 0 succeeded"
        cursor_ts = cursor_state["ts"]
        assert len(client1.post_calls) == 2  # window 0 ok + window 1 fail

        # --- Run 2: large cap, resume from cursor → effective_since != None → 1 POST ---
        client2 = _PartialClient(succeed=10)
        monkeypatch.setattr(
            "siftd.api.sync._preflight_http",
            lambda remote: SyncStatus(capabilities=SYNC_CAPABILITIES, max_body_size=500_000_000),
        )
        monkeypatch.setattr("siftd.config_sync.update_last_push", lambda *a, **kw: None)
        monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(
            Client=lambda **kw: client2,
            Timeout=lambda **kw: None,
            HTTPStatusError=_real_httpx.HTTPStatusError,
            ConnectError=_real_httpx.ConnectError,
        ))

        remote_with_cursor = SyncRemote(
            name="t", path="http://srv", host=None,
            last_push=cursor_ts, last_push_filters="",
        )
        result2 = sync_push(db_path=db, remote=remote_with_cursor)

        assert len(client2.post_calls) == 1, "Rerun must send remaining convs in one POST"
        # Convs 1, 2, 3 (conv 0 was confirmed in run 1 — no re-push)
        assert result2.conversations == 3

    def test_no_advertised_cap_single_post(self, tmp_path, monkeypatch):
        """When preflight returns None for max_body_size, no windowing, single POST."""
        db = _make_push_db(tmp_path, n_convs=3)
        client = _patch_push_deps(monkeypatch, max_body_size=None,
                                   responses=[_PostResp(body={"status": "ok"})])

        from siftd.api.sync import sync_push
        result = sync_push(db_path=db, remote=_remote())
        assert result.windows == 1
        assert len(client.post_calls) == 1

    def test_cursor_advances_per_window(self, tmp_path, monkeypatch):
        """update_last_push is called once per successfully delivered window."""
        db = _make_push_db(tmp_path, n_convs=4)
        update_calls: list[dict] = []

        def _fake_update(name, ts, *, filter_signature=""):
            update_calls.append({"name": name, "ts": ts})

        # Tiny cap → multiple windows; each window calls update_last_push
        client = _patch_push_deps(monkeypatch, max_body_size=1,
                                   responses=[_PostResp(body={"status": "ok"})] * 10,
                                   update_fn=_fake_update)

        from siftd.api.sync import sync_push
        result = sync_push(db_path=db, remote=_remote())

        assert result.windows > 1
        assert len(update_calls) == result.windows

    def test_incremental_push_single_post(self, tmp_path, monkeypatch):
        """Incremental push (effective_since != None) always uses single window."""
        db = _make_push_db(tmp_path, n_convs=3)
        # Tiny cap but incremental push should NOT trigger windowing
        client = _patch_push_deps(monkeypatch, max_body_size=1,
                                   responses=[_PostResp(body={"status": "ok"})])

        from siftd.api.sync import sync_push
        # since= overrides to incremental; windowing only gates on effective_since is None
        result = sync_push(db_path=db, remote=_remote(), since="2024-01-01T00:00:00Z")

        assert result.windows == 1
        assert len(client.post_calls) == 1


# ---------------------------------------------------------------------------
# E2e smoke: sync/status exposes max_body_size
# ---------------------------------------------------------------------------


@pytest.mark.serve
def test_sync_status_exposes_max_body_size(tmp_path):
    """GET /api/v1/sync/status returns max_body_size matching request_max_body_size."""
    pytest.importorskip("litestar")
    from litestar.testing import TestClient

    from siftd.serve.app import create_app

    db = tmp_path / "t.db"
    cap = 123_456_789
    app = create_app(db_path=db, auth_config=None, request_max_body_size=cap)

    with TestClient(app) as client:
        resp = client.get("/api/v1/sync/status")

    assert resp.status_code == 200
    data = resp.json()
    assert "max_body_size" in data
    assert data["max_body_size"] == cap


# ---------------------------------------------------------------------------
# CLI argparse layer: window count + resume hint
# ---------------------------------------------------------------------------


def _build_args(name="t", dry_run=False):
    """Build a minimal args namespace that cmd_db_push expects."""
    return SimpleNamespace(
        name=name,
        dry_run=dry_run,
        since=None,
        push_all=False,
        workspace=None,
        tag=None,
        no_tag=None,
        owner=None,
        strategy=None,
        db=None,
    )


def _run_cmd_push(args, push_result, db_path):
    """Call cmd_db_push with patched dependencies, return (rc, stdout, stderr)."""
    from unittest.mock import patch

    from siftd.cli.db import cmd_db_push

    with patch("siftd.api.sync.sync_push", return_value=push_result), \
         patch("siftd.config_sync.get_sync_remote", return_value={
             "name": args.name, "host": None, "path": "/tmp/t.db",
             "strategy": "incremental", "filters": None,
             "last_push": None, "last_pull": None, "last_sent": None,
             "last_push_filters": "", "last_pull_filters": "", "last_sent_filters": "",
         }):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_db_push(args)
        return rc, out.getvalue(), err.getvalue()


class TestCmdDbPushOutput:
    """CLI output for windowed push results."""

    def test_single_window_no_count_shown(self, tmp_path):
        db = _make_push_db(tmp_path, n_convs=2)
        result = PushResult(
            conversations=2, size_bytes=1024, remote_name="t",
            remote_existed=True, dry_run=False, last_push_updated=True, windows=1,
        )
        args = _build_args()
        # resolve_db uses args.db; patch it to return the real file
        args.db = str(db)
        rc, out, err = _run_cmd_push(args, result, db)
        assert rc == 0
        assert "windows" not in out.lower()

    def test_multi_window_shows_count(self, tmp_path):
        db = _make_push_db(tmp_path, n_convs=2)
        result = PushResult(
            conversations=100, size_bytes=10 * 1024 * 1024, remote_name="t",
            remote_existed=True, dry_run=False, last_push_updated=True, windows=5,
        )
        args = _build_args()
        args.db = str(db)
        rc, out, err = _run_cmd_push(args, result, db)
        assert rc == 0
        assert "(5 windows)" in out

    def test_partial_failure_shows_resume_hint(self, tmp_path):
        db = _make_push_db(tmp_path, n_convs=2)
        result = PushResult(
            conversations=50, size_bytes=5 * 1024 * 1024, remote_name="t",
            remote_existed=True, dry_run=False, last_push_updated=False, windows=3,
        )
        args = _build_args()
        args.db = str(db)
        rc, out, err = _run_cmd_push(args, result, db)
        assert rc == 0
        assert "Re-run" in err

    def test_dry_run_shows_window_count(self, tmp_path):
        db = _make_push_db(tmp_path, n_convs=2)
        result = PushResult(
            conversations=200, size_bytes=20 * 1024 * 1024, remote_name="t",
            remote_existed=True, dry_run=True, last_push_updated=False, windows=4,
        )
        args = _build_args(dry_run=True)
        args.db = str(db)
        rc, out, err = _run_cmd_push(args, result, db)
        assert rc == 0
        assert "4 windows" in out
