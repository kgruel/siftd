import asyncio
import builtins
import json
from types import SimpleNamespace

from siftd.serve import routes


def _run(coro):
    return asyncio.run(coro)


def test_index_lists_core_endpoints():
    out = _run(routes.index.fn())
    assert out["service"] == "siftd"
    assert any(e["path"] == "/api/v1/health" for e in out["endpoints"])


def test_dispatch_wrappers_forward_params(monkeypatch, tmp_path):
    seen = []

    def fake_dispatch(path, method, fn, params, render_method, db):
        seen.append((path, method, render_method, params, db))
        return {"ok": True}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    db = tmp_path / "team.db"

    _run(routes.stats_route.fn(db))
    _run(routes.workspaces_route.fn(db, n=7))
    _run(routes.tools_route.fn(db, prefix="x:"))
    _run(routes.tools_by_workspace_route.fn(db, prefix="x:", n=3))
    _run(routes.tags_route.fn(db, since="a", before="b"))
    _run(routes.tool_search_route.fn(db, q="q", n=2))
    _run(routes.export_route.fn(db, n=1))
    _run(routes.conversation_detail.fn(db, id="abc", include_thinking=True, include_tool_content=True, tool_filter="shell"))
    _run(routes.conversation_list.fn(db, n=5, oldest=True))

    assert seen[0][0] == "/api/v1/stats"
    assert any(p == "/api/v1/tool-search" and prm["q"] == "q" for p, _, _, prm, _ in seen)
    assert any(p == "/api/v1/conversations" and prm["id"] == "abc" for p, _, _, prm, _ in seen)


class _Req:
    def __init__(self, body: bytes, headers=None, user=None, client=None):
        self._body = body
        self.headers = headers or {}
        self.user = user
        self.client = client

    async def body(self):
        return self._body


def test_tag_write_route_error_paths(monkeypatch, tmp_path):
    conn = SimpleNamespace(commit=lambda: None, close=lambda: None)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: conn)

    # Missing entity_id/last
    req = _Req(json.dumps({"action": "apply", "tags": ["t"]}).encode())
    out = _run(routes.tag_write_route.fn(req, tmp_path / "db.db"))
    assert out["error"] == "entity_id or last required"

    # No matching entities
    monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda *a, **k: None)
    req2 = _Req(json.dumps({"action": "apply", "tags": ["t"], "entity_id": "x"}).encode())
    out2 = _run(routes.tag_write_route.fn(req2, tmp_path / "db.db"))
    assert out2["error"] == "no matching entities found"


def test_push_and_pull_light_paths(monkeypatch, tmp_path):
    db = tmp_path / "team.db"

    # push invalid tiny body
    tiny = _Req(b"short")
    bad = _run(routes.push.fn(tiny, db, "on_push"))
    assert bad.status_code == 400

    # push success path with mocks
    monkeypatch.setattr("siftd.api.receive.receive_database", lambda *a, **k: {"status": "created", "conversations": 2})
    monkeypatch.setattr(routes, "_get_push_identity", lambda _r: "u")
    monkeypatch.setattr(routes, "_record_push_log", lambda *a, **k: None)
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda **k: (_ for _ in ()).throw(RuntimeError("cache")))
    monkeypatch.setattr("siftd.api.stats.write_stats_cache", lambda _s: None)
    ok_req = _Req(b"x" * 32, client=SimpleNamespace(host="127.0.0.1"))
    ok = _run(routes.push.fn(ok_req, db, "on_push"))
    assert ok.status_code == 201

    # pull empty slice path
    monkeypatch.setattr("siftd.api.slice.slice_database", lambda **k: {"conversations": 0})
    pull = _run(routes.pull.fn(db))
    assert pull.status_code == 200
    assert pull.headers["X-Siftd-Conversations"] == "0"


def test_search_route_importerror_and_dispatch_error(monkeypatch, tmp_path):
    db = tmp_path / "db.db"

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("siftd.api.search"):
            raise ImportError("no embed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r1 = _run(routes.search_route.fn(db, q="hi"))
    assert r1.status_code == 501

    monkeypatch.setattr(builtins, "__import__", real_import)
    monkeypatch.setattr(routes, "_dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r2 = _run(routes.search_route.fn(db, q="hi"))
    assert r2.status_code == 501


def test_health_existing_db_and_pull_nonempty(monkeypatch, tmp_path):
    db = tmp_path / "team.db"
    db.write_bytes(b"x")

    class _Conn:
        def execute(self, _sql):
            return SimpleNamespace(fetchone=lambda: [3])

        def close(self):
            return None

    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *_a, **_k: _Conn())
    h = _run(routes.health.fn(db))
    assert h["conversations"] == 3 and h["db_size_bytes"] > 0

    def fake_slice(**kwargs):
        kwargs["target_path"].write_bytes(b"abc")
        return {"conversations": 2}

    monkeypatch.setattr("siftd.api.slice.slice_database", fake_slice)
    resp = _run(routes.pull.fn(db))
    assert resp.status_code == 200 and resp.headers["X-Siftd-Conversations"] == "2"


def test_tag_write_rename_delete_remove_apply_paths(monkeypatch, tmp_path):
    events = {"commits": 0}

    class _Conn:
        def commit(self):
            events["commits"] += 1

        def close(self):
            events["closed"] = True

    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda _p: _Conn())

    # rename and delete
    called = {}
    monkeypatch.setattr("siftd.api.tags.rename_tag", lambda o, n, conn=None, commit=True: called.setdefault("renamed", (o, n, commit)))
    monkeypatch.setattr("siftd.api.tags.delete_tag", lambda c, t, commit=True: called.setdefault("deleted", (t, commit)))
    assert _run(routes.tag_write_route.fn(_Req(json.dumps({"action": "rename", "old_name": "a", "new_name": "b"}).encode()), tmp_path / "db.db"))["status"] == "renamed"
    assert _run(routes.tag_write_route.fn(_Req(json.dumps({"action": "delete", "tag_name": "x"}).encode()), tmp_path / "db.db"))["status"] == "deleted"

    # remove path with not_found + removed
    monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda *a, **k: "cid")
    monkeypatch.setattr("siftd.api.tags.get_tag_id", lambda _c, t: None if t == "t1" else "tid")
    monkeypatch.setattr("siftd.api.tags.remove_tag", lambda *a, **k: True)
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda **k: (_ for _ in ()).throw(RuntimeError("cache")))
    out_r = _run(routes.tag_write_route.fn(_Req(json.dumps({"action": "remove", "tags": ["t1", "t2"], "entity_id": "cid"}).encode()), tmp_path / "db.db"))
    assert out_r["action"] == "remove" and len(out_r["results"]) == 2

    # apply path via last_n
    monkeypatch.setattr("siftd.api.conversations.get_recent_conversation_ids", lambda _c, _n: ["a", "b"])
    monkeypatch.setattr("siftd.api.tags.get_or_create_tag", lambda _c, _t: "tid")
    monkeypatch.setattr("siftd.api.tags.apply_tag", lambda c, et, eid, tid, commit=False: eid == "a")
    out_a = _run(routes.tag_write_route.fn(_Req(json.dumps({"action": "apply", "tags": ["t"], "last": 2}).encode()), tmp_path / "db.db"))
    assert out_a["results"][0]["count"] == 1 and events["commits"] >= 2


def test_search_success_and_identity_exception_paths(monkeypatch, tmp_path):
    db = tmp_path / "db.db"
    monkeypatch.setattr(routes, "_dispatch", lambda *a, **k: {"ok": True})
    out = _run(routes.search_route.fn(db, q="hi", embeddings_only=False))
    assert out == {"ok": True}

    class _BadUser:
        @property
        def user(self):
            raise RuntimeError("no user")

    req2 = _BadUser()
    req2.headers = {}
    assert routes._get_push_identity(req2) == "anonymous"
    assert routes._get_push_identity(SimpleNamespace(user=SimpleNamespace(sub="anonymous"), headers={})) == "anonymous"
