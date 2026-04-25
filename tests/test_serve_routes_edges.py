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
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: {"result": True})
    monkeypatch.setattr("siftd.api.dispatch.render", lambda result, op, fmt: {"ok": True, "fmt": fmt is not None, "result": result})
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {"a": 1}, "stats", tmp_path / "db.db")
    assert out["ok"] and out["result"] == {"result": True} and seen["path"] == "/api/v1/x" and seen["method"] == "GET"


def test_dispatch_returns_structured_error_on_exception(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)
    def _raise(*_a, **_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("siftd.api.dispatch.execute", _raise)
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {}, "stats", tmp_path / "db.db")
    # Returns a Response, not a raised exception
    assert hasattr(out, "status_code")
    assert out.status_code == 500
    assert "boom" not in (out.content or {}).get("error", "")


def test_dispatch_detail_none_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: None)
    monkeypatch.setattr("siftd.api.dispatch.render", lambda *_a, **_k: {"should": "not run"})
    out = routes._dispatch("/api/v1/conversations", "GET", lambda: None, {"id": "x"}, "detail", tmp_path / "db.db")
    assert out.status_code == 404


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

    monkeypatch.setattr("siftd.api.serve_status.open_database", lambda _p: _Conn())
    monkeypatch.setattr("siftd.api.serve_status.ensure_push_log_table", lambda _c: calls.setdefault("ensure", True))
    routes._record_push_log(tmp_path / "db.db", "anon", 2, 10, SimpleNamespace(client=None), push_id="p1")
    assert calls["commit"] and calls["close"] and calls["params"][0] == "p1" and calls["params"][-1] is None


def test_dispatch_file_not_found_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)
    def _raise(*_a, **_kw):
        raise FileNotFoundError("Database not found: /tmp/missing.db")
    monkeypatch.setattr("siftd.api.dispatch.execute", _raise)
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {}, "stats", tmp_path / "db.db")
    assert out.status_code == 404
    assert "not found" in out.content["error"].lower()


def test_dispatch_value_error_returns_400(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)
    def _raise(*_a, **_kw):
        raise ValueError("invalid date format")
    monkeypatch.setattr("siftd.api.dispatch.execute", _raise)
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {}, "stats", tmp_path / "db.db")
    assert out.status_code == 400
    assert "invalid date format" in out.content["error"]


def test_dispatch_query_error_returns_400(monkeypatch, tmp_path):
    from siftd.api.conversations import QueryError

    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)
    def _raise(*_a, **_kw):
        raise QueryError("Missing template variables: foo")
    monkeypatch.setattr("siftd.api.dispatch.execute", _raise)
    out = routes._dispatch("/api/v1/x", "GET", lambda: None, {}, "stats", tmp_path / "db.db")
    assert out.status_code == 400
    assert "Missing template variables" in out.content["error"]


def test_tag_write_invalid_json_returns_400(monkeypatch, tmp_path):
    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return b"not json"
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert hasattr(out, "status_code") and out.status_code == 400
    assert "invalid JSON" in out.content["error"]


def test_tag_write_rename_missing_fields_returns_400(monkeypatch, tmp_path):
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"action": "rename"}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: SimpleNamespace(close=lambda: None))
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert hasattr(out, "status_code") and out.status_code == 400
    assert "old_name" in out.content["error"]


def test_tag_write_delete_missing_tag_name_returns_400(monkeypatch, tmp_path):
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"action": "delete"}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: SimpleNamespace(close=lambda: None))
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert hasattr(out, "status_code") and out.status_code == 400
    assert "tag_name" in out.content["error"]


def test_tag_write_missing_entity_returns_400(monkeypatch, tmp_path):
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"action": "apply", "tags": ["foo"]}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: SimpleNamespace(close=lambda: None))
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert hasattr(out, "status_code") and out.status_code == 400
    assert "entity_id or last" in out.content["error"]


def test_tag_write_invalid_last_returns_400(monkeypatch, tmp_path):
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"action": "apply", "tags": ["foo"], "last": "abc"}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: SimpleNamespace(close=lambda: None))
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert hasattr(out, "status_code") and out.status_code == 400
    assert "integer" in out.content["error"]


def test_tag_write_no_matching_entities_returns_404(monkeypatch, tmp_path):
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"action": "apply", "tags": ["foo"], "entity_id": "missing123"}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda *a, **kw: None)
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert hasattr(out, "status_code") and out.status_code == 404


def test_sync_status_redacts_inbox_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "siftd.api.inbox.get_inbox_status",
        lambda _db: {"pending": 0, "total": 1, "last": {"status": "failed", "error": "boom"}},
    )
    out = _run(routes.sync_status_route.fn(tmp_path / "db.db"))
    assert "error" not in out["inbox"]["last"]
