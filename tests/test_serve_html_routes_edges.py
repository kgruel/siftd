import asyncio
from types import SimpleNamespace

from siftd.serve import html_routes as hr


def _run(coro):
    return asyncio.run(coro)


def test_wants_html_header_and_accept_logic():
    assert hr._wants_html(SimpleNamespace(headers={"HX-Request": "true"}))
    assert hr._wants_html(SimpleNamespace(headers={"Accept": "text/html"}))
    assert not hr._wants_html(SimpleNamespace(headers={"Accept": "text/html,application/json"}))


def test_html_helpers_detail_and_tool_chars():
    assert hr._hx_detail("", "cid") == ""
    detail = hr._hx_detail("/ui/query", "cid", "/ui")
    assert 'hx-get="/ui/query?id=cid"' in detail and 'hx-push-url="/ui?id=cid"' in detail

    f_full = hr._fidelity(depth=3, tools=True, thinking=True)
    f_brief = hr._fidelity(depth=1)
    assert f_full.shows("tools") and f_full.shows("thinking")
    assert hr._tool_chars(f_full) == 0 and hr._tool_chars(f_brief) == 120


def test_page_shell_modes():
    shell_q = hr._page_shell(search_q="abc")
    shell_follow = hr._page_shell(follow_sid="sid-1")
    shell_id = hr._page_shell(conv_id="cid-1")
    assert '/ui/search?q=abc' in shell_q
    assert '/ui/follow?sid=sid-1' in shell_follow
    assert '/ui/query?id=cid-1' in shell_id


def test_ui_shell_returns_html_response():
    resp = _run(hr.ui_shell.fn(id="cid", q="qq", follow=None))
    assert resp.media_type == "text/html" and "<!DOCTYPE html>" in resp.content


def test_ui_meta_handles_data_source_failures(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.stats.list_workspaces", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.tags.list_tags", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))

    resp = _run(hr.ui_meta.fn(tmp_path / "db.db"))

    assert resp.media_type == "text/html"
    assert "<select" in resp.content and 'name="workspace"' in resp.content
