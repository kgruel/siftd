"""Tests for VSCode adapter.

Inputs are redacted captures of real VS Code chat sessions (schema version 3,
the top-level "version" field) under
tests/fixtures/adapters/vscode/ (see tests/test_fixture_redaction.py). Earlier
hand-written fixtures used response parts VS Code never writes (markdownContent,
toolName/input/result), so the adapter dropped every answer and named every tool
"unknown" while the suite stayed green. Keep new cases to captured shapes.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR

from siftd.adapters import vscode
from siftd.adapters.assembly import assemble, decode
from siftd.adapters.records import Disposition, Ignored, Unknown
from siftd.adapters.sdk import AdapterParseError
from siftd.domain import Usage
from siftd.domain.source import Source
from siftd.safecall import epoch_ms_to_iso

S = Source

GOLDEN = FIXTURES_DIR / "adapters" / "vscode"
JSON_CASE = GOLDEN / "json_format" / "input.json"
JSONL_CASE = GOLDEN / "jsonl_tool_calls" / "input.jsonl"
JSONL_EMPTY_WINDOW = GOLDEN / "jsonl_empty_window" / "input.jsonl"
JSONL_WHOLE_REQUEST = GOLDEN / "jsonl_whole_request" / "input.jsonl"
JSONL_USAGE = GOLDEN / "jsonl_usage" / "input.jsonl"

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


def _assembly(path: Path):
    """The assembled source and its ledger, as parse() sees them."""
    return assemble(
        Source(kind="file", location=path),
        read=vscode.read_strict,
        decoder=vscode.VSCodeDecoder,
        harness=vscode.HARNESS,
        default_session=vscode._default_session(path),
    )


def _input_of(case: str) -> Path:
    return next(p for p in (GOLDEN / case).iterdir() if p.name != "expected.json")


def _request(*parts, request_id="r1", response_id="resp1", timestamp=1771376214522, **extra) -> dict:
    """A request as VS Code writes it, with only the keys the decoder reads."""
    return {"requestId": request_id, "responseId": response_id, "timestamp": timestamp,
            "message": {"text": "hi"}, "modelId": "m", "response": list(parts), **extra}


def _invocation(call_id: str, text: str = "Reading", **state) -> dict:
    return {"kind": "toolInvocationSerialized", "toolCallId": call_id, "toolId": "copilot_readFile",
            "invocationMessage": {"value": text}, "isConfirmed": {"type": 1}, **state}


def _session_json(tmp_path, requests, *, creation=1771376214522, name="s.json") -> Path:
    """A whole-file session with the given requests, in a chatSessions dir."""
    cs = tmp_path / "h" / "chatSessions"
    cs.mkdir(parents=True, exist_ok=True)
    data = {"version": 3, "sessionId": "s", "requests": requests}
    if creation is not None:
        data["creationDate"] = creation
    path = cs / name
    path.write_text(json.dumps(data))
    return path


# One example per row of the decoder's table. The test below checks each row's
# "Becomes" text against what its example decodes to.
EXAMPLES = {
    "session": vscode.SessionUnit(session={"sessionId": "s", "creationDate": 1771376214522}),
    "request": vscode.RequestUnit(request=_request()),
    "part:text": vscode.RequestUnit(request=_request({"value": "answer"})),
    "part:thinking": vscode.RequestUnit(request=_request({"kind": "thinking", "value": "reasoning", "id": "t"})),
    "part:thinking:marker": vscode.RequestUnit(request=_request({"kind": "thinking", "value": "", "id": "t"})),
    "part:toolInvocationSerialized": vscode.RequestUnit(request=_request(_invocation("c1", isComplete=True))),
    "part:inlineReference": vscode.RequestUnit(request=_request({"kind": "inlineReference", "inlineReference": {}})),
    "part:mcpServersStarting": vscode.RequestUnit(request=_request({"kind": "mcpServersStarting",
                                                                   "didStartServerIds": []})),
    "part:progressMessage": vscode.RequestUnit(request=_request({"kind": "progressMessage",
                                                                "content": {"value": "Getting ready"}})),
    "part:warning": vscode.RequestUnit(request=_request({"kind": "warning", "content": {"value": "slow"}})),
    "part:command": vscode.RequestUnit(request=_request({"kind": "command", "command": {"id": "x"}})),
    "part:other": vscode.RequestUnit(request=_request({"kind": "textEditGroup", "uri": {}, "edits": []})),
    "part:no-kind": vscode.RequestUnit(request=_request({"value": 5})),
    "part:not-an-object": vscode.RequestUnit(request=_request("stray")),
    "tool-invocation:no-toolCallId": vscode.RequestUnit(request=_request(
        {"kind": "toolInvocationSerialized", "toolId": "t", "isComplete": True})),
    "request:not-an-object": vscode.RequestUnit(request="stray"),
    "patch:unapplied": vscode.PatchUnit(reason="patch:path-missing"),
}


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

    @pytest.mark.parametrize("part, status, result", [
        ({"isComplete": False}, "pending", None),
        ({"isComplete": True}, "success", {}),
        ({"isComplete": True, "resultDetails": [{"uri": {}}]}, "success", {"resultDetails": [{"uri": {}}]}),
        ({"isComplete": True, "resultDetails": {"isError": True, "output": []}}, "error",
         {"resultDetails": {"isError": True, "output": []}}),
    ])
    def test_tool_status_and_result_from_real_fields(self, tmp_path, part, status, result):
        path = _session_json(tmp_path, [_request(_invocation("c", **part))])
        (call,) = _tool_calls(list(vscode.parse(S(kind="file", location=path)))[0])
        assert (call.status, call.result) == (status, result)

    def test_jsonl_and_json_of_the_same_session_parse_identically(self, tmp_path):
        from_jsonl = list(vscode.parse(_session_file(tmp_path / "a", JSONL_CASE, "same.jsonl")))
        from_json = list(vscode.parse(_session_file(tmp_path / "b", JSON_CASE, "same.json")))
        assert from_jsonl == from_json and from_jsonl

    def test_peek_projects_the_conversation_ingest_stores(self):
        conv = list(vscode.parse(S(kind="file", location=JSONL_CASE)))[0]
        exchanges = vscode.peek_exchanges(JSONL_CASE, last_n=50)
        assert vscode.peek_scan(JSONL_CASE).exchange_count == len(conv.prompts) == len(exchanges)
        narrative = [b for ex in exchanges for b in ex.narrative]
        assert sum(1 for b in narrative if b.block_type == "text") > 0
        names = {tc.tool_name for b in narrative if b.block_type == "tool_calls" for tc in b.tool_calls}
        # Aliased names are canonical in peek; an unaliased name is shown as written.
        assert {"file.read", "search.grep", "shell.execute", "configure_python_environment"} == names
        assert len([tc for b in narrative if b.block_type == "tool_calls" for tc in b.tool_calls]) == len(_tool_calls(conv))

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

    def test_peek_discover_and_unparseable_file(self, tmp_path):
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


class TestDecoding:
    def test_every_unit_and_part_kind_is_named_and_the_table_matches_the_code(self):
        """The decoder's docstring table is the ledger of unit and part kinds; it must not drift from the code."""
        rows = {}
        for line in vscode.VSCodeDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows[re.match(r"`([^`]+)`", cells[0]).group(1)] = cells[1]
        folded = {f"patch:{k}" for k in vscode._FOLDED_PATCH_KINDS}
        assert {"part:text", "part:thinking", "part:thinking:marker", "part:toolInvocationSerialized",
                "part:inlineReference", "part:mcpServersStarting", "part:progressMessage",
                "part:warning", "part:command"} <= set(rows)
        assert set(rows) == set(EXAMPLES) | folded

        decoder = vscode.VSCodeDecoder(Source(kind="file", location=Path("/x/s.json")))
        for name, unit in EXAMPLES.items():
            becomes = rows[name]
            records = decoder.decode(unit)
            classes = {type(r).__name__ for r in records}
            reasons = {r.reason for r in records if isinstance(r, Ignored)}
            if "Session" in becomes:
                assert "Session" in classes, name
            if "PromptRec" in becomes:
                assert "PromptRec" in classes, name
            if "ToolCallRec" in becomes:
                assert "ToolCallRec" in classes, name
            if becomes.startswith("content"):
                (response,) = [r for r in records if type(r).__name__ == "ResponseRec"]
                assert response.content, name
            assert ("Ignored" in becomes) == bool(reasons), name
            for named in re.findall(r"Ignored \(`([^`]+)`\)", becomes):
                assert named in reasons, name
            assert ("Unknown" in becomes) == any(isinstance(r, Unknown) for r in records), name

    def test_thinking_text_is_a_thinking_block_and_empty_markers_are_ignored_by_name(self):
        conv = list(vscode.parse(S(kind="file", location=JSONL_CASE)))[0]
        blocks = [b for p in conv.prompts for r in p.responses for b in r.content if b.block_type == "thinking"]
        assert len(blocks) == 23 and all(b.content["text"] for b in blocks)
        assert _assembly(JSONL_CASE).ledger.reasons[(Disposition.IGNORED, "thinking:marker")] == 41

    def test_usage_is_the_requests_total_from_result_usage(self):
        (conv,) = vscode.parse(S(kind="file", location=JSONL_USAGE))
        assert conv.prompts[0].responses[0].usage == Usage(input_tokens=12302, output_tokens=1739)
        # A request whose result carries no usage has none, rather than zero.
        (plain,) = vscode.parse(S(kind="file", location=JSONL_CASE))
        assert all(r.usage is None for p in plain.prompts for r in p.responses)

    def test_a_re_emitted_invocation_is_one_call_in_its_last_state(self, tmp_path):
        running = _invocation("c1", "Reading")
        done = {**running, "isComplete": True, "invocationMessage": {"value": "Read file"},
                "resultDetails": [{"uri": {}}]}
        path = _session_json(tmp_path, [_request(running, {"value": "text"}, done)])
        conv = list(vscode.parse(S(kind="file", location=path)))[0]
        (call,) = _tool_calls(conv)
        assert call.input == {"description": "Read file"}
        assert (call.status, call.result) == ("success", {"resultDetails": [{"uri": {}}]})
        ledger = _assembly(path).ledger
        assert ledger.count(Disposition.ORPHAN) == 0 and ledger.count(Disposition.UNKNOWN) == 0
        assert ledger.dispositions[("ToolCallRec", Disposition.EVENT)] == 1
        assert ledger.dispositions[("ToolResultRec", Disposition.PATCH)] == 1

    def test_a_pending_invocation_has_no_result_and_a_pending_restatement_is_named(self, tmp_path):
        path = _session_json(tmp_path, [_request(_invocation("c1"), _invocation("c1"))])
        (call,) = _tool_calls(list(vscode.parse(S(kind="file", location=path)))[0])
        assert (call.status, call.result) == ("pending", None)
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.IGNORED, "tool-invocation:pending-restated")] == 1

    def test_a_pending_emission_after_a_settled_one_reverts_the_call_to_pending(self, tmp_path):
        # The last emission is the state the call is left in, even when an earlier one settled it.
        settled = _invocation("c1", isComplete=True, resultDetails=[{"uri": {}}])
        path = _session_json(tmp_path, [_request(settled, _invocation("c1"))])
        (call,) = _tool_calls(list(vscode.parse(S(kind="file", location=path)))[0])
        assert (call.status, call.result) == ("pending", {})
        ledger = _assembly(path).ledger
        assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0
        assert ledger.dispositions[("ToolResultRec", Disposition.PATCH)] == 2

    def test_a_call_repeated_in_a_later_request_is_one_call_completed_there(self, tmp_path):
        first = _request(_invocation("c1", "Reading"), request_id="r1", response_id="resp1",
                         timestamp=1771376214522)
        second = _request(_invocation("c1", "Read file", isComplete=True), request_id="r2", response_id="resp2",
                          timestamp=1771376314522)
        path = _session_json(tmp_path, [first, second])
        conv = list(vscode.parse(S(kind="file", location=path)))[0]
        assert [len(r.tool_calls) for p in conv.prompts for r in p.responses] == [1, 0]
        (call,) = _tool_calls(conv)
        assert (call.status, call.input) == ("success", {"description": "Read file"})
        assert _assembly(path).ledger.count(Disposition.ORPHAN) == 0

    def test_a_call_restated_in_later_requests_keeps_its_last_input_and_final_state(self, tmp_path):
        # Request 1 settles the call, request 2 restates it with other input, request 3
        # settles it again as an error. Pairing is per call, so the last result is the error.
        error = {"isError": True, "output": []}
        first = _request(_invocation("c1", "Reading a", isComplete=True, resultDetails=[]),
                         request_id="r1", response_id="resp1", timestamp=1771376214522)
        second = _request(_invocation("c1", "Reading b", isComplete=True, resultDetails=[]),
                          request_id="r2", response_id="resp2", timestamp=1771376314522)
        third = _request(_invocation("c1", "Reading b", isComplete=True, resultDetails=error),
                         request_id="r3", response_id="resp3", timestamp=1771376414522)
        path = _session_json(tmp_path, [first, second, third])
        conv = list(vscode.parse(S(kind="file", location=path)))[0]
        assert [len(r.tool_calls) for p in conv.prompts for r in p.responses] == [1, 0, 0]
        (call,) = _tool_calls(conv)
        assert (call.input, call.status, call.result) == ({"description": "Reading b"}, "error",
                                                          {"resultDetails": error})
        ledger = _assembly(path).ledger
        assert ledger.count(Disposition.ORPHAN) == 0 and ledger.count(Disposition.UNKNOWN) == 0
        assert ledger.dispositions[("ToolCallRec", Disposition.EVENT)] == 1
        assert ledger.dispositions[("ToolResultRec", Disposition.PATCH)] == 3

    def test_a_part_whose_kind_is_not_a_string_is_unkeyed_and_counted(self):
        decoder = vscode.VSCodeDecoder(Source(kind="file", location=Path("/x/s.json")))
        records = decoder.decode(vscode.RequestUnit(request=_request({"kind": ["not", "a", "string"], "value": 1})))
        (response,) = [r for r in records if type(r).__name__ == "ResponseRec"]
        assert [b.block_type for b in response.content] == ["unkeyed"]
        assert [r.reason for r in records if isinstance(r, Unknown)] == ["part:no-kind"]

    def test_a_request_or_part_that_is_not_an_object_is_counted_not_fatal(self, tmp_path):
        path = _session_json(tmp_path, ["a stray string", _request("a stray part", {"value": "answer"})])
        (conv,) = vscode.parse(S(kind="file", location=path))
        assert [b.content["text"] for b in _text_blocks(conv)] == ["answer"]
        reasons = _assembly(path).ledger.reasons
        assert reasons[(Disposition.UNKNOWN, "request:not-an-object")] == 1
        assert reasons[(Disposition.UNKNOWN, "part:not-an-object")] == 1

    def test_patches_that_cannot_apply_are_counted_by_reason(self, tmp_path):
        header = {"version": 3, "sessionId": "j", "creationDate": 1771376214522, "requests": []}
        lines = [
            {"kind": 1, "k": ["x"], "v": 1},  # before the root
            {"kind": 0, "v": header},
            {"kind": 2, "k": ["requests"], "v": [_request("ok")]},
            {"kind": 3, "k": ["requests"], "v": 1},  # unknown kind
            {"kind": 1, "k": ["requests", 9, "result"], "v": {}},  # no such request
            {"kind": 2, "k": [], "v": []},  # no key path
        ]
        path = tmp_path / "h" / "chatSessions" / "j.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        (conv,) = vscode.parse(S(kind="file", location=path))
        assert len(conv.prompts) == 1
        reasons = _assembly(path).ledger.reasons
        for reason in ("patch:before-root", "patch:kind-3", "patch:path-missing", "patch:no-path"):
            assert reasons[(Disposition.UNKNOWN, reason)] == 1, reason

    def test_a_session_without_creation_date_starts_at_its_latest_request(self, tmp_path):
        # The adapter has always dated such a session by its latest request (not the earliest).
        path = _session_json(tmp_path, [_request(timestamp=1771376214522), _request(
            request_id="r2", response_id="resp2", timestamp=1771376314522)], creation=None)
        conv = list(vscode.parse(S(kind="file", location=path)))[0]
        assert conv.started_at == epoch_ms_to_iso(1771376314522)
        assert conv.ended_at == epoch_ms_to_iso(1771376314522)

    @pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir()))
    def test_fixtures_decode_with_nothing_unknown_or_orphaned(self, case):
        ledger = _assembly(_input_of(case)).ledger
        assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0

    def test_json_and_jsonl_forms_of_one_session_decode_to_the_same_records(self, tmp_path):
        state, unapplied, malformed = vscode._replay(JSONL_CASE, strict=True)
        assert unapplied == [] and malformed == []
        jsonl = _session_file(tmp_path / "a", JSONL_CASE, "same.jsonl")
        whole = tmp_path / "b" / "chatSessions" / "same.json"
        whole.parent.mkdir(parents=True)
        whole.write_text(json.dumps(state))
        (whole.parent.parent / "workspace.json").write_text(json.dumps({"folder": "file:///test/workspace"}))
        records = {}
        for label, source in (("jsonl", jsonl), ("json", S(kind="file", location=whole))):
            decoder = vscode.VSCodeDecoder(source)
            records[label] = [rec for _, rec in decode(vscode.read_strict(source), decoder)]
        assert records["jsonl"] == records["json"] and records["json"]

    def test_a_truncated_patch_line_fails_ingest_and_still_peeks(self, tmp_path):
        lines = JSONL_CASE.read_text(encoding="utf-8").splitlines()
        cs = tmp_path / "h" / "chatSessions"
        cs.mkdir(parents=True)
        path = cs / "t.jsonl"
        path.write_text("\n".join(lines[:-1] + [lines[-1][: len(lines[-1]) // 2]]) + "\n")
        with pytest.raises(AdapterParseError, match="invalid JSONL"):
            list(vscode.parse(S(kind="file", location=path)))
        assert vscode.peek_scan(path).exchange_count >= 1
