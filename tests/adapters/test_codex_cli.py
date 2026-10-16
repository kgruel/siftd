"""Tests for the Codex CLI adapter.

Each test names one decision the decoder or the shared assembler makes for Codex
records, and the defect it replaces. The golden cases under
tests/fixtures/adapters/codex_cli/ are redacted captures of real rollouts; the
records built inline here are shaped like those captures.
"""

import json
import re
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR, CodexSession, default_location_source, fixture_source, write_jsonl

from siftd.adapters import codex_cli
from siftd.adapters.contract import adapt
from siftd.adapters.records import Disposition, ResponseRec, ToolCallRec
from siftd.domain.source import Source

GOLDEN = FIXTURES_DIR / "adapters" / "codex_cli"


def _assembly(path):
    """The assembled source and its ledger, as parse() sees them."""
    return adapt(codex_cli).assemble(Source(kind="file", location=path))


def _only(path):
    (conversation,) = adapt(codex_cli).parse(Source(kind="file", location=path))
    return conversation


def _responses(conversation):
    return list(conversation.preamble) + [r for p in conversation.prompts for r in p.responses]


def _calls(conversation):
    return [c for r in _responses(conversation) for c in r.tool_calls]


def _meta(ident="s1", ts="T0", **payload):
    return {"type": "session_meta", "timestamp": ts, "payload": {"id": ident, "cwd": "/p", **payload}}


def _user(ts="T1", text="go"):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def _assistant(ts, text="ok"):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}


def _call(ts, call_id, name="shell_command"):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "function_call", "name": name, "call_id": call_id, "arguments": "{}"}}


def _output(ts, call_id, output="done"):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "function_call_output", "call_id": call_id, "output": output}}


def _turn(ts, model="gpt-5"):
    return {"type": "turn_context", "timestamp": ts, "payload": {"model": model}}


def _token_count(ts, *, inp, cached, out, reasoning, total_in, total=True):
    """A token_count event. total_in is the cumulative input after this call."""
    info = {"last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                 "output_tokens": out, "reasoning_output_tokens": reasoning}}
    if total:
        info["total_token_usage"] = {"input_tokens": total_in, "cached_input_tokens": cached,
                                     "output_tokens": out, "reasoning_output_tokens": reasoning}
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": info}}


def _task_complete(ts, **durations):
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": "task_complete", "turn_id": "t", **durations}}


def _stored_totals(conv):
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_output_tokens": 0}
    for r in _responses(conv):
        if r.usage:
            totals["input_tokens"] += r.usage.input_tokens or 0
            totals["output_tokens"] += r.usage.output_tokens or 0
        totals["cached_input_tokens"] += int(r.attributes.get("cached_input_tokens", "0"))
        totals["reasoning_output_tokens"] += int(r.attributes.get("reasoning_output_tokens", "0"))
    return totals


def _parse_one(tmp_path, records, name="s.jsonl"):
    return _only(write_jsonl(tmp_path, records, name))


class TestCodexCliAdapter:

    def test_can_handle(self):
        assert codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.jsonl")))
        assert not codex_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl"))
        assert not codex_cli.can_handle(Source(kind="sqlite", location=Path("/mock/sessions/s.jsonl")))
        assert codex_cli.can_handle(default_location_source(codex_cli))

    def test_parse_full(self, tmp_path):
        conv = _only(fixture_source(tmp_path, "codex_cli_minimal.jsonl", "sessions").as_path)
        assert conv.external_id == "codex_cli::codex-session-1" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "codex_cli" and conv.harness.source == "openai"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "Run ls" in prompt.content[0].content["text"] and len(prompt.responses) == 2
        resp = prompt.responses[1]
        tc = resp.tool_calls[0]
        assert tc.tool_name == "shell_command" and tc.status == "success" and "README.md" in str(tc.result)
        assert resp.usage.input_tokens == 120 and resp.usage.output_tokens == 45
        assert resp.attributes.get("cache_read_input_tokens") == "10"

    def test_discover_and_empty_file(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        (d / "s.jsonl").write_text("{}\n")
        assert list(codex_cli.discover(locations=[str(tmp_path)]))
        (d / "empty.jsonl").write_text("")
        assert list(adapt(codex_cli).parse(Source(kind="file", location=d / "empty.jsonl"))) == []

    def test_tool_aliases_cover_exec_and_wait(self):
        assert codex_cli.TOOL_ALIASES["exec"] == "shell.execute"
        assert codex_cli.TOOL_ALIASES["wait"] == "shell.wait"

    def test_custom_and_function_tools_keep_their_raw_names(self, tmp_path):
        path = CodexSession(tmp_path, exchanges=1).with_custom_tools(["my_tool"]).with_tools(["shell"]).build()
        names = {(c.tool_name, c.status) for c in _calls(_only(path))}
        assert names == {("my_tool", "success"), ("shell", "success")}


class TestRecordTable:
    """The decoder's docstring table is the record ledger; it must not drift from the code."""

    @staticmethod
    def _rows():
        rows = []
        for line in codex_cli.CodexDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((re.match(r"`([^`]+)`", cells[0]).group(1), cells[1]))
        return rows

    @staticmethod
    def _unit(name):
        """A representative unit for a table row name (``type`` or ``type/payload[:role]``)."""
        kind, _, rest = name.partition("/")
        if kind == "session_meta":
            return _meta(ts="T")
        if kind == "turn_context":
            return _turn("T")
        if kind != "response_item" and kind != "event_msg":
            return {"type": kind, "timestamp": "T"}
        ptype, _, role = rest.partition(":")
        payload = {"type": ptype, "role": role or None, "call_id": "c", "name": "n", "arguments": "{}",
                   "output": "o", "input": "raw", "content": [{"type": "input_text", "text": "x"}],
                   "summary": [{"type": "summary_text", "text": "t"}]}
        if kind == "event_msg" and ptype == "token_count":
            payload["info"] = {"last_token_usage": {"input_tokens": 1, "output_tokens": 1},
                               "total_token_usage": {"input_tokens": 1}}
        if kind == "event_msg" and ptype == "task_complete":
            payload["duration_ms"] = 5
        return {"type": kind, "timestamp": "T", "payload": payload}

    def test_every_record_type_is_named_and_the_table_matches_the_code(self):
        rows = self._rows()
        names = {name for name, _ in rows}
        assert len(rows) >= 35 and len(names) == len(rows)
        ignored = {name for name, becomes in rows if becomes.startswith("Ignored")}
        top = {n for n in ignored if "/" not in n}
        items = {n.split("/", 1)[1] for n in ignored if n.startswith("response_item/")}
        events = {n.split("/", 1)[1] for n in ignored if n.startswith("event_msg/")}
        assert top == codex_cli._IGNORED_TOP_TYPES
        assert items == set(codex_cli._IGNORED_ITEM_TYPES) | {"message:developer"}
        assert events == codex_cli._IGNORED_EVENT_TYPES

    def test_every_row_decodes_to_named_records_and_nothing_unknown(self):
        decoder = codex_cli.CodexDecoder(Source(kind="file", location=Path("/x/s.jsonl")))
        for name, _ in self._rows():
            records = decoder.decode(self._unit(name))
            assert records and not any(type(r).__name__ == "Unknown" for r in records), name

    def test_an_unrecognized_shape_is_unknown_and_counted(self, tmp_path):
        path = write_jsonl(tmp_path, [
            _meta(), _user(),
            {"type": "a-new-top-type", "timestamp": "T2", "payload": {}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "a-new-item"}},
            {"type": "event_msg", "timestamp": "T4", "payload": {"type": "a-new-event"}},
            {"type": "response_item", "timestamp": "T5", "payload": {"type": "function_call_output", "output": "x"}},
        ])
        with path.open("a") as f:
            f.write("{truncated\n")
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "type:a-new-top-type")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "response_item:a-new-item")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "event_msg:a-new-event")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "response_item/function_call_output:no-call_id")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "malformed")] == 1
        assert ledger.disposed == ledger.records


class TestIdentity:
    def test_a_file_is_named_by_its_first_session_meta_not_its_last(self, tmp_path):
        """A forked child restates its parent's meta after its own; last-wins named the child by the parent's id."""
        records = [_meta("child"), _meta("parent", ts="T0b", source="exec"), _user(), _assistant("T2")]
        conv = _parse_one(tmp_path, records)
        assert conv.external_id == "codex_cli::child"

    def test_the_restated_id_is_superseded_so_ingest_can_adopt_its_old_row(self, tmp_path):
        records = [_meta("child"), _meta("parent"), _user(), _assistant("T2")]
        assert _parse_one(tmp_path, records).superseded_external_ids == ("codex_cli::parent",)

    def test_a_file_with_one_meta_supersedes_nothing(self, tmp_path):
        assert _parse_one(tmp_path, [_meta("only"), _user(), _assistant("T2")]).superseded_external_ids == ()

    def test_files_that_restated_one_parent_keep_distinct_identities(self, tmp_path):
        """Eleven parent ids were each shared by 2-6 files: the collision that kept one child per parent."""
        ids = []
        for n in range(3):
            path = write_jsonl(tmp_path, [_meta(f"child{n}"), _meta("parent"), _user(), _assistant("T2")],
                               f"child{n}.jsonl")
            ids.append(adapt(codex_cli).parse(Source(kind="file", location=path))[0].external_id)
        assert len(set(ids)) == 3

    def test_a_file_with_no_id_is_named_for_its_stem(self, tmp_path):
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"cwd": "/p"}}, _user(), _assistant("T2")]
        assert _parse_one(tmp_path, records, "rollout-x.jsonl").external_id == "codex_cli::rollout-x"

    def test_a_subagent_names_the_parent_it_ran_in(self, tmp_path):
        meta = _meta("child", source={"subagent": {"thread_spawn": {"parent_thread_id": "P1"}}})
        conv = _parse_one(tmp_path, [meta, _user(), _assistant("T2")])
        assert conv.parent("subagent").parent == "codex_cli::P1"

    def test_a_review_subagent_takes_its_parent_from_the_top_level_field(self, tmp_path):
        meta = _meta("child", source={"subagent": "review"}, parent_thread_id="P2")
        conv = _parse_one(tmp_path, [meta, _user(), _assistant("T2")])
        assert conv.parent("subagent").parent == "codex_cli::P2"

    def test_a_fork_names_what_it_continues_and_a_forked_subagent_keeps_both(self, tmp_path):
        meta = _meta("child", forked_from_id="P3",
                     source={"subagent": {"thread_spawn": {"parent_thread_id": "P3"}}})
        conv = _parse_one(tmp_path, [meta, _user(), _assistant("T2")])
        assert conv.parent("subagent").parent == "codex_cli::P3"
        assert conv.parent("fork").parent == "codex_cli::P3"

    def test_a_plain_session_has_no_lineage(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta("s"), _user(), _assistant("T2")])
        assert conv.lineage == ()


class TestResponses:
    def test_each_response_item_is_its_own_response_and_nothing_is_grouped(self, tmp_path):
        """No field says which model call an item belongs to, so reasoning, a call and a message never merge."""
        reasoning = {"type": "response_item", "timestamp": "T3", "payload": {
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "plan"}]}}
        records = [_meta(), _user(), _assistant("T2"), reasoning, _call("T4", "c1"), _output("T5", "c1")]
        conv = _parse_one(tmp_path, records)
        responses = conv.prompts[0].responses
        assert [len(r.tool_calls) for r in responses] == [0, 0, 1]
        assert [b.block_type for b in responses[1].content] == ["thinking"]
        assert [b.block_type for b in responses[2].content] == ["tool_use"]

    def test_two_reasoning_items_around_a_call_stay_two_responses(self, tmp_path):
        """The reviewer's shape: user -> reasoning -> call -> result -> reasoning. The second reasoning is not the first's."""
        def reasoning(ts, item_id, text):
            return {"type": "response_item", "timestamp": ts, "payload": {
                "type": "reasoning", "id": item_id, "summary": [{"type": "summary_text", "text": text}]}}
        records = [_meta(), _user(), reasoning("T2", "rs_1", "first"), _call("T3", "c1"), _output("T4", "c1"),
                   reasoning("T5", "rs_2", "second")]
        conv = _parse_one(tmp_path, records)
        thinking = [b.content["thinking"] for r in conv.prompts[0].responses for b in r.content
                    if b.block_type == "thinking"]
        assert thinking == ["first", "second"]
        assert len(conv.prompts[0].responses) == 3

    def test_keyed_items_key_their_responses_by_their_own_ids(self, tmp_path):
        """Current files: a call and its output item keep their own ids, and the call's response is its item."""
        call = {"type": "response_item", "timestamp": "T2", "payload": {
            "type": "function_call", "id": "fc_1", "name": "shell_command", "call_id": "c1", "arguments": "{}"}}
        message = {"type": "response_item", "timestamp": "T3", "payload": {
            "type": "message", "id": "msg_1", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]}}
        records = [_meta(), _user(), call, _output("T4", "c1"), message]
        conv = _parse_one(tmp_path, records)
        responses = conv.prompts[0].responses
        assert [len(r.tool_calls) for r in responses] == [1, 0]
        assert responses[1].content[0].content["text"] == "x"

    def test_the_decoder_names_each_response_and_call_by_its_source_id(self, tmp_path):
        """The keys the assembler merges and attaches by, read from the decoder's own records.

        A keyed item carries its id as the response key and, for a call, as the
        call's parent_key. An item that states no id carries None, so the assembler
        opens it as its own response instead of merging it into a neighbour.
        """
        decoder = codex_cli.CodexDecoder(Source(kind="file", location=tmp_path / "s.jsonl"))
        decoder.decode(_meta())
        decoder.decode(_user())

        def item(item_id, payload):
            return {"type": "response_item", "timestamp": "T2", "payload": {"id": item_id, **payload}}

        keyed_call = decoder.decode(item("fc_1", {
            "type": "function_call", "name": "shell_command", "call_id": "c1", "arguments": "{}"}))
        unkeyed_call = decoder.decode(item(None, {
            "type": "function_call", "name": "shell_command", "call_id": "c2", "arguments": "{}"}))
        keyed_message = decoder.decode(item("msg_1", {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]}))
        unkeyed_message = decoder.decode(item(None, {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "y"}]}))

        def response_key(records):
            return next(r.key for r in records if isinstance(r, ResponseRec))

        def call_parent(records):
            return next(r.parent_key for r in records if isinstance(r, ToolCallRec))

        assert response_key(keyed_call) == "fc_1"
        assert call_parent(keyed_call) == "fc_1"
        assert response_key(unkeyed_call) is None
        assert call_parent(unkeyed_call) is None
        assert response_key(keyed_message) == "msg_1"
        assert response_key(unkeyed_message) is None

    def test_a_new_assistant_message_opens_a_new_response(self, tmp_path):
        records = [_meta(), _user(), _assistant("T2", "one"), _call("T3", "c1"), _output("T4", "c1"),
                   _assistant("T5", "two")]
        conv = _parse_one(tmp_path, records)
        assert [len(r.tool_calls) for r in conv.prompts[0].responses] == [0, 1, 0]

    def test_a_call_with_no_message_opens_a_response_of_its_own(self, tmp_path):
        records = [_meta(), _user(), _call("T2", "c1"), _output("T3", "c1")]
        conv = _parse_one(tmp_path, records)
        assert len(conv.prompts[0].responses) == 1
        assert conv.prompts[0].responses[0].tool_calls[0].status == "success"

    def test_a_call_without_a_result_stays_pending(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta(), _user(), _call("T2", "c1")])
        (call,) = _calls(conv)
        assert call.status == "pending" and call.result is None and call.timestamp == "T2"

    def test_an_output_whose_call_never_appears_is_a_named_orphan(self, tmp_path):
        path = write_jsonl(tmp_path, [_meta(), _user(), _output("T2", "ghost")])
        assert _assembly(path).ledger.reasons[(Disposition.ORPHAN, "tool result with no call")] == 1

    def test_reasoning_with_an_empty_summary_opens_no_response(self, tmp_path):
        empty = {"type": "response_item", "timestamp": "T2", "payload": {
            "type": "reasoning", "summary": [], "encrypted_content": "x"}}
        conv = _parse_one(tmp_path, [_meta(), _user(), empty])
        assert conv.prompts[0].responses == []

    def test_developer_messages_are_ignored_by_name(self, tmp_path):
        developer = {"type": "response_item", "timestamp": "T0", "payload": {
            "type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}}
        path = write_jsonl(tmp_path, [_meta(), developer, _user(), _assistant("T2")])
        assert _assembly(path).ledger.reasons[(Disposition.IGNORED, "response_item/message:developer")] == 1
        assert len(_only(path).prompts) == 1


class TestPreamble:
    """Nothing before the first user message is dropped, and no prompt is invented for it."""

    def test_assistant_text_before_any_user_message_is_the_preamble(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta(), _assistant("T0", "early"), _user("T1", "go"), _assistant("T2")])
        assert [b.content["text"] for r in conv.preamble for b in r.content] == ["early"]
        assert len(conv.prompts) == 1 and conv.prompts[0].content[0].content["text"] == "go"

    def test_calls_before_any_user_message_land_in_the_preamble_without_a_prompt(self, tmp_path):
        path = write_jsonl(tmp_path, [_meta(), _call("T1", "c1"), _output("T2", "c1"), _user("T3"), _assistant("T4")])
        conv = _only(path)
        assert len(conv.prompts) == 1
        assert [c.tool_name for r in conv.preamble for c in r.tool_calls] == ["shell_command"]
        ledger = _assembly(path).ledger
        assert ledger.synthesized == 0 and ledger.count(Disposition.ORPHAN) == 0

    def test_a_file_with_only_preamble_is_kept(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta(), _assistant("T0", "only")])
        assert conv.prompts == [] and len(conv.preamble) == 1


class TestInjectedContext:
    def test_harness_context_stays_a_prompt_with_its_text_under_meta_text(self, tmp_path):
        injected = _user("T1", "<environment_context>\n  <cwd>/Users/user/p</cwd>\n</environment_context>")
        conv = _parse_one(tmp_path, [_meta(), injected, _assistant("T2")])
        (block,) = conv.prompts[0].content
        assert block.block_type == "meta_text"
        assert block.content == {"meta_text": "<environment_context>\n  <cwd>/Users/user/p</cwd>\n</environment_context>",
                                 "meta_source": "environment_context"}

    def test_agents_instructions_are_injected_context(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta(), _user("T1", "# AGENTS.md instructions for /p\n\nrules"), _assistant("T2")])
        assert conv.prompts[0].content[0].block_type == "meta_text"
        assert conv.prompts[0].content[0].content["meta_source"] == "AGENTS.md"

    def test_a_persons_own_prompt_is_text_even_when_it_mentions_the_context(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta(), _user("T1", "why does <environment_context> look odd?"), _assistant("T2")])
        assert conv.prompts[0].content[0].block_type == "text"

    def test_the_first_text_block_decides(self, tmp_path):
        typed_first = {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "hi"}, {"type": "input_text", "text": "# AGENTS.md instructions"}]}}
        conv = _parse_one(tmp_path, [_meta(), typed_first, _assistant("T2")])
        assert [b.block_type for b in conv.prompts[0].content] == ["text", "text"]

    def test_automated_prompts_are_text_because_a_person_or_skill_wrote_them(self, tmp_path):
        conv = _parse_one(tmp_path, [_meta(), _user("T1", "# Task\n\nreview the diff"), _assistant("T2")])
        assert conv.prompts[0].content[0].block_type == "text"


class TestImages:
    def test_an_inline_base64_image_is_filtered_before_storage(self, tmp_path):
        payload = "iVBORw0KGgo" + "A" * 4000
        image = {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{payload}", "detail": "high"}]}}
        conv = _parse_one(tmp_path, [_meta(), image, _assistant("T2")])
        blocks = conv.prompts[0].content
        assert [b.block_type for b in blocks] == ["text", "input_image"]
        assert payload not in json.dumps(blocks[1].content)
        assert blocks[1].content["source"] == {"type": "filtered", "original_type": "base64", "media_type": "image/png",
                                               "original_size": len(payload), "filtered_reason": "binary_content"}


class TestUsageAccounting:
    """One API call is one increment; the response it belongs to is the latest one. Parse and peek agree."""

    def test_several_token_counts_each_land_on_their_call_and_all_sum(self, tmp_path):
        """One assistant turn with three tool calls is three API calls: each count is its call's, and all three sum."""
        records = [_meta(), _user(), _call("T2", "c1"),
                   _token_count("T3", inp=100, cached=40, out=10, reasoning=2, total_in=100),
                   _call("T4", "c2"),
                   _token_count("T5", inp=150, cached=120, out=20, reasoning=3, total_in=250),
                   _call("T6", "c3"),
                   _token_count("T7", inp=170, cached=160, out=30, reasoning=4, total_in=420)]
        conv = _parse_one(tmp_path, records)
        calls = [r for r in conv.prompts[0].responses if r.tool_calls]
        assert [r.usage.input_tokens for r in calls] == [100, 150, 170]
        assert _stored_totals(conv) == {"input_tokens": 420, "output_tokens": 60, "cached_input_tokens": 320,
                                        "reasoning_output_tokens": 9}

    def test_usage_before_any_response_attaches_to_the_next_response(self, tmp_path):
        records = [_meta(),
                   _token_count("T1", inp=50, cached=0, out=5, reasoning=0, total_in=50),
                   _user(), _assistant("T3"),
                   _token_count("T4", inp=60, cached=10, out=6, reasoning=0, total_in=110)]
        conv = _parse_one(tmp_path, records)
        assert conv.prompts[0].responses[0].usage.input_tokens == 110
        assert _stored_totals(conv)["input_tokens"] == 110

    def test_null_info_and_missing_last_usage_add_nothing(self, tmp_path):
        records = [_meta(), _user(), _assistant("T2"),
                   {"type": "event_msg", "timestamp": "T3", "payload": {"type": "token_count", "info": None}},
                   {"type": "event_msg", "timestamp": "T4", "payload": {
                       "type": "token_count", "info": {"total_token_usage": {"input_tokens": 9}}}},
                   _token_count("T5", inp=7, cached=0, out=1, reasoning=0, total_in=7)]
        resp = _parse_one(tmp_path, records).prompts[0].responses[0]
        assert resp.usage.input_tokens == 7 and resp.usage.output_tokens == 1

    def test_reemitted_token_count_is_not_counted_twice(self, tmp_path):
        """A token_count repeating the previous cumulative total adds nothing.

        Two real calls (100 then 50) plus a re-emission of the second. Correct: 150.
        """
        first = _token_count("T3", inp=100, cached=10, out=5, reasoning=1, total_in=100)
        second = _token_count("T5", inp=50, cached=20, out=3, reasoning=0, total_in=150)
        records = [_meta(), _user(), _assistant("T2"), first, _call("T4", "c2"), second,
                   _call("T6", "c3"), dict(second, timestamp="T7")]
        conv = _parse_one(tmp_path, records)
        assert _stored_totals(conv)["input_tokens"] == 150
        assert _stored_totals(conv)["cached_input_tokens"] == 30
        assert _stored_totals(conv)["output_tokens"] == 8

    def test_same_last_usage_with_moved_total_is_a_new_call(self, tmp_path):
        records = [_meta(), _user(), _assistant("T2"),
                   _token_count("T3", inp=100, cached=0, out=5, reasoning=0, total_in=100),
                   _token_count("T4", inp=100, cached=0, out=5, reasoning=0, total_in=200)]
        assert _parse_one(tmp_path, records).prompts[0].responses[0].usage.input_tokens == 200

    def test_usage_with_no_response_anywhere_is_a_counted_orphan_not_a_response(self, tmp_path):
        """Usage in a file with no message or call has nothing to belong to: named in the ledger, not invented."""
        path = write_jsonl(tmp_path, [_meta(), _token_count("T1", inp=80, cached=0, out=8, reasoning=0, total_in=80)])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.ORPHAN, "nothing to attach to")] == 1
        assert list(adapt(codex_cli).parse(Source(kind="file", location=path))) == []

    def test_token_usage_record_is_ignored(self, tmp_path):
        """token_usage_record mirrors token_count for the same calls; it is not a second source."""
        mirror = {"type": "token_usage_record", "timestamp": "T3", "payload": {
            "response_id": "resp_x", "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                                                "output_tokens": 5, "reasoning_output_tokens": 1}}}
        records = [_meta(), _user(), _assistant("T2"),
                   _token_count("T3", inp=100, cached=10, out=5, reasoning=1, total_in=100), mirror]
        assert _stored_totals(_parse_one(tmp_path, records))["input_tokens"] == 100

    def test_total_only_token_count_does_not_mask_the_next_call(self, tmp_path):
        """A token_count with a total and no last_token_usage is not a call; the next real call counts."""
        call = _token_count("T4", inp=100, cached=0, out=1, reasoning=0, total_in=5)
        total_only = {"type": "event_msg", "timestamp": "T3", "payload": {
            "type": "token_count", "info": {"total_token_usage": call["payload"]["info"]["total_token_usage"]}}}
        records = [_meta(), _user(), _assistant("T2"), total_only, call]
        conv = _parse_one(tmp_path, records)
        assert _stored_totals(conv)["input_tokens"] == 100

    def test_usage_after_a_preamble_response_is_the_preamble_responses(self, tmp_path):
        records = [_meta(), _assistant("T0", "early"),
                   _token_count("T1", inp=30, cached=0, out=3, reasoning=0, total_in=30),
                   _user("T2"), _assistant("T3")]
        conv = _parse_one(tmp_path, records)
        assert conv.preamble[0].usage.input_tokens == 30
        assert conv.prompts[0].responses[0].usage is None


class TestTaskComplete:
    def test_durations_go_on_the_latest_response(self, tmp_path):
        records = [_meta(), _user(), _assistant("T2"),
                   _token_count("T3", inp=10, cached=0, out=5, reasoning=0, total_in=10),
                   _task_complete("T4", duration_ms=4321, time_to_first_token_ms=250)]
        resp = _parse_one(tmp_path, records).prompts[0].responses[0]
        assert resp.usage.input_tokens == 10
        assert resp.attributes["duration_ms"] == "4321" and resp.attributes["time_to_first_token_ms"] == "250"

    def test_one_per_turn_so_the_later_turn_wins(self, tmp_path):
        """Setdefault kept the first task_complete of a response; each turn's duration is its own PatchRec."""
        records = [_meta(), _user(), _assistant("T2"), _task_complete("T3", duration_ms=100),
                   _task_complete("T4", duration_ms=900)]
        assert _parse_one(tmp_path, records).prompts[0].responses[0].attributes["duration_ms"] == "900"

    def test_a_turn_with_no_response_yet_has_nothing_to_describe(self, tmp_path):
        path = write_jsonl(tmp_path, [_meta(), _user(), _task_complete("T2", duration_ms=5)])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.IGNORED, "event_msg/task_complete:no-response")] == 1
        assert _only(path).prompts[0].responses == []

    def test_a_task_complete_without_durations_is_ignored_by_name(self, tmp_path):
        path = write_jsonl(tmp_path, [_meta(), _user(), _assistant("T2"), _task_complete("T3")])
        assert _assembly(path).ledger.reasons[(Disposition.IGNORED, "event_msg/task_complete:no-duration")] == 1


class TestBoundsAndModel:
    def test_bounds_span_every_line_as_they_always_have(self, tmp_path):
        """A line that creates no event still bounds the conversation: the bounds are the file's, not the events'.

        An ignored record (an echo) after the last message ends the conversation, as the first-session parse did.
        """
        echo = {"type": "event_msg", "timestamp": "2026-01-01T00:09:00Z", "payload": {"type": "agent_reasoning"}}
        records = [_meta(ts="2026-01-01T00:00:00Z"), _user("2026-01-01T00:00:01Z"), _assistant("2026-01-01T00:00:02Z"),
                   echo]
        conv = _parse_one(tmp_path, records)
        assert conv.started_at == "2026-01-01T00:00:00Z" and conv.ended_at == "2026-01-01T00:09:00Z"

    def test_each_response_carries_the_model_of_its_own_turn(self, tmp_path):
        records = [_meta(), _turn("T0", "first"), _user("T1"), _assistant("T2"),
                   _turn("T3", "second"), _user("T4", "more"), _assistant("T5")]
        conv = _parse_one(tmp_path, records)
        assert [r.model for r in _responses(conv)] == ["first", "second"]

    def test_a_turn_without_a_model_keeps_the_previous_one(self, tmp_path):
        records = [_meta(), _turn("T0", "first"), {"type": "turn_context", "timestamp": "T1", "payload": {}},
                   _user("T2"), _assistant("T3")]
        assert _responses(_parse_one(tmp_path, records))[0].model == "first"

    def test_a_restated_meta_supplies_no_workspace_and_the_first_wins(self, tmp_path):
        records = [_meta("c", cwd="/child"), _meta("p", cwd="/parent"), _user(), _assistant("T2")]
        assert _parse_one(tmp_path, records).workspace_path == "/child"


class TestPeek:
    def test_peek_counts_the_preamble_and_agrees_with_parse(self, tmp_path):
        path = write_jsonl(tmp_path, [_meta(), _assistant("T0", "early"), _user("T1"), _assistant("T2")])
        conv = _only(path)
        scan = adapt(codex_cli).peek_scan(path)
        exchanges = adapt(codex_cli).peek_exchanges(path, last_n=10)
        assert scan.exchange_count == len(exchanges) == len(conv.prompts) + 1
        assert exchanges[0].prompt_text is None and exchanges[1].prompt_text == "go"

    def test_peek_of_a_subagent_names_its_parent_session(self, tmp_path):
        meta = _meta("child", source={"subagent": {"thread_spawn": {"parent_thread_id": "P1"}}})
        path = write_jsonl(tmp_path, [meta, _user(), _assistant("T2")])
        assert adapt(codex_cli).peek_scan(path).parent_session_id == "P1"

    def test_a_subagent_is_found_by_its_own_id_through_the_real_reader(self, tmp_path, monkeypatch):
        """Peek lookup uses the file's own thread id, which is what its name and stored identity carry."""
        from siftd.peek.reader import find_session_file
        from siftd.peek.scanner import DiscoveredFile

        child = "019f66ea-bd42-7ea3-acbc-13a34a3aa4d0"
        parent = "019f66ea-963d-7101-a549-37c4fd3fccaa"
        meta = _meta(child, source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}})
        path = write_jsonl(tmp_path, [meta, _user(), _assistant("T2")], name=f"rollout-2026-07-15T12-54-47-{child}.jsonl")
        assert adapt(codex_cli).peek_scan(path).session_id == child
        monkeypatch.setattr("siftd.peek.reader._discover_files", lambda **kw: [
            DiscoveredFile(path=path, mtime=1000.0, adapter_module=adapt(codex_cli), adapter_name="codex_cli")])
        assert find_session_file(child) == path
        assert find_session_file(child[:13]) == path

    def test_peek_tool_names_are_canonicalized(self, tmp_path):
        path = CodexSession(tmp_path, exchanges=1).with_tools(["exec_command"]).build()
        (exchange,) = adapt(codex_cli).peek_exchanges(path, last_n=5)
        assert exchange.tool_calls == [("shell.execute", 1)]


class TestGoldenCases:
    @pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()))
    def test_fixtures_decode_with_nothing_unknown_or_orphaned(self, case):
        ledger = _assembly(GOLDEN / case / "input.jsonl").ledger
        assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0
        assert ledger.disposed == ledger.records

    def test_a_forked_child_keeps_its_own_identity_and_both_parents(self):
        conv = _only(GOLDEN / "forked_child" / "input.jsonl")
        assert conv.external_id.startswith("codex_cli::019f66ea-bd42")
        assert conv.superseded_external_ids == ("codex_cli::019f66ea-963d-7101-a549-37c4fd3fccaa",)
        assert conv.parent("subagent").parent == conv.parent("fork").parent

    def test_an_injected_context_capture_has_no_text_block_from_the_harness(self):
        conv = _only(GOLDEN / "injected_context" / "input.jsonl")
        assert [b.block_type for p in conv.prompts for b in p.content][:2] == ["meta_text", "meta_text"]

    def test_a_capture_with_assistant_text_before_the_prompt_keeps_it(self):
        conv = _only(GOLDEN / "preamble_before_prompt" / "input.jsonl")
        assert len(conv.preamble) == 1

    def test_per_turn_models_in_a_capture(self):
        conv = _only(GOLDEN / "model_turns" / "input.jsonl")
        assert [r.model for r in _responses(conv)] == ["gpt-5.6-sol", "gpt-5.6-terra"]
