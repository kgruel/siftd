import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import html_routes as hr


def _run(result):
    # Most UI handlers are sync (threadpool via sync_to_thread); ui_tag is async.
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


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
    # A facet (workspace) with no content term takes the browse/dispatch path
    # (Slice 2b: bare no-facet/no-term renders the prompt instead).
    out = _run(hr.ui_query.fn(SimpleNamespace(), tmp_path / "db.db", workspace="/proj", since=None, before=None, model="", tool=None, tag=[""], search="", owner=None, n=5, mode="auto", view="chunks", sort="score", threshold=None, full=None))
    assert "<list/>" in out.content


def test_ui_sessions_and_follow_branches(monkeypatch, tmp_path):
    db = tmp_path / "db.db"
    monkeypatch.setattr("siftd.api.conversations.list_conversations", lambda **_k: [])

    monkeypatch.setattr("siftd.api.peek.list_active_sessions", lambda **_k: [])
    out = _run(hr.ui_sessions.fn(SimpleNamespace(), db, live_enabled=True)).content
    assert "no live sessions on this host" in out

    monkeypatch.setattr(
        "siftd.api.peek.list_active_sessions",
        lambda **_k: [SimpleNamespace(session_id="s12345678", workspace_name="ws", branch="b", model="m", exchange_count=2, adapter_name="a", last_activity=0.0, started_at=None)],
    )
    out = _run(hr.ui_sessions.fn(SimpleNamespace(), db, live_enabled=True)).content
    assert "card--live" in out and 'hx-get="/follow?sid=s12345678"' in out

    # live_enabled=False: no live zone, no follow links, no peek scan at all
    monkeypatch.setattr(
        "siftd.api.peek.list_active_sessions",
        lambda **_k: (_ for _ in ()).throw(AssertionError("scan must not run")),
    )
    out = _run(hr.ui_sessions.fn(SimpleNamespace(), db, live_enabled=False)).content
    assert "zone--live" not in out and "/follow" not in out

    assert "No session ID" in _run(hr.ui_follow.fn(SimpleNamespace(), sid="")).content

    from siftd.api.peek import AmbiguousSessionError

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: (_ for _ in ()).throw(AmbiguousSessionError("x", [])))
    assert "Ambiguous" in _run(hr.ui_follow.fn(SimpleNamespace(), sid="x")).content

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: None)
    assert "Session not found" in _run(hr.ui_follow.fn(SimpleNamespace(), sid="x")).content

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: "/tmp/f")
    monkeypatch.setattr("siftd.api.peek.read_session_detail", lambda *a, **k: None)
    assert "Cannot read session" in _run(hr.ui_follow.fn(SimpleNamespace(), sid="x")).content


def test_ui_tags_suggest_and_export(monkeypatch, tmp_path):
    db = tmp_path / "db.db"
    monkeypatch.setattr("siftd.output.format_registry.get_format", lambda _n: _Fmt())

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
    monkeypatch.setattr("siftd.api.stats.list_models", lambda **k: ["m1"])
    monkeypatch.setattr("siftd.api.stats.list_workspaces", lambda **k: [{"path": "/w1"}])
    monkeypatch.setattr("siftd.api.tags.list_tags", lambda **k: [SimpleNamespace(name="tag1")])
    # Pass facet params explicitly (off-route, Parameter defaults are markers).
    out = _run(hr.ui_meta.fn(
        SimpleNamespace(), tmp_path / "db.db", None, tag=None, view="chunks",
        mode="auto", workspace=None, model=None, tool=None, owner=None,
        since=None, before=None, sort="score", threshold=None, full=None,
    ))
    assert "/w1" in out.content and "tag1" in out.content and "m1" in out.content


def test_ui_follow_renders_live_folio(monkeypatch):
    # Follow IS the folio rendered from a live source: real html renderer over
    # in-memory peek exchanges — no DB, no separate poll fragment.
    from siftd.domain.peek import PeekExchange, PeekNarrativeBlock

    monkeypatch.setattr("siftd.api.peek.find_session_file", lambda _sid: "/tmp/f")
    monkeypatch.setattr(
        "siftd.api.peek.read_session_detail",
        lambda *a, **k: SimpleNamespace(
            info=SimpleNamespace(session_id="sid123456789", workspace_name="ws", branch="b", model="m", exchange_count=1, adapter_name="a"),
            exchanges=[PeekExchange(
                timestamp="2026-06-12T10:00:00Z",
                prompt_text="hello",
                narrative=[PeekNarrativeBlock(block_type="text", content="world")],
                tool_calls=[("shell.execute", 2)],
                input_tokens=10,
                output_tokens=5,
            )],
        ),
    )

    out = _run(hr.ui_follow.fn(SimpleNamespace(), sid="sid123")).content
    # Folio chrome under the Sessions view, self-refreshing as a whole fragment.
    assert "folio--live" in out and 'data-view="sessions"' in out
    assert 'hx-get="/follow?sid=sid123"' in out and "every 2s" in out
    assert 'hx-swap="outerHTML"' in out
    # Projection carried the content: turns, ledger, token sum.
    assert "hello" in out and "world" in out
    assert "shell.execute" in out
    # Pre-ingest: no curation (tags/export need the DB), cost stays an em dash.
    assert "add tag" not in out and "&mdash;" in out


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
