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


def test_dispatch_threads_caveats_into_envelope(monkeypatch, tmp_path):
    """I02: producer findings reach the serve JSON envelope as `caveats`."""
    from dataclasses import dataclass

    @dataclass
    class _Finding:
        check: str
        severity: str
        message: str

    monkeypatch.setattr(
        "siftd.api.dispatch.Operation",
        lambda **kw: SimpleNamespace(
            params=kw.get("params", {}), render_method=kw.get("render_method"),
            render_context={}, fidelity=None,
        ),
    )
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: {"result": []})
    monkeypatch.setattr("siftd.api.dispatch.render", lambda result, op, fmt: {"result": result})
    monkeypatch.setattr("siftd.api.caveats.ProducerContext", lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        "siftd.api.caveats.run_producers",
        lambda op, result, ctx: [_Finding("stale-embeddings", "warning", "embeddings are stale")],
    )

    out = routes._dispatch(
        "/api/v1/stats", "GET", lambda: None, {"db_path": tmp_path / "db"}, "stats", tmp_path / "db.db",
    )
    assert out["caveats"] == [
        {"check": "stale-embeddings", "severity": "warning", "message": "embeddings are stale"}
    ]


def test_dispatch_no_caveats_leaves_envelope_clean(monkeypatch, tmp_path):
    """No findings → no caveats key (envelopes stay minimal)."""
    monkeypatch.setattr(
        "siftd.api.dispatch.Operation",
        lambda **kw: SimpleNamespace(
            params=kw.get("params", {}), render_method=kw.get("render_method"),
            render_context={}, fidelity=None,
        ),
    )
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: {"result": []})
    monkeypatch.setattr("siftd.api.dispatch.render", lambda result, op, fmt: {"result": result})
    monkeypatch.setattr("siftd.api.caveats.ProducerContext", lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("siftd.api.caveats.run_producers", lambda op, result, ctx: [])

    out = routes._dispatch(
        "/api/v1/stats", "GET", lambda: None, {"db_path": tmp_path / "db"}, "stats", tmp_path / "db.db",
    )
    assert "caveats" not in out


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
    # The detail-template path (not the list path) is what resolves the
    # conversations-detail OpSpec with not_found_on_none=True. The route now
    # passes this template form; this test follows the corrected convention.
    out = routes._dispatch("/api/v1/conversations/{id}", "GET", lambda: None, {"id": "x"}, "detail", tmp_path / "db.db")
    assert out.status_code == 404
    assert out.content["error"] == "conversation not found"


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


def test_event_detail_route_threads_effective_owner(monkeypatch, tmp_path):
    """I03: the event route must scope reads to the authenticated owner."""
    seen = {}

    def _fake_get_event(event_id, **kwargs):
        seen["event_id"] = event_id
        seen["owner"] = kwargs.get("owner", "MISSING")
        return None  # -> 404, fine for this wiring check

    monkeypatch.setattr("siftd.api.events.get_event", _fake_get_event)
    monkeypatch.setattr(routes, "_effective_owner", lambda _req, _o: "alice")

    req = SimpleNamespace(user=SimpleNamespace(sub="alice"))
    out = _run(routes.event_detail_route.fn(req, "01EVT", tmp_path / "db.db", neighbors=False))

    assert seen["owner"] == "alice", "route must pass the effective owner to get_event"
    assert out.status_code == 404  # _fake_get_event returned None


def test_dispatch_ambiguous_prefix_returns_structured_400(monkeypatch, tmp_path):
    """I04: AmbiguousPrefix over HTTP keeps matched_ids/total, not just str(e)."""
    from siftd.api.conversations import AmbiguousPrefix

    monkeypatch.setattr("siftd.api.dispatch.Operation", lambda **kw: None)

    def _raise(*_a, **_kw):
        raise AmbiguousPrefix("01ABC", ["01ABCDEF0001", "01ABCDEF0002"], 2)

    monkeypatch.setattr("siftd.api.dispatch.execute", _raise)
    out = routes._dispatch("/api/v1/conversations/{id}", "GET", lambda: None, {}, "detail", tmp_path / "db.db")
    assert out.status_code == 400
    assert out.content["kind"] == "ambiguous_prefix"
    assert out.content["prefix"] == "01ABC"
    assert out.content["matched_ids"] == ["01ABCDEF0001", "01ABCDEF0002"]
    assert out.content["total"] == 2
    assert "error" in out.content  # human message still present


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


def _make_session_db(tmp_path):
    """Create a real db with active_sessions/pending_tags so the route can write."""
    from siftd.api import create_database
    from siftd.api.sessions import register_session

    db_path = tmp_path / "db.db"
    conn = create_database(db_path)
    register_session(conn, "sess-1", "claude_code", "/p", commit=True)
    conn.close()
    return db_path


def test_session_queue_tag_route_happy_path(monkeypatch, tmp_path):
    """POST /api/v1/sessions/{id}/tags queues a pending tag and returns the result."""
    import json as json_mod

    from siftd.api import open_database
    from siftd.storage.sessions import get_pending_tags

    db_path = _make_session_db(tmp_path)

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({
            "tags": ["review-me"],
            "entity_type": "response",
            "last_marker": "last_response",
        }).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    out = _run(routes.session_queue_tag_route.fn(req, "sess-1", db_path))
    assert out == {"queued": ["review-me"], "duplicate": []}

    # Verify it actually landed in pending_tags
    conn = open_database(db_path, read_only=True)
    try:
        tags = get_pending_tags(conn, "sess-1")
        assert len(tags) == 1
        assert tags[0].tag_name == "review-me"
        assert tags[0].entity_type == "response"
        assert tags[0].last_marker == "last_response"
    finally:
        conn.close()


def test_session_queue_tag_route_duplicate(monkeypatch, tmp_path):
    """Re-queueing the same tag returns it under 'duplicate', not 'queued'."""
    import json as json_mod

    db_path = _make_session_db(tmp_path)

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"tags": ["dup-me"]}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    _run(routes.session_queue_tag_route.fn(req, "sess-1", db_path))
    # Second call with the same body
    req2 = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))
    req2.body = _body
    out = _run(routes.session_queue_tag_route.fn(req2, "sess-1", db_path))
    assert out == {"queued": [], "duplicate": ["dup-me"]}


def test_session_queue_tag_route_missing_db_returns_404(monkeypatch, tmp_path):
    """Non-existent DB returns 404 instead of silently creating one."""
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"tags": ["x"]}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    out = _run(routes.session_queue_tag_route.fn(
        req, "sess-1", tmp_path / "missing.db",
    ))
    assert hasattr(out, "status_code") and out.status_code == 404
    assert not (tmp_path / "missing.db").exists()


def test_session_queue_tag_route_invalid_marker_returns_400(monkeypatch, tmp_path):
    """An invalid last_marker value surfaces as a 400."""
    import json as json_mod

    db_path = _make_session_db(tmp_path)
    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({
            "tags": ["x"],
            "last_marker": "bogus",
        }).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    out = _run(routes.session_queue_tag_route.fn(req, "sess-1", db_path))
    assert hasattr(out, "status_code") and out.status_code == 400
    assert "Unknown last_marker" in out.content["error"]


def test_session_queue_tag_route_empty_tags_returns_400(monkeypatch, tmp_path):
    """Empty tags list returns 400 (and never opens the DB)."""
    import json as json_mod

    req = SimpleNamespace(user=SimpleNamespace(sub="anonymous"))

    async def _body():
        return json_mod.dumps({"tags": []}).encode()
    req.body = _body

    monkeypatch.setattr("siftd.serve.auth.require_write", lambda _r: None)
    out = _run(routes.session_queue_tag_route.fn(
        req, "sess-1", tmp_path / "irrelevant.db",
    ))
    assert hasattr(out, "status_code") and out.status_code == 400


def test_sync_status_redacts_inbox_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "siftd.api.inbox.get_inbox_status",
        lambda _db: {"pending": 0, "total": 1, "last": {"status": "failed", "error": "boom"}},
    )
    out = _run(routes.sync_status_route.fn(tmp_path / "db.db", request_max_body_size=500 * 1024 * 1024))
    assert "error" not in out["inbox"]["last"]
