import asyncio
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import html_routes as hr


def _run(coro):
    return asyncio.run(coro)


class _Fmt:
    def render_detail(self, *_a, **_k):
        return "<detail/>"

    def render_list(self, *_a, **_k):
        return "<list/>"

    def render_search(self, *_a, **_k):
        return "<search/>"

    def render_stats(self, *_a, **_k):
        return "<stats/>"


def test_ui_query_list_path(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())
    monkeypatch.setattr("siftd.api.dispatch.dispatch", lambda _op, fmt: "<list/>")
    out = _run(hr.ui_query.fn(SimpleNamespace(), tmp_path / "db.db", workspace="", since=None, before=None, model="", tag=[""], search="", owner=None, n=5))
    assert "<list/>" in out.content


def test_ui_search_modes_and_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())
    db = tmp_path / "db.db"

    empty = _run(hr.ui_search.fn(SimpleNamespace(), db, q="   "))
    assert "Type to search" in empty.content

    fake_search = SimpleNamespace(
        aggregate_by_conversation=lambda _r, limit=20: [
            SimpleNamespace(conversation_id="c1", max_score=1.0, mean_score=1.0, chunk_count=1, workspace_path="/w", started_at="t")
        ],
        search_chunks=lambda **_k: [],
    )
    monkeypatch.setitem(sys.modules, "siftd.api.search", fake_search)
    monkeypatch.setattr(
        "siftd.api.dispatch.execute",
        lambda _op: [SimpleNamespace(conversation_id="c1", score=1.0, text="x", chunk_type="text", workspace_path="/w", started_at="t")],
    )
    sem = _run(hr.ui_search.fn(SimpleNamespace(), db, q="foo", mode="conversations"))
    assert "<search/>" in sem.content

    monkeypatch.setattr(sys.modules["siftd.api.search"], "search_chunks", lambda **_k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: [{"id": "c1"}])
    fb = _run(hr.ui_search.fn(SimpleNamespace(), db, q="foo"))
    assert "<list/>" in fb.content


def test_ui_peek_and_follow_branches(monkeypatch):
    monkeypatch.setattr("siftd.api.peek.list_active_sessions", lambda **_k: [])
    assert "No active sessions" in _run(hr.ui_peek.fn()).content

    monkeypatch.setattr(
        "siftd.api.peek.list_active_sessions",
        lambda **_k: [SimpleNamespace(session_id="s12345678", workspace_name="ws", branch="b", model="m", exchange_count=2, adapter_name="a")],
    )
    assert "conversation-list" in _run(hr.ui_peek.fn()).content

    assert "No session ID" in _run(hr.ui_follow.fn(sid="")).content

    from siftd.api.peek import AmbiguousSessionError

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: (_ for _ in ()).throw(AmbiguousSessionError("x", [])))
    assert "Ambiguous" in _run(hr.ui_follow.fn(sid="x")).content

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: None)
    assert "Session not found" in _run(hr.ui_follow.fn(sid="x")).content

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: "/tmp/f")
    monkeypatch.setattr("siftd.api.peek.read_session_detail", lambda *a, **k: None)
    assert "Cannot read session" in _run(hr.ui_follow.fn(sid="x")).content


def test_ui_stats_tools_tag_suggest_and_export(monkeypatch, tmp_path):
    db = tmp_path / "db.db"
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: (_ for _ in ()).throw(RuntimeError("x")))
    assert "No data available" in _run(hr.ui_stats.fn(SimpleNamespace(), db)).content

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: {"ok": True})
    monkeypatch.setattr("siftd.api.stats.get_usage_summary", lambda **_k: None)
    monkeypatch.setattr("siftd.api.stats.get_cost_coverage", lambda **_k: None)
    monkeypatch.setattr("siftd.api.stats.get_usage_by_model", lambda **_k: [])
    monkeypatch.setattr("siftd.api.stats.get_usage_by_workspace", lambda **_k: [])
    assert "<stats/>" in _run(hr.ui_stats.fn(SimpleNamespace(), db)).content

    monkeypatch.setattr("siftd.api.tags.list_tags", lambda **_k: [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")])
    assert "alpha" in _run(hr.ui_tags_suggest.fn(SimpleNamespace(), db, tag="a")).content
    assert "No conversation ID" in _run(hr.ui_export.fn(SimpleNamespace(), db, id="", format="md")).content

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: SimpleNamespace(count=0))
    assert "Not found" in _run(hr.ui_export.fn(SimpleNamespace(), db, id="abc", format="md")).content

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: SimpleNamespace(count=1, content="x", media_type="text/plain", filename="x.txt"))
    ex = _run(hr.ui_export.fn(SimpleNamespace(), db, id="abc", format="md"))
    assert "attachment" in ex.headers.get("Content-Disposition", "") and ex.media_type == "text/plain"


def test_ui_tag_missing_fields(monkeypatch, tmp_path):
    class _Req:
        async def form(self):
            return {"action": "apply", "id": "", "tag": ""}

    out = _run(hr.ui_tag.fn(_Req(), tmp_path / "db.db"))
    assert "missing id or tag" in out.content


def test_ui_meta_populates_non_empty_options(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda **k: SimpleNamespace(models=["m1"]))
    monkeypatch.setattr("siftd.api.stats.list_workspaces", lambda **k: [{"path": "/w1"}])
    monkeypatch.setattr("siftd.api.tags.list_tags", lambda **k: [SimpleNamespace(name="tag1")])
    out = _run(hr.ui_meta.fn(SimpleNamespace(), tmp_path / "db.db", None))
    assert "/w1" in out.content and "tag1" in out.content and "m1" in out.content


def test_ui_search_no_results_fallback(monkeypatch, tmp_path):
    db = tmp_path / "db.db"
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())
    fake_search = SimpleNamespace(aggregate_by_conversation=lambda *a, **k: [], search_chunks=lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setitem(sys.modules, "siftd.api.search", fake_search)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: [])
    out = _run(hr.ui_search.fn(SimpleNamespace(), db, q="nomatch"))
    assert "No results for" in out.content


def test_ui_follow_poll_and_first_load(monkeypatch):
    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: "/tmp/f")
    monkeypatch.setattr(
        "siftd.api.peek.read_session_detail",
        lambda *a, **k: SimpleNamespace(
            info=SimpleNamespace(session_id="sid123456789", workspace_name="ws", branch="b", model="m", exchange_count=3, adapter_name="a"),
            exchanges=[{"x": 1}],
        ),
    )
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())

    poll = _run(hr.ui_follow.fn(sid="sid123", poll=True))
    assert "<detail/>" in poll.content

    full = _run(hr.ui_follow.fn(sid="sid123", poll=False))
    assert "follow-content" in full.content and "every 2s" in full.content


def test_ui_stats_exception_branches(monkeypatch, tmp_path):
    db = tmp_path / "db.db"
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: {"ok": True})
    monkeypatch.setattr("siftd.api.stats.get_usage_summary", lambda **_k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.stats.get_usage_by_model", lambda **_k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.stats.get_usage_by_workspace", lambda **_k: (_ for _ in ()).throw(RuntimeError("x")))
    out = _run(hr.ui_stats.fn(SimpleNamespace(), db))
    assert "<stats/>" in out.content


def test_ui_folio_ambiguous_prefix_stub(monkeypatch, tmp_path):
    # /query is list-only now — the folio owns the ambiguous-id UX.
    from siftd.api.conversations import AmbiguousPrefix

    amb = AmbiguousPrefix("01AMBPFX", ["01AMBPFXA1B2C3D4E5F6G7H8", "01AMBPFXB2C3D4E5F6G7H8I9"], 2)
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())
    monkeypatch.setattr(
        "siftd.api.conversations.get_conversation",
        lambda *_a, **_k: (_ for _ in ()).throw(amb),
    )
    resp = _run(hr.ui_folio.fn(SimpleNamespace(), tmp_path / "db.db", id="01AMBPFX"))
    assert "ambiguous id" in resp.content and "2 matches" in resp.content


def test_ui_export_ambiguous_prefix(monkeypatch, tmp_path):
    from siftd.api.conversations import AmbiguousPrefix

    db = tmp_path / "db.db"
    amb = AmbiguousPrefix("01AMBPFX", ["01AMBPFXA1B2C3D4E5F6G7H8", "01AMBPFXB2C3D4E5F6G7H8I9"], 2)

    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())
    monkeypatch.setattr(
        "siftd.api.dispatch.execute",
        lambda _op: (_ for _ in ()).throw(amb),
    )

    export_resp = _run(hr.ui_export.fn(SimpleNamespace(), db, id="01AMBPFX", format="md"))
    assert "Ambiguous prefix" in export_resp.content


def test_ui_tag_success(monkeypatch, tmp_path):
    db = tmp_path / "db.db"

    class _Req:
        async def form(self):
            return {"action": "apply", "id": "cid", "tag": "tag1"}

    monkeypatch.setattr("siftd.api.tags.modify_conversation_tag", lambda *a, **k: ["tag1"])
    monkeypatch.setattr("siftd.output.html_fmt.render_tag_section", lambda *a, **k: "<tags/>")
    tag_out = _run(hr.ui_tag.fn(_Req(), db))
    assert "<tags/>" in tag_out.content
