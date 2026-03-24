import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import routes


def _run(coro):
    return asyncio.run(coro)


def test_dispatch_builds_operation_and_calls_dispatch(monkeypatch, tmp_path):
    seen = {}

    class _Op:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("siftd.api.dispatch.Operation", _Op)
    monkeypatch.setattr("siftd.api.dispatch.dispatch", lambda op, fmt: {"ok": True, "fmt": fmt is not None})
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {"a": 1}, "stats", tmp_path / "db.db")
    assert out["ok"] and seen["path"] == "/api/v1/x" and seen["method"] == "GET"


def test_dispatch_returns_structured_error_on_exception(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)
    def _raise(*_a, **_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("siftd.api.dispatch.dispatch", _raise)
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {}, "stats", tmp_path / "db.db")
    # Returns a Response, not a raised exception
    assert hasattr(out, "status_code")
    assert out.status_code == 500


def test_health_nonexistent_db_returns_zero_counts(tmp_path):
    out = _run(routes.health.fn(tmp_path / "missing.db"))
    assert out["status"] == "ok" and out["db_size_bytes"] == 0 and out["conversations"] == 0


def test_get_push_identity_prefers_user_then_header():
    req1 = SimpleNamespace(user=SimpleNamespace(sub="u1"), headers={})
    assert routes._get_push_identity(req1) == "u1"
    req2 = SimpleNamespace(user=SimpleNamespace(sub="anonymous"), headers={"x-siftd-identity": "h1"})
    assert routes._get_push_identity(req2) == "h1"


def test_record_push_log_handles_missing_client(monkeypatch, tmp_path):
    calls = {}

    class _Conn:
        def execute(self, sql, params):
            calls["sql"] = sql
            calls["params"] = params

        def commit(self):
            calls["commit"] = True

        def close(self):
            calls["close"] = True

    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: _Conn())
    monkeypatch.setattr("siftd.storage.sqlite.ensure_push_log_table", lambda _c: calls.setdefault("ensure", True))
    routes._record_push_log(tmp_path / "db.db", "anon", 2, 10, SimpleNamespace(client=None), push_id="p1")
    assert calls["commit"] and calls["close"] and calls["params"][0] == "p1" and calls["params"][-1] is None
