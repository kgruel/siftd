"""Tests for VSCode adapter.

Inputs are redacted captures of real VS Code chat sessions (schema version 3,
the top-level "version" field) under
tests/fixtures/adapters/vscode/ (see tests/test_fixture_redaction.py). Earlier
hand-written fixtures used response parts VS Code never writes (markdownContent,
toolName/input/result), so the adapter dropped every answer and named every tool
"unknown" while the suite stayed green. Keep new cases to captured shapes.
"""

import json
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR

from siftd.adapters import vscode
from siftd.adapters.sdk import AdapterParseError
from siftd.domain.source import Source

S = Source

GOLDEN = FIXTURES_DIR / "adapters" / "vscode"
JSON_CASE = GOLDEN / "json_format" / "input.json"
JSONL_CASE = GOLDEN / "jsonl_tool_calls" / "input.jsonl"
JSONL_EMPTY_WINDOW = GOLDEN / "jsonl_empty_window" / "input.jsonl"
JSONL_WHOLE_REQUEST = GOLDEN / "jsonl_whole_request" / "input.jsonl"

# Real tool calls in jsonl_tool_calls: 15 emissions, 8 distinct toolCallIds.
REAL_TOOL_NAMES = {
    "copilot_readFile", "copilot_findTextInFiles", "configure_python_environment", "run_in_terminal",
}


def _session_file(tmp_path, source: Path, name: str, ws="/test/workspace") -> Source:
    """Copy a captured session into a chatSessions dir with a workspace.json."""
    cs = tmp_path / "h" / "chatSessions"
    cs.mkdir(parents=True, exist_ok=True)
    dest = cs / name
    shutil.copy(source, dest)
    if ws:
        (cs.parent / "workspace.json").write_text(json.dumps({"folder": f"file://{ws}"}))
    return S(kind="file", location=dest)


def _tool_calls(conv):
    return [tc for p in conv.prompts for r in p.responses for tc in r.tool_calls]


def _text_blocks(conv):
    return [b for p in conv.prompts for r in p.responses for b in r.content if b.block_type == "text"]


class TestVscode:
    def test_can_handle(self):
        assert vscode.can_handle(S(kind="file", location=Path("/m/chatSessions/t.json")))
        assert vscode.can_handle(S(kind="file", location=Path("/m/chatSessions/t.jsonl")))
        assert vscode.can_handle(S(kind="file", location=Path("/m/emptyWindowChatSessions/t.json")))
        assert vscode.can_handle(S(kind="file", location=Path("/m/emptyWindowChatSessions/t.jsonl")))
        assert not vscode.can_handle(S(kind="file", location=Path("/m/chatSessions/t.txt")))
        assert not vscode.can_handle(S(kind="file", location=JSON_CASE))
        assert not vscode.can_handle(S(kind="directory", location=Path("/m/chatSessions")))

    def test_real_json_session_keeps_every_answer_and_tool_call(self, tmp_path):
        conv = list(vscode.parse(_session_file(tmp_path, JSON_CASE, "s.json")))[0]
        assert conv.workspace_path == "/test/workspace" and conv.harness.name == "vscode"
        assert conv.started_at and conv.ended_at and len(conv.prompts) == 2
        assert conv.prompts[0].responses[0].model
        assert "user prompt" in conv.prompts[0].content[0].content["text"]
        # Answer prose is kindless; each part is one text block, none dropped.
        assert len(_text_blocks(conv)) == 24
        assert all(b.content["text"] for b in _text_blocks(conv))

    def test_real_tool_calls_have_real_names_and_inputs(self, tmp_path):
        conv = list(vscode.parse(_session_file(tmp_path, JSONL_CASE, "s.jsonl")))[0]
        calls = _tool_calls(conv)
        assert {tc.tool_name for tc in calls} == REAL_TOOL_NAMES
        # Every real call has some recoverable input; run_in_terminal's invocation text is empty.
        assert all(tc.input for tc in calls)
        terminal = next(tc for tc in calls if tc.tool_name == "run_in_terminal")
        assert terminal.input["command"].startswith("pytest -q tests/test_adapters.py")
        configure = next(tc for tc in calls if tc.tool_name == "configure_python_environment")
        assert configure.result["resultDetails"]["isError"] is True and configure.status == "error"
        search = next(tc for tc in calls if tc.tool_name == "copilot_findTextInFiles")
        assert search.status == "success" and isinstance(search.result["resultDetails"], list)

    def test_each_tool_call_id_is_one_tool_call(self, tmp_path):
        # VS Code re-emits an invocation as it progresses; storage keys tool calls on
        # (conversation, external_id), so a duplicate id would fail the whole file.
        conv = list(vscode.parse(_session_file(tmp_path, JSONL_CASE, "s.jsonl")))[0]
        ids = [tc.external_id for tc in _tool_calls(conv)]
        assert len(ids) == 8 and len(set(ids)) == 8

    def test_superseded_emission_is_dropped_latest_wins(self):
        running = {"kind": "toolInvocationSerialized", "toolCallId": "c1", "toolId": "copilot_readFile",
                   "invocationMessage": {"value": "Reading"}, "isConfirmed": {"type": 1}}
        done = {**running, "isComplete": True, "generatedTitle": "Read file"}
        requests = [{"requestId": "r", "response": [running, {"value": "text"}, done]}]
        kept = vscode._latest_tool_emissions(requests)[0]["response"]
        assert kept == [{"value": "text"}, done]

    @pytest.mark.parametrize("part, status", [
        ({"isComplete": False}, "pending"),
        ({"isComplete": True}, "success"),
        ({"isComplete": True, "resultDetails": [{"uri": {}}]}, "success"),
        ({"isComplete": True, "resultDetails": {"isError": True, "output": []}}, "error"),
    ])
    def test_tool_status_from_real_fields(self, part, status):
        call = vscode._tool_call({"kind": "toolInvocationSerialized", "toolId": "t", "toolCallId": "c", **part})
        assert call.status == status

    def test_jsonl_and_json_of_the_same_session_parse_identically(self, tmp_path):
        from_jsonl = list(vscode.parse(_session_file(tmp_path / "a", JSONL_CASE, "same.jsonl")))
        from_json = list(vscode.parse(_session_file(tmp_path / "b", JSON_CASE, "same.json")))
        assert from_jsonl == from_json and from_jsonl

    def test_peek_and_parse_agree_on_answers_and_tool_calls(self):
        records = list(vscode.iter_vscode_records(JSONL_CASE))
        normalized = [vscode.normalize_record(r) for r in records]
        peek_text = sum(1 for n in normalized if n and n.kind == "assistant"
                        for b in n.content_blocks if b["type"] == "text")
        peek_tools = sum(1 for n in normalized if n and n.kind == "assistant"
                         for b in n.content_blocks if b["type"] == "tool_use")
        conv = list(vscode.parse(Source(kind="file", location=JSONL_CASE)))[0]
        assert peek_text == len(_text_blocks(conv)) == 24
        assert peek_tools == len(_tool_calls(conv)) == 8

    def test_peek_exchanges_show_every_tool_call_input(self):
        # The SDK exchange builder reads only a fixed set of input keys
        # (description, command, file_path, path, pattern). Parse and the normalizer
        # can agree on an input dict that the consumer never reads, so this checks
        # the value at the peek_exchanges() boundary, where it is displayed.
        exchanges = vscode.peek_exchanges(JSONL_CASE, last_n=50)
        calls = [tc for ex in exchanges for block in ex.narrative
                 if block.block_type == "tool_calls" for tc in block.tool_calls]
        assert len(calls) == 8
        assert [tc.tool_name for tc in calls if not tc.input] == []

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

    def test_parse_edges(self, tmp_path):
        cs = tmp_path / "nw" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(JSON_CASE, cs / "t.json")
        assert list(vscode.parse(S(kind="file", location=cs / "t.json")))[0].workspace_path is None
        cs2 = tmp_path / "e" / "chatSessions"
        cs2.mkdir(parents=True)
        empty = {"version": 3, "sessionId": "e", "creationDate": 1708012345678, "requests": []}
        (cs2 / "e.json").write_text(json.dumps(empty))
        (cs2 / "e.jsonl").write_text(json.dumps({"kind": 0, "v": empty}) + "\n")
        assert list(vscode.parse(S(kind="file", location=cs2 / "e.json"))) == []
        assert list(vscode.parse(S(kind="file", location=cs2 / "e.jsonl"))) == []
        req = {"requestId": "r1", "message": {"text": "Hi"}, "timestamp": 1708012345678,
               "modelId": "m", "response": [{"value": "ok"}], "responseId": "r1"}
        (cs2 / "s.json").write_text(json.dumps({**empty, "sessionId": "s", "requests": [req]}))
        assert list(vscode.parse(S(kind="file", location=cs2 / "s.json")))
        # workspace.json with plain path (no file://) and empty folder
        (cs2.parent / "workspace.json").write_text(json.dumps({"folder": "/plain"}))
        assert list(vscode.parse(S(kind="file", location=cs2 / "s.json")))[0].workspace_path == "/plain"
        (cs2.parent / "workspace.json").write_text(json.dumps({"folder": ""}))
        assert list(vscode.parse(S(kind="file", location=cs2 / "s.json")))[0].workspace_path is None

    def test_peek_normalizer_discover(self, tmp_path):
        cs = tmp_path / "ws" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(JSON_CASE, cs / "test.json")
        shutil.copy(JSONL_CASE, cs / "test.jsonl")
        assert vscode.peek_scan(cs / "test.json").exchange_count >= 1
        assert vscode.peek_exchanges(cs / "test.json", last_n=5)[0].prompt_text
        assert vscode.peek_scan(cs / "test.jsonl").exchange_count >= 1
        (tmp_path / "chatSessions").mkdir()
        (tmp_path / "chatSessions" / "e.json").write_text("{}")
        assert vscode.peek_scan(tmp_path / "chatSessions" / "e.json") is None
        n = vscode.normalize_record
        assert n({"_kind": "user", "_ts": "T", "message": {"text": "hi"}}).content_blocks[0]["text"] == "hi"
        nr = n({"_kind": "assistant", "_ts": "T", "modelId": "m", "response": [
            {"value": "ok"},
            {"kind": "toolInvocationSerialized", "toolId": "copilot_readFile", "toolCallId": "c",
             "invocationMessage": {"value": "Reading"}, "isComplete": True}]})
        assert nr.kind == "assistant" and len(nr.content_blocks) == 2
        assert nr.content_blocks[1] == {"type": "tool_use", "name": "copilot_readFile",
                                        "input": {"description": "Reading"}}
        assert n({"_kind": "unknown"}) is None
        assert list(vscode.discover(locations=[str(tmp_path)]))

    def test_real_empty_window_session(self, tmp_path):
        # Sessions without a workspace live directly under
        # globalStorage/emptyWindowChatSessions/ -- no surrounding hash dir
        # or workspace.json, so workspace_path must resolve to None.
        gs = tmp_path / "globalStorage" / "emptyWindowChatSessions"
        gs.mkdir(parents=True)
        shutil.copy(JSON_CASE, gs / "ew.json")
        shutil.copy(JSONL_EMPTY_WINDOW, gs / "ew.jsonl")

        conv_json = list(vscode.parse(S(kind="file", location=gs / "ew.json")))[0]
        conv_jsonl = list(vscode.parse(S(kind="file", location=gs / "ew.jsonl")))[0]
        assert conv_json.workspace_path is None and conv_jsonl.workspace_path is None
        assert conv_json.harness.name == "vscode" and len(conv_json.prompts) == 2
        assert len(conv_jsonl.prompts) == 1 and _text_blocks(conv_jsonl)

        # discover() should pick up empty-window files when the parent
        # (analogue of .../User/globalStorage) is passed as a location.
        sources = list(vscode.discover(locations=[str(tmp_path / "globalStorage")]))
        assert {Path(s.location).name for s in sources} == {"ew.json", "ew.jsonl"}

    def test_whole_request_patches_keep_raw_parts(self):
        conv = list(vscode.parse(S(kind="file", location=JSONL_WHOLE_REQUEST)))[0]
        kinds = [b.block_type for p in conv.prompts for r in p.responses for b in r.content]
        assert "mcpServersStarting" in kinds and "progressMessage" in kinds and "text" in kinds

    def test_replay_and_parse_errors(self, tmp_path):
        cs = tmp_path / "chatSessions"
        cs.mkdir(parents=True)
        sess = {"version": 3, "sessionId": "j", "creationDate": 1708012345678, "requests": [
            {"requestId": "r1", "message": {"text": "Hi"}, "timestamp": 1708012345678,
             "modelId": "m", "response": [{"value": "ok"}],
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
