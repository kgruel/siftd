"""Tests for client-side token acquisition (src/siftd/credentials.py).

Storage safety (permissions, atomicity), the RFC 8628 poll loop, refresh-token
rotation, the never-raise contract of resolve_live_bearer, and the
concurrent-refresh flock race are the load-bearing behaviours here.

HTTP is mocked at the stdlib _post_form layer (credentials.py is stdlib-only;
the non-embed CI lane has no httpx). One test exercises the real http.client
path against a throwaway http.server to prove that an HTTP 400 device-grant
poll response is READ, not raised.
"""

from __future__ import annotations

import base64
import json
import stat
import threading
import time

import pytest

from siftd import credentials
from siftd.credentials import AuthLoginError, Credential


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Isolate XDG state+config and configure a fake issuer with explicit endpoints."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from siftd.config import set_config

    set_config("auth.issuer", "https://idp.test")
    set_config("auth.client_id", "siftd-cli")
    set_config("auth.device_authorization_endpoint", "https://idp.test/device")
    set_config("auth.token_endpoint", "https://idp.test/token")
    return "https://idp.test"


def _jwt_with_exp(exp: float) -> str:
    """Build an unsigned JWT carrying only an exp claim (for fallback tests)."""
    def seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


class _ScriptedPost:
    """A fake _post_form that returns queued (status, body) responses.

    Dispatches by grant_type so device-auth, polling, and refresh can be scripted
    independently and asserted on. Records call count per kind.
    """

    def __init__(self, *, device=None, poll=None, refresh=None):
        self.device = device or (200, {})
        self.poll = list(poll or [])
        self.refresh = list(refresh or [])
        self.calls = {"device": 0, "poll": 0, "refresh": 0}

    def __call__(self, url, fields, *, timeout=30.0):
        grant = fields.get("grant_type", "")
        if "device" in url and not grant:
            self.calls["device"] += 1
            return self.device
        if grant == credentials._DEVICE_GRANT:
            self.calls["poll"] += 1
            return self.poll.pop(0)
        if grant == "refresh_token":
            self.calls["refresh"] += 1
            return self.refresh.pop(0)
        raise AssertionError(f"unexpected post: {url} {fields}")


# --------------------------------------------------------------------------- #
# Storage: permissions + round-trip
# --------------------------------------------------------------------------- #

def test_save_sets_restrictive_permissions(auth_env):
    from siftd.paths import credential_file

    credentials.save(Credential(issuer=auth_env, access_token="tok", expires_at=time.time() + 3600))
    path = credential_file(auth_env)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_load_roundtrip(auth_env):
    cred = Credential(
        issuer=auth_env, access_token="acc", refresh_token="ref",
        expires_at=1234.5, token_type="Bearer", scope="openid offline_access",
    )
    credentials.save(cred)
    assert credentials.load(auth_env) == cred


def test_load_missing_returns_none(auth_env):
    assert credentials.load(auth_env) is None


def test_load_corrupt_returns_none(auth_env):
    from siftd.paths import credential_file

    path = credential_file(auth_env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert credentials.load(auth_env) is None


def test_delete(auth_env):
    credentials.save(Credential(issuer=auth_env, access_token="tok"))
    assert credentials.delete(auth_env) is True
    assert credentials.delete(auth_env) is False


# --------------------------------------------------------------------------- #
# Credential.is_stale
# --------------------------------------------------------------------------- #

def test_is_stale_logic():
    fresh = Credential(issuer="i", access_token="t", expires_at=1000.0)
    assert fresh.is_stale(now=800.0) is False          # 800 + 120 < 1000
    assert fresh.is_stale(now=900.0) is True           # 900 + 120 >= 1000
    # Unknown expiry is treated as NOT stale (reactive path owns it).
    assert Credential(issuer="i", access_token="t").is_stale(now=10_000.0) is False


# --------------------------------------------------------------------------- #
# _post_form against a real server: 400 must be READ, not raised
# --------------------------------------------------------------------------- #

def test_post_form_reads_4xx_without_raising():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            status = 400 if self.path == "/pending" else 200
            payload = (
                {"error": "authorization_pending"} if status == 400
                else {"access_token": "real", "expires_in": 3600}
            )
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        status, body = credentials._post_form(f"http://127.0.0.1:{port}/pending", {"x": "1"})
        assert status == 400
        assert body == {"error": "authorization_pending"}  # read, NOT raised

        status, body = credentials._post_form(f"http://127.0.0.1:{port}/ok", {"x": "1"})
        assert status == 200
        assert body["access_token"] == "real"
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# device_login: poll loop
# --------------------------------------------------------------------------- #

def test_device_login_polls_then_succeeds(auth_env, monkeypatch):
    scripted = _ScriptedPost(
        device=(200, {
            "device_code": "dev", "user_code": "WXYZ",
            "verification_uri": "https://idp.test/activate",
            "interval": 5, "expires_in": 600,
        }),
        poll=[
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, {"access_token": "ACCESS", "refresh_token": "REFRESH", "expires_in": 3600}),
        ],
    )
    monkeypatch.setattr(credentials, "_post_form", scripted)
    intervals: list[int] = []

    cred = credentials.device_login(
        auth_env, on_prompt=lambda *a: None,
        sleep=lambda s: intervals.append(s), now=lambda: 0.0,
    )

    assert cred.access_token == "ACCESS"
    assert cred.refresh_token == "REFRESH"
    assert scripted.calls["poll"] == 3
    # slow_down permanently bumps the interval by 5s.
    assert intervals == [5, 5, 10]
    # Persisted as a side effect.
    assert credentials.load(auth_env).access_token == "ACCESS"


@pytest.mark.parametrize("error,match", [
    ("access_denied", "denied"),
    ("expired_token", "expired"),
])
def test_device_login_terminal_errors(auth_env, monkeypatch, error, match):
    scripted = _ScriptedPost(
        device=(200, {
            "device_code": "dev", "user_code": "WXYZ",
            "verification_uri": "https://idp.test/activate", "interval": 1, "expires_in": 600,
        }),
        poll=[(400, {"error": error})],
    )
    monkeypatch.setattr(credentials, "_post_form", scripted)
    with pytest.raises(AuthLoginError, match=match):
        credentials.device_login(auth_env, on_prompt=lambda *a: None, sleep=lambda s: None, now=lambda: 0.0)


def test_device_login_deadline_expiry(auth_env, monkeypatch):
    clock = {"t": 0.0}
    scripted = _ScriptedPost(
        device=(200, {
            "device_code": "dev", "user_code": "WXYZ",
            "verification_uri": "https://idp.test/activate", "interval": 1, "expires_in": 10,
        }),
        poll=[(400, {"error": "authorization_pending"})] * 100,
    )
    monkeypatch.setattr(credentials, "_post_form", scripted)

    def advancing_sleep(s):
        clock["t"] += 20  # jump past the 10s deadline

    with pytest.raises(AuthLoginError, match="expired"):
        credentials.device_login(
            auth_env, on_prompt=lambda *a: None,
            sleep=advancing_sleep, now=lambda: clock["t"],
        )


def test_jwt_exp_fallback_when_expires_in_omitted(auth_env, monkeypatch):
    exp = time.time() + 1800
    scripted = _ScriptedPost(
        device=(200, {
            "device_code": "dev", "user_code": "WXYZ",
            "verification_uri": "https://idp.test/activate", "interval": 1, "expires_in": 600,
        }),
        poll=[(200, {"access_token": _jwt_with_exp(exp)})],  # no expires_in
    )
    monkeypatch.setattr(credentials, "_post_form", scripted)
    cred = credentials.device_login(auth_env, on_prompt=lambda *a: None, sleep=lambda s: None, now=lambda: 0.0)
    assert cred.expires_at == pytest.approx(exp, abs=1)


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #

def test_refresh_rotates_refresh_token(auth_env, monkeypatch):
    credentials.save(Credential(
        issuer=auth_env, access_token="old", refresh_token="r1", expires_at=time.time() - 1,
    ))
    scripted = _ScriptedPost(refresh=[(200, {
        "access_token": "new", "refresh_token": "r2", "expires_in": 3600,
    })])
    monkeypatch.setattr(credentials, "_post_form", scripted)

    cred = credentials.refresh(auth_env)
    assert cred.access_token == "new"
    assert cred.refresh_token == "r2"  # rotated
    assert credentials.load(auth_env).refresh_token == "r2"


def test_refresh_keeps_prior_refresh_token_when_omitted(auth_env, monkeypatch):
    credentials.save(Credential(
        issuer=auth_env, access_token="old", refresh_token="r1", expires_at=time.time() - 1,
    ))
    scripted = _ScriptedPost(refresh=[(200, {"access_token": "new", "expires_in": 3600})])
    monkeypatch.setattr(credentials, "_post_form", scripted)

    cred = credentials.refresh(auth_env)
    assert cred.refresh_token == "r1"  # not rotated → keep prior


def test_refresh_no_stored_credential_raises(auth_env):
    with pytest.raises(AuthLoginError, match="no stored credential"):
        credentials.refresh(auth_env)


# --------------------------------------------------------------------------- #
# resolve_live_bearer — never raises; proactive refresh
# --------------------------------------------------------------------------- #

def test_resolve_returns_token_when_fresh(auth_env, monkeypatch):
    credentials.save(Credential(issuer=auth_env, access_token="fresh", expires_at=time.time() + 3600))
    # Any network call here would be a bug — fail loudly if refresh is attempted.
    monkeypatch.setattr(credentials, "_post_form", lambda *a, **k: pytest.fail("should not refresh"))
    assert credentials.resolve_live_bearer(auth_env) == "fresh"


def test_resolve_refreshes_when_stale(auth_env, monkeypatch):
    credentials.save(Credential(
        issuer=auth_env, access_token="stale", refresh_token="r1", expires_at=time.time() - 1,
    ))
    scripted = _ScriptedPost(refresh=[(200, {"access_token": "refreshed", "expires_in": 3600})])
    monkeypatch.setattr(credentials, "_post_form", scripted)
    assert credentials.resolve_live_bearer(auth_env) == "refreshed"


def test_resolve_missing_returns_none(auth_env):
    assert credentials.resolve_live_bearer(auth_env) is None


def test_resolve_degrades_to_existing_token_on_refresh_failure(auth_env, monkeypatch):
    # Best-effort: a refresh network failure must NOT raise and must NOT omit a
    # possibly-still-valid token — fall back to the existing access token.
    credentials.save(Credential(
        issuer=auth_env, access_token="stale", refresh_token="r1", expires_at=time.time() - 1,
    ))

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(credentials, "_post_form", boom)
    assert credentials.resolve_live_bearer(auth_env) == "stale"  # degraded, not None, not raised


def test_resolve_stale_without_refresh_token_returns_existing(auth_env, monkeypatch):
    # No refresh token → nothing to refresh with → fall back to the existing
    # (stale-but-maybe-valid) token rather than omitting auth.
    credentials.save(Credential(issuer=auth_env, access_token="stale", expires_at=time.time() - 1))
    monkeypatch.setattr(credentials, "_post_form", lambda *a, **k: pytest.fail("nothing to refresh with"))
    assert credentials.resolve_live_bearer(auth_env) == "stale"


# --------------------------------------------------------------------------- #
# refresh_after_rejection — reactive backstop + rotation race
# --------------------------------------------------------------------------- #

def test_reactive_uses_winner_token_without_refreshing(auth_env, monkeypatch):
    # Stored token differs from the one that got 401 → another process already
    # rotated; use it, do NOT burn our refresh token.
    credentials.save(Credential(issuer=auth_env, access_token="winner", refresh_token="r1",
                                expires_at=time.time() + 3600))
    monkeypatch.setattr(credentials, "_post_form", lambda *a, **k: pytest.fail("should not refresh"))
    assert credentials.refresh_after_rejection(auth_env, rejected_token="loser") == "winner"


def test_reactive_refreshes_when_token_still_rejected(auth_env, monkeypatch):
    credentials.save(Credential(issuer=auth_env, access_token="rejected", refresh_token="r1",
                                expires_at=time.time() + 3600))
    scripted = _ScriptedPost(refresh=[(200, {"access_token": "new", "expires_in": 3600})])
    monkeypatch.setattr(credentials, "_post_form", scripted)
    assert credentials.refresh_after_rejection(auth_env, rejected_token="rejected") == "new"


def test_reactive_never_raises(auth_env, monkeypatch):
    credentials.save(Credential(issuer=auth_env, access_token="rejected", refresh_token="r1",
                                expires_at=time.time() + 3600))
    monkeypatch.setattr(credentials, "_post_form", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert credentials.refresh_after_rejection(auth_env, rejected_token="rejected") is None


# --------------------------------------------------------------------------- #
# Concurrent refresh: flock + in-critical-section recheck => exactly one refresh
# --------------------------------------------------------------------------- #

def test_concurrent_refresh_happens_once(auth_env, monkeypatch):
    credentials.save(Credential(
        issuer=auth_env, access_token="stale", refresh_token="r1", expires_at=time.time() - 1,
    ))
    lock = threading.Lock()
    refresh_count = {"n": 0}

    def slow_refresh(url, fields, *, timeout=30.0):
        assert fields.get("grant_type") == "refresh_token"
        with lock:
            refresh_count["n"] += 1
        time.sleep(0.05)  # widen the race window
        return 200, {"access_token": "refreshed", "expires_in": 3600}

    monkeypatch.setattr(credentials, "_post_form", slow_refresh)

    results: list[str | None] = []

    def worker():
        results.append(credentials.resolve_live_bearer(auth_env))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The lock + recheck means the first thread refreshes; the rest re-read the
    # now-fresh credential under the lock and skip the refresh entirely.
    assert refresh_count["n"] == 1
    assert results == ["refreshed"] * 5
