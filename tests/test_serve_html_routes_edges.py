import asyncio

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import html_routes as hr


def _run(coro):
    return asyncio.run(coro)


def test_html_helpers_detail_and_tool_chars():
    assert hr._hx_detail("", "cid") == ""
    detail = hr._hx_detail("/query", "cid", "/")
    assert 'hx-get="/query?id=cid"' in detail and 'hx-push-url="/?id=cid"' in detail

    f_full = hr._fidelity(depth=3, tools=True, thinking=True)
    f_brief = hr._fidelity(depth=1)
    assert f_full.shows("tools") and f_full.shows("thinking")
    assert hr._tool_chars(f_full) == 0 and hr._tool_chars(f_brief) == 120


def test_page_shell_modes():
    shell_q = hr._page_shell(search_q="abc")
    shell_follow = hr._page_shell(follow_sid="sid-1")
    shell_id = hr._page_shell(conv_id="cid-1")
    # Swiss deep-link remap: ?q= -> Search (live /find), ?follow= -> Sessions, ?id= -> folio.
    assert "/find?q=abc" in shell_q
    assert "/view/sessions" in shell_follow
    assert "/folio?id=cid-1" in shell_id
    # The mounted view is the current one in the rail.
    assert 'data-view="transcript"' in shell_id and 'aria-current="page"' in shell_id


def test_ui_shell_returns_html_response(tmp_path):
    resp = _run(hr.ui_shell.fn(
        db_path=tmp_path / "db.db", auth_config=None, id="cid", q="qq", follow=None,
    ))
    assert resp.media_type == "text/html" and "<!DOCTYPE html>" in resp.content
    assert "chrome--swiss" in resp.content


def test_ui_meta_handles_data_source_failures(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.stats.get_stats", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.stats.list_workspaces", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.tags.list_tags", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))

    resp = _run(hr.ui_meta.fn(object(), tmp_path / "db.db", None))

    assert resp.media_type == "text/html"
    assert "<select" in resp.content and 'name="workspace"' in resp.content
