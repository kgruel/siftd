"""Tests for VSCode adapter."""

import json
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR

from siftd.adapters import vscode
from siftd.adapters.sdk import AdapterParseError
from siftd.domain.source import Source

S = Source


def _src(tmp_path, fixture, ws="/test/workspace"):
    """Set up VSCode dir structure with fixture. Returns Source."""
    cs = tmp_path / "h" / "chatSessions"
    cs.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES_DIR / fixture, cs / Path(fixture).name)
    if ws:
        (cs.parent / "workspace.json").write_text(json.dumps({"folder": f"file://{ws}"}))
    return S(kind="file", location=cs / Path(fixture).name)


class TestVscode:
    def test_can_handle(self):
        assert vscode.can_handle(S(kind="file", location=Path("/m/chatSessions/t.json")))
        assert vscode.can_handle(S(kind="file", location=Path("/m/chatSessions/t.jsonl")))
        assert vscode.can_handle(S(kind="file", location=Path("/m/emptyWindowChatSessions/t.json")))
        assert vscode.can_handle(S(kind="file", location=Path("/m/emptyWindowChatSessions/t.jsonl")))
        assert not vscode.can_handle(S(kind="file", location=Path("/m/chatSessions/t.txt")))
        assert not vscode.can_handle(S(kind="file", location=FIXTURES_DIR / "vscode_minimal.json"))
        assert not vscode.can_handle(S(kind="directory", location=Path("/m/chatSessions")))

    def test_parse_json(self, tmp_path):
        conv = list(vscode.parse(_src(tmp_path, "vscode_minimal.json")))[0]
        assert conv.workspace_path == "/test/workspace" and conv.harness.name == "vscode"
        assert conv.started_at and "2024-02-15" in conv.started_at and conv.ended_at and len(conv.prompts) == 2
        assert "read a file" in conv.prompts[0].content[0].content["text"]
        assert conv.prompts[0].responses[0].model == "gpt-4o"
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "text" and "open()" in b.content["text"]]
        tc = conv.prompts[1].responses[0].tool_calls[0]
        assert tc.tool_name == "listFiles" and tc.result == {"files": ["README.md", "src/", "tests/"]}
        assert [b for b in conv.prompts[1].responses[0].content if b.block_type == "text_edit"]

    def test_parse_jsonl(self, tmp_path):
        conv = list(vscode.parse(_src(tmp_path, "vscode_minimal.jsonl")))[0]
        assert conv.workspace_path == "/test/workspace" and "2024-02-15" in conv.started_at
        assert len(conv.prompts) == 2 and conv.prompts[1].responses[0].tool_calls[0].tool_name == "listFiles"

    def test_parse_edges(self, tmp_path):
        cs = tmp_path / "nw" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", cs / "t.json")
        assert list(vscode.parse(S(kind="file", location=cs / "t.json")))[0].workspace_path is None
        cs2 = tmp_path / "e" / "chatSessions"
        cs2.mkdir(parents=True)
        empty = {"version": 3, "sessionId": "e", "creationDate": 1708012345678, "requests": []}
        (cs2 / "e.json").write_text(json.dumps(empty))
        (cs2 / "e.jsonl").write_text(json.dumps({"kind": 0, "v": empty}) + "\n")
        assert list(vscode.parse(S(kind="file", location=cs2 / "e.json"))) == []
        assert list(vscode.parse(S(kind="file", location=cs2 / "e.jsonl"))) == []
        req = {"requestId": "r1", "message": {"text": "Hi"}, "timestamp": 1708012345678,
            "modelId": "m", "response": [{"kind": "markdownContent", "content": {"value": "ok"}}],
            "responseId": "r1"}
        (cs2 / "s.json").write_text(json.dumps({**empty, "sessionId": "s", "requests": [req]}))
        assert list(vscode.parse(S(kind="file", location=cs2 / "s.json")))
        # workspace.json with plain path (no file://) and empty folder
        (cs2.parent / "workspace.json").write_text(json.dumps({"folder": "/plain"}))
        assert list(vscode.parse(S(kind="file", location=cs2 / "s.json")))[0].workspace_path == "/plain"
        (cs2.parent / "workspace.json").write_text(json.dumps({"folder": ""}))
        assert list(vscode.parse(S(kind="file", location=cs2 / "s.json")))[0].workspace_path is None

    def test_path_helpers(self):
        obj = {"requests": [{"response": [], "result": None}]}
        vscode._set_at_path(obj, ["requests", 0, "result"], {"ok": True})
        vscode._append_at_path(obj, ["requests", 0, "response"], [{"kind": "text"}])
        assert obj["requests"][0]["result"] == {"ok": True} and len(obj["requests"][0]["response"]) == 1
        obj2 = {"requests": []}
        vscode._append_at_path(obj2, ["requests"], [{"id": "r1"}])
        vscode._set_at_path(obj2, ["requests", 99, "result"], "v")
        assert len(obj2["requests"]) == 1
        # Traversal guards: dict missing key, non-traversable, valid list index set
        vscode._set_at_path({"a": None}, ["a", "b"], "v")
        vscode._set_at_path({"a": "str"}, ["a", "b", "c"], "v")
        lst = [1, 2, 3]
        vscode._set_at_path(lst, [1], 99)
        assert lst[1] == 99
        # _append_at_path guards: missing key, non-traversable, out-of-range list
        vscode._append_at_path({"a": None}, ["a", "b"], [1])
        vscode._append_at_path({"a": "str"}, ["a", "b"], [1])
        vscode._append_at_path([[]], [5], [1])
        # Empty path guards
        obj3 = {"x": 1}
        vscode._set_at_path(obj3, [], "v")
        assert obj3 == {"x": 1}, "empty path should be a no-op"

    def test_peek_normalizer_discover(self, tmp_path):
        cs = tmp_path / "ws" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", cs / "test.json")
        shutil.copy(FIXTURES_DIR / "vscode_minimal.jsonl", cs / "test.jsonl")
        assert vscode.peek_scan(cs / "test.json").exchange_count >= 1
        assert vscode.peek_exchanges(cs / "test.json", last_n=5)[0].prompt_text
        assert vscode.peek_scan(cs / "test.jsonl").exchange_count >= 1
        (tmp_path / "chatSessions").mkdir()
        (tmp_path / "chatSessions" / "e.json").write_text("{}")
        assert vscode.peek_scan(tmp_path / "chatSessions" / "e.json") is None
        n = vscode.normalize_record
        assert n({"_kind": "user", "_ts": "T", "message": {"text": "hi"}}).content_blocks[0]["text"] == "hi"
        nr = n({"_kind": "assistant", "_ts": "T", "modelId": "m", "response": [
            {"kind": "markdownContent", "content": {"value": "ok"}},
            {"kind": "toolInvocationSerialized", "toolName": "f"}]})
        assert nr.kind == "assistant" and len(nr.content_blocks) == 2
        assert n({"_kind": "unknown"}) is None
        assert list(vscode.discover(locations=[str(tmp_path)]))

    def test_empty_window_chat_sessions(self, tmp_path):
        # Sessions without a workspace live directly under
        # globalStorage/emptyWindowChatSessions/ -- no surrounding hash dir
        # or workspace.json, so workspace_path must resolve to None.
        gs = tmp_path / "globalStorage" / "emptyWindowChatSessions"
        gs.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", gs / "ew.json")
        shutil.copy(FIXTURES_DIR / "vscode_minimal.jsonl", gs / "ew.jsonl")

        conv_json = list(vscode.parse(S(kind="file", location=gs / "ew.json")))[0]
        conv_jsonl = list(vscode.parse(S(kind="file", location=gs / "ew.jsonl")))[0]
        assert conv_json.workspace_path is None and conv_jsonl.workspace_path is None
        assert conv_json.harness.name == "vscode" and len(conv_json.prompts) == 2

        # discover() should pick up empty-window files when the parent
        # (analogue of .../User/globalStorage) is passed as a location.
        sources = list(vscode.discover(locations=[str(tmp_path / "globalStorage")]))
        assert {Path(s.location).name for s in sources} == {"ew.json", "ew.jsonl"}

    def test_replay_and_parse_errors(self, tmp_path):
        cs = tmp_path / "chatSessions"
        cs.mkdir(parents=True)
        sess = {"version": 3, "sessionId": "j", "creationDate": 1708012345678, "requests": [
            {"requestId": "r1", "message": "Hi", "timestamp": 1708012345678,
             "modelId": "m", "response": [{"kind": "markdownContent", "content": {"value": "ok"}}],
             "responseId": "r1"}]}
        lines = [json.dumps({"kind": 1, "v": "before-init"}),
            json.dumps({"kind": 0, "v": sess}), "",
            json.dumps({"kind": 1, "k": [], "v": "x"}),
            json.dumps({"kind": 2, "k": [], "v": []})]
        (cs / "j.jsonl").write_text("\n".join(lines) + "\n")
        assert list(vscode.parse(S(kind="file", location=cs / "j.jsonl")))
        (cs / "bad.json").write_bytes(b'\xff\xfe bad')
        with pytest.raises(AdapterParseError, match="could not be read"):
            list(vscode.parse(S(kind="file", location=cs / "bad.json")))
        (cs / "b.jsonl").write_text('not json\n{"kind":0,"v":null}\n')
        with pytest.raises(AdapterParseError, match="invalid JSONL"):
            list(vscode.parse(S(kind="file", location=cs / "b.jsonl")))
        (cs / "missing-requests.json").write_text(json.dumps({"sessionId": "x"}))
        with pytest.raises(AdapterParseError, match="missing a requests array"):
            list(vscode.parse(S(kind="file", location=cs / "missing-requests.json")))
