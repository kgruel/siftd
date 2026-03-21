"""Tests for VSCode adapter."""

import json
import shutil
from pathlib import Path

from conftest import FIXTURES_DIR

from siftd.adapters import vscode
from siftd.domain.source import Source


def _vscode_session_dir(tmp_path, fixture, ws="/test/workspace"):
    """Set up VSCode workspace dir structure with fixture. Returns Source."""
    h = tmp_path / "hash"
    cs = h / "chatSessions"
    cs.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / fixture, cs / Path(fixture).name)
    if ws:
        (h / "workspace.json").write_text(json.dumps({"folder": f"file://{ws}"}))
    return Source(kind="file", location=cs / Path(fixture).name)


class TestVscodeAdapter:
    def test_can_handle(self):
        assert vscode.can_handle(Source(kind="file", location=Path("/mock/chatSessions/test.json")))
        assert vscode.can_handle(Source(kind="file", location=Path("/mock/chatSessions/test.jsonl")))
        assert not vscode.can_handle(Source(kind="file", location=FIXTURES_DIR / "vscode_minimal.json"))
        assert not vscode.can_handle(Source(kind="directory", location=Path("/mock/chatSessions")))

    def test_parse_json_full(self, tmp_path):
        conv = list(vscode.parse(_vscode_session_dir(tmp_path, "vscode_minimal.json")))[0]
        assert conv.workspace_path == "/test/workspace" and conv.harness.name == "vscode"
        assert conv.started_at and "2024-02-15" in conv.started_at and conv.ended_at
        assert len(conv.prompts) == 2
        assert "read a file" in conv.prompts[0].content[0].content["text"]
        assert conv.prompts[0].responses[0].model == "gpt-4o"
        r0_text = [b for b in conv.prompts[0].responses[0].content if b.block_type == "text"]
        assert r0_text and "open()" in r0_text[0].content["text"]
        tc = conv.prompts[1].responses[0].tool_calls[0]
        assert tc.tool_name == "listFiles" and tc.result == {"files": ["README.md", "src/", "tests/"]}
        assert [b for b in conv.prompts[1].responses[0].content if b.block_type == "text_edit"]
        assert all(r.usage is None for p in conv.prompts for r in p.responses)

    def test_parse_jsonl_full(self, tmp_path):
        conv = list(vscode.parse(_vscode_session_dir(tmp_path, "vscode_minimal.jsonl")))[0]
        assert conv.workspace_path == "/test/workspace" and "2024-02-15" in conv.started_at
        assert len(conv.prompts) == 2 and conv.prompts[1].responses[0].tool_calls[0].tool_name == "listFiles"

    def test_parse_edge_cases(self, tmp_path):
        cs = tmp_path / "nw" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", cs / "t.json")
        assert list(vscode.parse(Source(kind="file", location=cs / "t.json")))[0].workspace_path is None
        cs2 = tmp_path / "e" / "chatSessions"
        cs2.mkdir(parents=True)
        empty = {"version": 3, "sessionId": "e", "creationDate": 1708012345678, "requests": []}
        (cs2 / "e.json").write_text(json.dumps(empty))
        assert list(vscode.parse(Source(kind="file", location=cs2 / "e.json"))) == []
        (cs2 / "e.jsonl").write_text(json.dumps({"kind": 0, "v": empty}) + "\n")
        assert list(vscode.parse(Source(kind="file", location=cs2 / "e.jsonl"))) == []
        (cs2 / "s.json").write_text(json.dumps({**empty, "sessionId": "s",
            "requests": [{"requestId": "r1", "message": {"text": "Hello"}, "timestamp": 1708012345678,
                "modelId": "gpt-4o", "response": [{"kind": "markdownContent", "content": {"value": "Hi"}}], "responseId": "r1"}]}))
        assert list(vscode.parse(Source(kind="file", location=cs2 / "s.json")))

    def test_replay_path_helpers(self):
        obj = {"requests": [{"response": [], "result": None}]}
        vscode._set_at_path(obj, ["requests", 0, "result"], {"ok": True})
        vscode._append_at_path(obj, ["requests", 0, "response"], [{"kind": "text"}])
        assert obj["requests"][0]["result"] == {"ok": True} and len(obj["requests"][0]["response"]) == 1
        obj2 = {"requests": []}
        vscode._append_at_path(obj2, ["requests"], [{"id": "r1"}])
        vscode._set_at_path(obj2, ["requests", 99, "result"], "v")
        assert len(obj2["requests"]) == 1


class TestVSCodeNormalizerAndPeek:
    def test_peek_json_and_jsonl(self, tmp_path):
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

    def test_normalizer_and_parse_errors(self, tmp_path):
        n = vscode.normalize_record
        assert n({"_kind": "user", "_ts": "T", "message": {"text": "hi"}}).content_blocks[0]["text"] == "hi"
        nr = n({"_kind": "assistant", "_ts": "T", "modelId": "m", "response": [
            {"kind": "markdownContent", "content": {"value": "ok"}}, {"kind": "toolInvocationSerialized", "toolName": "f"}]})
        assert nr.kind == "assistant" and len(nr.content_blocks) == 2
        assert n({"_kind": "unknown"}) is None
        cs = tmp_path / "chatSessions"
        cs.mkdir()
        (cs / "bad.json").write_bytes(b'\xff\xfe bad')
        assert list(vscode.parse(Source(kind="file", location=cs / "bad.json"))) == []
        (cs / "b.jsonl").write_text('not json\n{"kind":0,"v":null}\n')
        assert list(vscode.parse(Source(kind="file", location=cs / "b.jsonl"))) == []
