"""Tests for Copilot CLI adapter."""

import json
import re
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR, default_location_source, fixture_source

from siftd.adapters import copilot_cli
from siftd.adapters.assembly import AssemblyError
from siftd.adapters.contract import adapt
from siftd.adapters.records import Disposition, Ignored, Meta, Unknown
from siftd.domain.source import Source

GOLDEN = FIXTURES_DIR / "adapters" / "copilot_cli"
# The spawning call of the one real sub-agent in the local corpus (a `task` call).
SPAWN_CALL = "toolu_01Xj5hydeGkf1X1sZDqEPmix"


def _assembly(path):
    """The assembled source and its ledger, as parse() sees them."""
    return adapt(copilot_cli).assemble(Source(kind="file", location=path))


def _only(path):
    (conversation,) = adapt(copilot_cli).parse(Source(kind="file", location=path))
    return conversation


def _event(kind, at, data=None, **extra):
    return {"type": kind, "timestamp": at, "data": {} if data is None else data, **extra}


def _write(tmp_path, events, dirname="session-1"):
    """Write events as the events.jsonl of a session directory, return its path."""
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    path = d / "events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _start(sid="s1", cwd="/w", branch="main", at="T0"):
    return _event("session.start", at, {"sessionId": sid, "context": {"cwd": cwd, "branch": branch}})


def _request(call_id, name="bash", **arguments):
    return {"toolCallId": call_id, "name": name, "arguments": arguments or {"command": "ls"}}


def _calls(conversation):
    responses = list(conversation.preamble) + [r for p in conversation.prompts for r in p.responses]
    return [c for r in responses for c in r.tool_calls]


class TestCopilotCliAdapter:

    def test_can_handle(self):
        assert copilot_cli.can_handle(Source(kind="file", location=Path("/mock/.copilot/session-state/uuid/events.jsonl")))
        assert not copilot_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "copilot_cli_minimal.jsonl"))
        assert not copilot_cli.can_handle(Source(kind="directory", location=Path("/mock/.copilot/session-state")))
        assert copilot_cli.can_handle(default_location_source(copilot_cli, "uuid/events.jsonl"))

    def test_parse_full(self, tmp_path):
        copilot_source = fixture_source(tmp_path, "copilot_cli_minimal.jsonl", ".copilot/session-state/test-uuid", "events.jsonl")
        conv = list(adapt(copilot_cli).parse(copilot_source))[0]
        assert conv.external_id == "copilot_cli::copilot-session-001" and conv.workspace_path == "/test/workspace"
        assert conv.branch == "main" and conv.harness.name == "copilot_cli" and len(conv.prompts) == 1
        assert "List the files" in conv.prompts[0].content[0].content["text"] and len(conv.prompts[0].responses) == 2
        tc = conv.prompts[0].responses[0].tool_calls[0]
        assert tc.tool_name == "bash" and tc.status == "success" and "README.md" in str(tc.result)
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "thinking"]
        assert conv.prompts[0].responses[0].model == "claude-haiku-4.5"

    def test_parse_empty_file(self, tmp_path):
        d = tmp_path / ".copilot" / "session-state" / "uuid"
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("")
        assert list(adapt(copilot_cli).parse(Source(kind="file", location=d / "events.jsonl"))) == []

    def test_discover_wrapper_and_can_handle_fallback_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(copilot_cli, "discover_files", lambda *a, **k: [Source(kind="file", location=tmp_path / "x")])
        assert list(copilot_cli.discover(locations=[str(tmp_path)]))
        assert not copilot_cli.can_handle(Source(kind="file", location=tmp_path / "events.jsonl"))

    def test_parse_updates_started_at_when_timestamps_unordered(self, tmp_path):
        d = tmp_path / ".copilot" / "session-state" / "uuid"
        d.mkdir(parents=True)
        p = d / "events.jsonl"
        p.write_text("\n".join([
            '{"type":"assistant.message","timestamp":"2024-01-03T00:00:00Z","data":{"content":"later"}}',
            '{"type":"user.message","timestamp":"2024-01-02T00:00:00Z","data":{"content":"earlier"}}'
        ]))
        conv = list(adapt(copilot_cli).parse(Source(kind="file", location=p)))[0]
        assert conv.started_at == "2024-01-02T00:00:00Z"

    def test_windows_default_locations_branch_reload(self, monkeypatch):
        import importlib

        import siftd.adapters.copilot_cli as mod

        monkeypatch.setattr(mod.sys, "platform", "win32")
        mod = importlib.reload(mod)
        assert any("AppData/Local/.copilot/session-state" in p for p in mod.DEFAULT_LOCATIONS)


GOLDEN_CASES = sorted(p.name for p in GOLDEN.iterdir() if p.is_dir())


class TestDecoding:
    def test_every_event_type_is_named_and_the_table_matches_the_code(self):
        """The decoder's docstring table is the event-type ledger; it must not drift from what the code does."""
        rows = []
        for line in copilot_cli.CopilotDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((re.match(r"`([^`]+)`", cells[0]).group(1), cells[1]))
        assert len(rows) == 11
        ignored = {name for name, becomes in rows if becomes.startswith("Ignored")}
        assert ignored == copilot_cli._IGNORED_TYPES
        decoder = copilot_cli.CopilotDecoder(Source(kind="file", location=Path("/x/s/events.jsonl")))
        samples = {
            "tool.execution_complete": {"toolCallId": "c1", "success": True, "result": {"content": "x"}},
            "assistant.message": {"messageId": "m1", "content": "x"},
            "user.message": {"content": "x"},
            "session.model_change": {"newModel": "m"},
        }
        for name, _ in rows:
            records = decoder.decode(_event(name, "T", samples.get(name)))
            assert records and not any(type(r).__name__ == "Unknown" for r in records), name

    def test_an_unrecognized_type_is_unknown_and_counted(self, tmp_path):
        path = _write(tmp_path, [_start(), _event("user.message", "T1", {"content": "hi"}),
                                 _event("a.future.event", "T2", {})])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "type:a.future.event")] == 1
        assert ledger.disposed == ledger.records

    def test_a_null_data_field_is_an_empty_event_not_a_crash(self, tmp_path):
        """parse() raised AttributeError on `data: null`, and the whole file failed. A null payload is
        an event with no fields: a user message with no text is still a prompt, and the file still parses."""
        path = _write(tmp_path, [
            _start(),
            {"type": "user.message", "timestamp": "T1", "data": None},
            {"type": "assistant.turn_end", "timestamp": "T2", "data": None},
        ])
        ledger = _assembly(path).ledger
        assert ledger.count(Disposition.UNKNOWN) == 0
        assert len(_only(path).prompts) == 1

    def test_a_tool_request_without_an_id_is_still_a_call(self, tmp_path):
        """A request with no toolCallId was given a tool_use block and no ToolCall: the call vanished from
        the stored tools. It is a call that can never pair with a result, so it stays pending."""
        path = _write(tmp_path, [_start(), _event("user.message", "T1", {"content": "hi"}),
                                 _event("assistant.message", "T2", {"messageId": "m1", "content": "",
                                                                    "toolRequests": [{"name": "bash", "arguments": {}}]})])
        (call,) = _calls(_only(path))
        assert call.tool_name == "bash" and call.external_id is None and call.status == "pending"

    def test_a_result_with_no_call_is_an_orphan_and_counted(self, tmp_path):
        """A completion whose call never appears was silently skipped. It is named in the ledger as an orphan."""
        path = _write(tmp_path, [_start(), _event("user.message", "T1", {"content": "hi"}),
                                 _event("tool.execution_complete", "T2", {"toolCallId": "ghost", "success": True})])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.ORPHAN, "tool result with no call")] == 1
        assert _calls(_only(path)) == []

    def test_a_completion_with_no_call_id_is_unknown(self, tmp_path):
        path = _write(tmp_path, [_start(), _event("user.message", "T1", {"content": "hi"}),
                                 _event("tool.execution_complete", "T2", {"success": True})])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "tool.execution_complete:no-toolCallId")] == 1

    def test_a_header_only_file_yields_no_conversation(self, tmp_path):
        """A file with a session header and no messages was yielded as a conversation with zero events,
        stored as a hollow row. No conversation without an event is yielded."""
        path = _write(tmp_path, [_start(), _event("session.model_change", "T1", {"newModel": "m"})])
        assert list(adapt(copilot_cli).parse(Source(kind="file", location=path))) == []

    @pytest.mark.parametrize("case", GOLDEN_CASES)
    def test_fixtures_decode_with_nothing_unknown_or_orphaned(self, case):
        ledger = _assembly(GOLDEN / case / "input.jsonl").ledger
        assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0

    def test_an_undated_file_is_an_error_not_a_wall_clock_stamp(self, tmp_path):
        path = _write(tmp_path, [{"type": "user.message", "data": {"content": "hi"}}])
        with pytest.raises(AssemblyError, match="no timestamp"):
            adapt(copilot_cli).parse(Source(kind="file", location=path))


class TestAssembledShape:
    """What the shared assembler does with Copilot CLI's events, on captured shapes and on the edges."""

    def test_sub_agent_turns_are_responses_tagged_with_the_call_that_spawned_them(self):
        """The real session's sub-agent (a `task` call) wrote 31 of its 34 messages inside one prompt. They are
        separate responses: each keeps its own tool calls, paired with their results, and names the spawning call.
        The data supports that link (parentToolCallId), not a separate conversation: the sub-agent's events share
        one events.jsonl, so there is no lineage record."""
        path = GOLDEN / "subagent" / "input.jsonl"
        assembly = _assembly(path)
        (conversation,) = assembly.conversations
        (prompt,) = conversation.prompts
        responses = prompt.responses
        assert len(responses) == 34 and conversation.preamble == []
        tagged = [r for r in responses if "parent_tool_call_id" in r.attributes]
        assert len(tagged) == 31 and {r.attributes["parent_tool_call_id"] for r in tagged} == {SPAWN_CALL}
        assert "parent_tool_call_id" not in responses[0].attributes and responses[-1].attributes == {}
        assert all(c.status == "success" and c.result for c in _calls(conversation))
        assert "lineage.kind" not in conversation.attributes
        assert assembly.ledger.count(Disposition.UNKNOWN) == 0 and assembly.ledger.count(Disposition.ORPHAN) == 0

    def test_an_assistant_message_before_any_prompt_is_kept_in_the_preamble(self, tmp_path):
        """A response that arrived before the first prompt was attached to nothing and its tool calls vanished."""
        path = _write(tmp_path, [_start(), _event("assistant.message", "T1", {
            "messageId": "m0", "content": "early", "toolRequests": [_request("c0")]}),
            _event("tool.execution_complete", "T2", {"toolCallId": "c0", "success": True, "result": {"content": "ok"}}),
            _event("user.message", "T3", {"content": "hi"})])
        conversation = _only(path)
        assert len(conversation.preamble) == 1 and [c.external_id for c in conversation.preamble[0].tool_calls] == ["c0"]
        assert len(conversation.prompts) == 1

    def test_a_model_change_applies_to_the_responses_after_it(self, tmp_path):
        """The first model change was applied to every response in the file, including those before it.
        A later change now replaces it, for the responses after that change."""
        path = _write(tmp_path, [
            _start(),
            _event("user.message", "T1", {"content": "one"}),
            _event("assistant.message", "T2", {"messageId": "m1", "content": "a"}),
            _event("session.model_change", "T3", {"newModel": "model-a"}),
            _event("assistant.message", "T4", {"messageId": "m2", "content": "b"}),
            _event("session.model_change", "T5", {"newModel": "model-b"}),
            _event("assistant.message", "T6", {"messageId": "m3", "content": "c"}),
        ])
        models = [r.model for r in _only(path).prompts[0].responses]
        assert models == [None, "model-a", "model-b"]

    def test_tool_calls_keep_the_order_the_model_requested_them_in(self, tmp_path):
        """Calls were listed in the order their results arrived. The model requested them in one message,
        and that is the order the response's content gives them."""
        path = _write(tmp_path, [
            _start(),
            _event("user.message", "T1", {"content": "go"}),
            _event("assistant.message", "T2", {"messageId": "m1", "content": "",
                                               "toolRequests": [_request("first"), _request("second")]}),
            _event("tool.execution_complete", "T3", {"toolCallId": "second", "success": True, "result": {"content": "b"}}),
            _event("tool.execution_complete", "T4", {"toolCallId": "first", "success": True, "result": {"content": "a"}}),
        ])
        (response,) = _only(path).prompts[0].responses
        assert [c.external_id for c in response.tool_calls] == ["first", "second"]
        assert [c.timestamp for c in response.tool_calls] == ["T4", "T3"]

    def test_a_call_that_never_completes_stays_pending_with_no_result(self, tmp_path):
        path = _write(tmp_path, [_start(), _event("user.message", "T1", {"content": "go"}),
                                 _event("assistant.message", "T2", {"messageId": "m1", "content": "",
                                                                    "toolRequests": [_request("c1")]})])
        (call,) = _calls(_only(path))
        assert call.status == "pending" and call.result is None

    def test_identity_is_the_last_header_and_the_directory_when_none_states_one(self, tmp_path):
        """A file with several session headers was named by the last one, and a file with none by its directory."""
        path = _write(tmp_path, [_start("first"), _start("second"), _event("user.message", "T1", {"content": "x"})],
                      dirname="dir-name")
        assert _only(path).external_id == "copilot_cli::second"
        path = _write(tmp_path, [_event("user.message", "T1", {"content": "x"})], dirname="other-dir")
        assert _only(path).external_id == "copilot_cli::other-dir"

    def test_bounds_span_every_timestamped_event_as_before_the_port(self, tmp_path):
        """The conversation spans its earliest to its latest timestamp, notices and turn markers included, as
        the pre-port parser did. Dropping the markers' times would shorten the span and move date filters."""
        path = _write(tmp_path, [
            _start(at="2024-01-01T00:00:00Z"),
            _event("session.info", "2023-12-31T00:00:00Z", {"infoType": "mcp"}),
            _event("user.message", "2024-01-01T00:00:05Z", {"content": "x"}),
            _event("assistant.message", "2024-01-01T00:00:09Z", {"messageId": "m", "content": "y"}),
            _event("assistant.turn_end", "2024-01-01T00:00:30Z", {"turnId": "0"}),
        ])
        conversation = _only(path)
        assert conversation.started_at == "2023-12-31T00:00:00Z"
        assert conversation.ended_at == "2024-01-01T00:00:30Z"

    def test_an_event_with_no_content_still_states_its_time(self, tmp_path):
        """Ignored and Unknown records carry no timestamp of their own. A timed unit whose records are all of
        that kind is stated as a Meta at its time, so the assembler sees the time as evidence."""
        path = _write(tmp_path, [_start(at="T0"), _event("user.message", "T1", {"content": "x"})])
        decoder = copilot_cli.CopilotDecoder(Source(kind="file", location=path))
        notice = decoder.decode(_event("session.info", "T9", {"infoType": "mcp"}))
        assert [(type(r), getattr(r, "at", None)) for r in notice] == [(Meta, "T9"), (Ignored, None)]
        future = decoder.decode(_event("some.future_event", "T7", {}))
        assert [(type(r), getattr(r, "at", None)) for r in future] == [(Meta, "T7"), (Unknown, None)]
        # Every timed no-content event is stated the same way, whatever its type.
        assert [type(r) for r in decoder.decode(_event("assistant.turn_end", "T8", {}))] == [Meta, Ignored]

    def test_responses_carry_no_usage_because_the_format_records_none(self):
        responses = _only(GOLDEN / "subagent" / "input.jsonl").prompts[0].responses
        assert all(r.usage is None for r in responses)
        assert all(not any(k.endswith("_tokens") for k in r.attributes) for r in responses)


class TestPeek:
    def test_peek_scan_and_exchanges_agree_with_parse_on_the_real_capture(self):
        path = GOLDEN / "subagent" / "input.jsonl"
        scan = adapt(copilot_cli).peek_scan(path)
        exchanges = adapt(copilot_cli).peek_exchanges(path, 5)
        assert scan.exchange_count == len(exchanges) == 1
        assert scan.model == "claude-haiku-4.5"
        assert sum(count for _, count in exchanges[0].tool_calls) == 36

    def test_peek_reads_the_same_decoder_as_ingest(self, tmp_path):
        path = _write(tmp_path, [_start(), _event("user.message", "T1", {"content": "hi"}),
                                 _event("assistant.message", "T2", {"messageId": "m", "content": "ok"})])
        assert adapt(copilot_cli).peek_scan(path).exchange_count == len(_only(path).prompts) == 1
