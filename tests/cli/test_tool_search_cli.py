"""CLI-focused tests for siftd.cli.tool_search."""

import json
from types import SimpleNamespace

from siftd.cli.tool_search import (
    _compact_display_path,
    _compact_snippet,
    _compact_subject,
    _format_workspace_group,
    _summarize_group_results,
    cmd_tool_search,
)


def _args(**kwargs):
    base = {
        "query": [],
        "db": None,
        "limit": 20,
        "json": False,
        "grouped": True,
        "show_snippets": False,
        "rebuild_index": False,
        "workspace": None,
        "model": None,
        "since": None,
        "before": None,
        "tag": None,
        "all_tags": None,
        "no_tag": None,
        "tool": None,
        "tool_tag": None,
        "owner": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class _R(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _result(**kwargs):
    base = {
        "tool_name": "shell.execute",
        "status": "success",
        "timestamp": "2024-01-01",
        "tool_call_id": "tc1",
        "conversation_id": "c1",
        "response_id": "r1",
        "workspace_path": "/work/repo",
        "path": None,
        "ext": None,
        "command": None,
        "pattern": None,
        "result_snippet": None,
        "arg": None,
        "basename": None,
        "command_verb": None,
        "tool_family": "shell",
        "rank": None,
    }
    base.update(kwargs)
    return _R(**base)


def _delegated_payload(parsed, results):
    return {
        "query": parsed.raw,
        "fields": parsed.fields,
        "bare_terms": parsed.bare_terms,
        "unknown_fields": parsed.unknown_fields,
        "result_count": len(results),
        "results": [r.to_dict() for r in results],
    }


def test_cmd_tool_search_usage_and_error(monkeypatch, capsys):
    assert cmd_tool_search(_args()) == 1

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(ValueError("bad")))
    assert cmd_tool_search(_args(query=["x"])) == 1


def test_cmd_tool_search_json_and_grouped_text(monkeypatch, capsys):
    parsed = SimpleNamespace(raw="x", fields={}, bare_terms=[], unknown_fields={})
    r = _result()

    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (parsed, [r]))
    monkeypatch.setattr(
        "siftd.api.tool_search.group_tool_search_results",
        lambda rows: [SimpleNamespace(workspace_path="/work/repo", conversation_id="c1", tool_call_count=1, results=rows)],
    )

    assert cmd_tool_search(_args(query=["x"], json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["query"] == "x"

    assert cmd_tool_search(_args(query=["x"], grouped=True, show_snippets=True)) == 0
    assert "match" in capsys.readouterr().out


def test_cmd_tool_search_serve_json_and_ungrouped(monkeypatch, capsys):
    monkeypatch.setattr(
        "siftd.serve.delegation.try_serve",
        lambda op: {
            "query": "x",
            "fields": {},
            "bare_terms": [],
            "unknown_fields": {},
            "result_count": 0,
            "results": [],
        },
    )
    assert cmd_tool_search(_args(query=["x"], json=True)) == 0
    delegated = json.loads(capsys.readouterr().out)
    assert delegated["query"] == "x"
    assert delegated["groups"] == []

    parsed = SimpleNamespace(raw="x", fields={}, bare_terms=[], unknown_fields={"bad": ["v"]})
    r = _result(command="git status", result_snippet="ok", path="/a/b/c.py", pattern="needle")
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (parsed, [r]))
    monkeypatch.setattr("siftd.api.tool_search.group_tool_search_results", lambda rows: [])

    assert cmd_tool_search(_args(query=["x"], grouped=False)) == 0
    err = capsys.readouterr().err
    assert "unknown fields" in err


def test_cmd_tool_search_json_parity_local_vs_delegated(monkeypatch, capsys):
    parsed = SimpleNamespace(raw="x", fields={"tool": ["shell.execute"]}, bare_terms=["x"], unknown_fields={})
    r = _result(command="git status")
    groups = [SimpleNamespace(to_dict=lambda: {"conversation_id": "c1", "tool_call_count": 1})]

    monkeypatch.setattr("siftd.api.tool_search.group_tool_search_results", lambda rows: groups)
    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (parsed, [r]))
    assert cmd_tool_search(_args(query=["x"], json=True)) == 0
    local_payload = json.loads(capsys.readouterr().out)

    monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: _delegated_payload(parsed, [r]))
    assert cmd_tool_search(_args(query=["x"], json=True)) == 0
    delegated_payload = json.loads(capsys.readouterr().out)

    assert delegated_payload == local_payload


def test_cmd_tool_search_no_results(monkeypatch, capsys):
    parsed = SimpleNamespace(raw="x", fields={}, bare_terms=[], unknown_fields={})
    monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (parsed, []))
    monkeypatch.setattr("siftd.api.tool_search.group_tool_search_results", lambda rows: [])
    assert cmd_tool_search(_args(query=["x"])) == 0
    assert "No tool-call matches" in capsys.readouterr().out


def test_compact_helpers():
    assert _format_workspace_group(None) == "(no workspace)"
    assert _format_workspace_group("/repo") in {"repo", "/repo"}
    assert _format_workspace_group("/") == "/"

    assert _compact_display_path("/work/repo/src/siftd/cli/tool_search.py")
    assert _compact_display_path("/a/b/c/d") == "a/b/c/d"

    r = _result(tool_name="search.grep", pattern="needle", path="/a/b/c/d/e.py")
    assert "grep" in _compact_subject(r)

    r_pat = _result(tool_name="x", pattern="abc")
    assert "pattern" in _compact_subject(r_pat)

    r_arg = _result(tool_name="x", arg="some-arg-value")
    assert "arg" in _compact_subject(r_arg) or "some-arg" in _compact_subject(r_arg)

    r2 = _result(tool_name="file.read", result_snippet="line1\nline2")
    assert _compact_snippet(r2)

    r3 = _result(tool_name="file.read", result_snippet="different")
    summarized = _summarize_group_results([r2, r3])
    assert len(summarized) == 2

    summarized_dup = _summarize_group_results([r2, r2])
    assert summarized_dup and "×2" in summarized_dup[0]["line"]
