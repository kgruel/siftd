import asyncio

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import html_routes as hr


def _run(result):
    # Most UI handlers are sync (threadpool via sync_to_thread); ui_tag is async.
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_html_helpers_detail_and_tool_chars():
    from siftd.output.html_fmt import _hx_detail

    assert _hx_detail("", "cid") == ""
    detail = _hx_detail("/folio", "cid", "/")
    # Rows mount the folio into the Swiss shell's #main — the only swap target
    # that exists; "#detail" was the two-pane container and is gone.
    assert 'hx-get="/folio?id=cid"' in detail and 'hx-push-url="/?id=cid"' in detail
    assert 'hx-target="#main"' in detail

    f_full = hr._fidelity(depth=3, tools=True, thinking=True)
    assert f_full.shows("tools") and f_full.shows("thinking")


def test_page_shell_modes():
    shell_q = hr._page_shell(search={"q": "abc"})
    shell_follow = hr._page_shell(follow_sid="sid-1")
    shell_id = hr._page_shell(conv_id="cid-1")
    # Swiss deep-link remap: ?q= -> Search (inline find host), ?follow= -> Sessions, ?id= -> folio.
    assert 'class="find"' in shell_q and "/query?search=abc" in shell_q
    assert "/view/sessions" in shell_follow
    assert "/folio?id=cid-1" in shell_id
    # The mounted view is the current one in the rail.
    assert 'data-view="transcript"' in shell_id and 'aria-current="page"' in shell_id


def test_ui_shell_returns_html_response(tmp_path):
    resp = _run(hr.ui_shell.fn(
        db_path=tmp_path / "db.db", live_enabled=True, auth_config=None,
        id="cid", q="qq", follow=None,
    ))
    assert resp.media_type == "text/html" and "<!DOCTYPE html>" in resp.content
    assert "chrome--swiss" in resp.content


def test_ui_export_escapes_reflected_id_in_not_found(tmp_path):
    # The /export not-found fragment reflects the requested id; it must escape it
    # (the lone escaping holdout among the error fragments) so a crafted id can't
    # inject markup under the script-src 'unsafe-inline' CSP.
    from siftd.storage.sqlite import create_database

    db = tmp_path / "db.db"
    create_database(db).close()

    resp = _run(hr.ui_export.fn(object(), db, id="<img src=x>", format="md"))
    assert resp.media_type == "text/html"
    assert "<img src=x>" not in resp.content  # raw markup never reflected
    assert "&lt;img src=x&gt;" in resp.content  # escaped, truncated to 12 visible chars


def test_ui_meta_handles_data_source_failures(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.api.stats.list_models", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.stats.list_workspaces", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("siftd.api.tags.list_tags", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))

    # Direct .fn call: pass the facet params explicitly (litestar's Parameter
    # defaults aren't resolved off-route, so unspecified args are marker objects).
    resp = _run(hr.ui_meta.fn(
        object(), tmp_path / "db.db", None, tag=None, view="chunks", mode="auto",
        workspace=None, model=None, tool=None, owner=None, since=None, before=None,
        sort="score", threshold=None, full=None,
    ))

    assert resp.media_type == "text/html"
    assert "<select" in resp.content and 'name="workspace"' in resp.content


def test_ui_meta_renders_tool_facet(monkeypatch, tmp_path):
    # The tool facet (3b-2) is a <select> sourced from the corpus tool vocabulary
    # (list_tools, usage-ordered), riding the same control substrate as the other
    # facets so a selected tool round-trips through the URL.
    monkeypatch.setattr(
        "siftd.api.stats.list_tools",
        lambda **k: ["shell.execute", "fs.read"],
    )
    resp = _run(hr.ui_meta.fn(
        object(), tmp_path / "db.db", None, tag=None, view="chunks", mode="auto",
        workspace=None, model=None, tool="shell.execute",
        owner=None, since=None, before=None,
        sort="score", threshold=None, full=None,
    ))
    assert 'name="tool"' in resp.content
    assert '<option value="shell.execute" selected>' in resp.content
    assert '<option value="fs.read">' in resp.content


def test_ui_meta_renders_clean_win_controls(tmp_path):
    # 3b-3 clean-wins: the sort toggle (inline) + threshold/full-text controls
    # (in the "more filters" disclosure), with the engaged values pre-filled so
    # they round-trip through the canonical URL.
    resp = _run(hr.ui_meta.fn(
        object(), tmp_path / "db.db", None, tag=None, view="chunks", mode="auto",
        workspace=None, model=None, tool=None, owner=None, since=None, before=None,
        sort="time", threshold="0.7", full="1",
    ))
    body = resp.content
    # Sort toggle, with the non-default order selected.
    assert 'name="sort"' in body and '<option value="time" selected>' in body
    # Threshold number input pre-filled; full-text checkbox checked.
    assert 'name="threshold"' in body and 'value="0.7"' in body
    assert 'name="full"' in body and "checked" in body


def test_ui_query_clamps_sort_time_for_non_chunks_views(monkeypatch, tmp_path):
    # sort=time is valid only for the chunks shape; the thread/conversations
    # shapes impose their own order. ui_query clamps it back to score before the
    # engine sees it, so a hand-edited URL never trips axis validation.
    captured = {}

    def _fake_fragment(db_path, term, fmt, ctx, **kw):
        captured.update(kw)
        from siftd.serve.html_routes import _html_response
        return _html_response("<ok/>")

    monkeypatch.setattr(hr, "_find_search_fragment", _fake_fragment)
    monkeypatch.setattr("siftd.api.sanitize_fts5_query", lambda t: type("R", (), {"fts_query": t})())

    _run(hr.ui_query.fn(
        object(), tmp_path / "db.db", workspace=None, since=None, before=None,
        model=None, tool=None, tag=None, search="needle", owner=None, n=5,
        mode="auto", view="thread", sort="time", threshold=None, full=None,
    ))
    assert captured["sort"] == "score"
