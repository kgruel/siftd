"""Tests for siftd.serialization.serve_fmt — serve-side JSON renderers."""

from types import SimpleNamespace as NS

from siftd.serialization.serve_fmt import (
    render_detail,
    render_export,
    render_list,
    render_search,
    render_stats,
    render_tags,
    render_tool_search,
    render_tools,
    render_tools_by_workspace,
    render_workspaces,
)

_F = NS(depth=1)
_mock_detail = "siftd.serialization.conversations.serialize_conversation_detail"
_mock_list = "siftd.serialization.conversations.serialize_conversation_list"


def test_stats(monkeypatch):
    monkeypatch.setattr("siftd.serialization.stats.serialize_stats", lambda s: {"ok": 1})
    assert render_stats("x", _F) == {"ok": 1}


def test_workspaces():
    r = render_workspaces([{"path": "/a", "convs": 3, "last_activity": "d"}], _F)
    assert r["workspaces"][0]["conversations"] == 3


def test_tags():
    t = NS(name="b", conversation_count=1, workspace_count=1, tool_call_count=0, prompt_count=2)
    assert render_tags([t], _F)["tags"][0]["name"] == "b"


def test_tool_search_empty():
    assert render_tool_search((NS(raw="x"), []), _F)["result_count"] == 0


def test_tool_search_hit():
    hit = NS(tool_call_id="t", conversation_id="c", timestamp="d",
             tool_name="e", tool_family="f", status="ok", path="/p",
             basename="f", command="cmd", command_verb="v",
             result_snippet="r", workspace_path="/w", rank=1)
    assert render_tool_search((NS(raw="q"), [hit]), _F)["result_count"] == 1


def test_tools():
    assert render_tools([NS(name="r", count=5)], _F)["total"] == 5


def test_tools_empty():
    assert render_tools([], _F)["total"] == 0


def test_tools_by_workspace():
    ws = NS(workspace="/w", total=3, tags=[NS(name="x", count=3)])
    assert render_tools_by_workspace([ws], _F)["workspaces"][0]["total"] == 3


def test_search_empty():
    assert render_search([], _F)["result_count"] == 0


def test_export(monkeypatch):
    monkeypatch.setattr(_mock_detail, lambda c, **kw: {"id": c})
    assert render_export(["c1"], _F)["conversations"] == [{"id": "c1"}]


def test_detail_none():
    assert render_detail(None, _F)["error"] == "conversation not found"


def test_detail(monkeypatch):
    monkeypatch.setattr(_mock_detail, lambda c, **kw: {"id": c})
    assert render_detail("c1", _F)["conversation"]["id"] == "c1"


def test_list(monkeypatch):
    monkeypatch.setattr(_mock_list, lambda s, **kw: [{"id": x} for x in s])
    assert len(render_list(["a", "b"], _F)["conversations"]) == 2
