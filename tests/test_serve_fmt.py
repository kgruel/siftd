"""Tests for siftd.serialization.serve_fmt — serve-side JSON renderers."""

from dataclasses import fields as dataclass_fields
from types import SimpleNamespace as NS

from siftd.api.tags import TagInfo
from siftd.serialization.serve_fmt import (
    render_detail,
    render_export,
    render_list,
    render_search,
    render_stats,
    render_tags,
    render_workspaces,
)

_F = NS(depth=1)
_mock_detail = "siftd.serialization.conversations.serialize_conversation_detail"
_mock_list = "siftd.serialization.conversations.serialize_conversation_list"


def test_stats(monkeypatch):
    monkeypatch.setattr("siftd.serialization.stats.serialize_stats", lambda s: {"ok": 1})
    assert render_stats("x", _F) == {"ok": 1}


def test_workspaces():
    r = render_workspaces(
        [{"id": "01HWS", "path": "/a", "git_remote": None, "convs": 3, "last_activity": "d"}],
        _F,
    )
    assert r["workspaces"][0]["conversations"] == 3
    assert r["workspaces"][0]["id"] == "01HWS"


def test_tags():
    t = TagInfo(
        name="b",
        description="desc",
        created_at="2024-01-01T00:00:00Z",
        conversation_count=1,
        workspace_count=1,
        tool_call_count=0,
        exchange_count=2,
        prompt_count=0,
        response_count=0,
    )
    payload = render_tags([t], _F)["tags"][0]
    assert payload["name"] == "b"
    assert set(payload) == {f.name for f in dataclass_fields(TagInfo)}


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
