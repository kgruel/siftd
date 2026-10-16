"""Tests for Codex CLI adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, CodexSession, default_location_source, fixture_source, write_jsonl

from siftd.adapters import codex_cli
from siftd.domain.source import Source


class TestCodexCliAdapter:

    def test_can_handle(self):
        assert codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.jsonl")))
        assert not codex_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl"))

    def test_parse_full(self, tmp_path):
        codex_source = fixture_source(tmp_path, "codex_cli_minimal.jsonl", "sessions")
        conv = list(codex_cli.parse(codex_source))[0]
        assert conv.external_id == "codex_cli::codex-session-1" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "codex_cli" and conv.harness.source == "openai"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "Run ls" in prompt.content[0].content["text"] and len(prompt.responses) == 1
        resp = prompt.responses[0]
        tc = resp.tool_calls[0]
        assert tc.tool_name == "shell_command" and tc.status == "success" and "README.md" in str(tc.result)
        assert resp.usage.input_tokens == 120 and resp.usage.output_tokens == 45
        assert resp.attributes.get("cache_read_input_tokens") == "10"


class TestCodexCliParseEdgeCases:
    def test_custom_and_function_tools(self, tmp_path):
        def _tcs(c):
            return [tc for p in c.prompts for r in p.responses for tc in r.tool_calls]
        conv = list(codex_cli.parse(Source(kind="file", location=CodexSession(tmp_path, exchanges=1).with_custom_tools(["my_tool"]).build())))[0]
        assert any(tc.tool_name == "my_tool" and tc.status == "success" for tc in _tcs(conv))
        conv2 = list(codex_cli.parse(Source(kind="file", location=CodexSession(tmp_path, exchanges=1, name="f.jsonl").with_tools(["shell"]).build())))[0]
        assert any(tc.tool_name == "shell" and tc.status == "success" for tc in _tcs(conv2))

    def test_can_handle_and_usage(self, tmp_path):
        d = tmp_path / "sessions" / "2024"
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text("{}\n")
        assert codex_cli.can_handle(Source(kind="file", location=d / "s.jsonl"))
        assert not codex_cli.can_handle(Source(kind="file", location=tmp_path / "other.jsonl"))
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "turn_context", "timestamp": "T1", "payload": {"model": "gpt-4"}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}},
            {"type": "event_msg", "timestamp": "T4", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 30, "reasoning_output_tokens": 10}}}}]
        resp = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0].prompts[0].responses[0]
        assert resp.usage.input_tokens == 100 and resp.attributes.get("cached_input_tokens") == "30"

    def test_pending_tools_and_early_call(self, tmp_path):
        # function_call before assistant message → _get_or_create_response creates new (L289-292)
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert any(tc.status == "pending" for p in conv.prompts for r in p.responses for tc in r.tool_calls)
        assert not codex_cli.can_handle(Source(kind="sqlite", location=Path("/mock/sessions/s.jsonl")))

    def test_normalizer(self):
        n = codex_cli.normalize_record
        assert n({"type": "session_meta", "timestamp": "T", "payload": {"id": "s", "cwd": "/w"}}).session_id == "s"
        assert n({"type": "turn_context", "timestamp": "T", "payload": {"model": "m"}}).model == "m"
        u = n({"type": "response_item", "timestamp": "T", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]}})
        assert u.kind == "user" and u.content_blocks[0]["text"] == "q"
        a = n({"type": "response_item", "timestamp": "T", "payload": {"type": "message", "role": "assistant", "content": ["plain", {"type": "output_text", "text": "ok"}]}})
        assert a.kind == "assistant" and a.content_blocks[0]["text"] == "plain" and a.content_blocks[1]["text"] == "ok"
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "function_call", "name": "sh"}}).kind == "tool_use"
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "custom_tool_call", "name": "x"}}).tool_name == "x"
        ev = n({"type": "event_msg", "timestamp": "T", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 3}}}})
        assert ev.kind == "usage" and ev.input_tokens == 5
        assert n({"type": "event_msg", "timestamp": "T", "payload": {"type": "other"}}) is None
        assert n({"type": "unknown"}) is None
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "unknown"}}) is None

    def test_normalizer_block_edges(self):
        n = codex_cli.normalize_record
        a = n({"type": "response_item", "timestamp": "T", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "reasoning", "text": "think"}]}})
        assert a.content_blocks[0]["type"] == "reasoning"

    def test_discover_and_parse_edges(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        (d / "s.jsonl").write_text("{}\n")
        assert list(codex_cli.discover(locations=[str(tmp_path)]))
        (d / "empty.jsonl").write_text("")
        assert list(codex_cli.parse(Source(kind="file", location=d / "empty.jsonl"))) == []
        assert not codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.json")))
        assert codex_cli.can_handle(default_location_source(codex_cli))
        # Out-of-order timestamps to cover L109 (started_at update)
        records = [{"type": "session_meta", "timestamp": "2024-01-01T00:00:05Z", "payload": {"id": "s", "cwd": "/w"}},
            {"type": "response_item", "timestamp": "2024-01-01T00:00:01Z", "payload": {"type": "message", "role": "user", "content": ["plain text"]}},
            {"type": "response_item", "timestamp": "2024-01-01T00:00:02Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "other_type", "data": 1}]}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "ts.jsonl"))))[0]
        assert "00:01" in conv.started_at and conv.prompts[0].content[0].content["text"] == "plain text"

    def test_tool_aliases_cover_exec_and_wait(self):
        assert codex_cli.TOOL_ALIASES["exec"] == "shell.execute"
        assert codex_cli.TOOL_ALIASES["wait"] == "shell.wait"

    def test_reasoning_summary_parse(self, tmp_path):
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "reasoning", "id": "r1", "encrypted_content": "xxx", "summary": [{"type": "summary_text", "text": "I will run ls"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}},
        ]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "reason.jsonl"))))[0]
        blocks = [b for p in conv.prompts for r in p.responses for b in r.content]
        thinking = [b for b in blocks if b.block_type == "thinking"]
        assert thinking and thinking[0].content["thinking"] == "I will run ls"

    def test_reasoning_empty_summary_parse_is_noop(self, tmp_path):
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "reasoning", "id": "r1", "encrypted_content": "xxx", "summary": []}},
        ]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "reason2.jsonl"))))[0]
        # Empty summary must not synthesize a response
        assert conv.prompts[0].responses == []

    def test_reasoning_normalizer(self):
        n = codex_cli.normalize_record
        r = n({"type": "response_item", "timestamp": "T", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan"}]}})
        assert r.kind == "assistant" and r.content_blocks == [{"type": "thinking", "thinking": "plan"}]
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "reasoning", "summary": []}}) is None
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "reasoning", "summary": None}}) is None

    def test_task_complete_parse_attributes(self, tmp_path):
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "event_msg", "timestamp": "T3", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 5}}}},
            {"type": "event_msg", "timestamp": "T4", "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done", "duration_ms": 4321, "time_to_first_token_ms": 250}},
        ]
        resp = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "tc.jsonl"))))[0].prompts[0].responses[0]
        # token_count cleared pending_usage_response; task_complete still lands
        assert resp.usage.input_tokens == 10
        assert resp.attributes.get("duration_ms") == "4321"
        assert resp.attributes.get("time_to_first_token_ms") == "250"

    def test_task_complete_normalizer(self):
        n = codex_cli.normalize_record
        r = n({"type": "event_msg", "timestamp": "T", "payload": {"type": "task_complete", "duration_ms": 4321, "time_to_first_token_ms": 250}})
        assert r.kind == "metadata"
        assert r.extra == {"duration_ms": "4321", "time_to_first_token_ms": "250"}
        r2 = n({"type": "event_msg", "timestamp": "T", "payload": {"type": "task_complete"}})
        assert r2.kind == "metadata" and r2.extra == {}

    def test_orphan_tool_call_before_user_message(self, tmp_path):
        """Tool calls before any user message create a synthetic prompt."""
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {
                "type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"type": "response_item", "timestamp": "T2", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": "done"}},
        ]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "orphan.jsonl"))))[0]
        assert len(conv.prompts) == 1, "synthetic prompt should be created"
        assert len(conv.prompts[0].responses) == 1
        assert conv.prompts[0].responses[0].tool_calls[0].tool_name == "shell"


# --- Usage accounting -------------------------------------------------------
#
# One rule (codex_cli._UsageLedger): each token_count's last_token_usage adds to
# the most recent stored response; usage before any stored response is held for
# the next one. A token_count that repeats the previous cumulative total is a
# re-emission and is dropped (_drop_reemitted_token_counts). The invariant the
# tests pin: sum over stored responses == sum over non-re-emitted events.

_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _meta():
    return {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}}


def _user(ts="T1", text="go"):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def _assistant(ts, text="ok"):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}


def _call(ts, call_id):
    return {"type": "response_item", "timestamp": ts, "payload": {
        "type": "function_call", "name": "shell_command", "call_id": call_id, "arguments": "{}"}}


def _token_count(ts, *, inp, cached, out, reasoning, total_in, total=True):
    """A token_count event. total_in is the cumulative input after this call."""
    info = {"last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                 "output_tokens": out, "reasoning_output_tokens": reasoning}}
    if total:
        info["total_token_usage"] = {"input_tokens": total_in, "cached_input_tokens": cached,
                                     "output_tokens": out, "reasoning_output_tokens": reasoning}
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": info}}


def _parse_one(tmp_path, records, name="usage.jsonl"):
    return list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, name))))[0]


def _stored_totals(conv):
    totals = {k: 0 for k in _USAGE_KEYS}
    for p in conv.prompts:
        for r in p.responses:
            if r.usage:
                totals["input_tokens"] += r.usage.input_tokens or 0
                totals["output_tokens"] += r.usage.output_tokens or 0
            totals["cached_input_tokens"] += int(r.attributes.get("cached_input_tokens", "0"))
            totals["reasoning_output_tokens"] += int(r.attributes.get("reasoning_output_tokens", "0"))
    return totals


class TestUsageAccounting:

    def test_several_token_counts_sum_onto_one_response(self, tmp_path):
        """One assistant turn with three tool calls is three API calls: all three sum."""
        records = [_meta(), _user(), _call("T2", "c1"),
                   _token_count("T3", inp=100, cached=40, out=10, reasoning=2, total_in=100),
                   _call("T4", "c2"),
                   _token_count("T5", inp=150, cached=120, out=20, reasoning=3, total_in=250),
                   _call("T6", "c3"),
                   _token_count("T7", inp=170, cached=160, out=30, reasoning=4, total_in=420)]
        conv = _parse_one(tmp_path, records)
        assert len(conv.prompts[0].responses) == 1
        resp = conv.prompts[0].responses[0]
        assert resp.usage.input_tokens == 420
        assert resp.usage.output_tokens == 60
        assert resp.attributes["cached_input_tokens"] == "320"
        assert resp.attributes["cache_read_input_tokens"] == "320"
        assert resp.attributes["reasoning_output_tokens"] == "9"

    def test_usage_before_any_response_attaches_to_next_stored_response(self, tmp_path):
        records = [_meta(),
                   _token_count("T1", inp=50, cached=0, out=5, reasoning=0, total_in=50),
                   _user(),
                   _assistant("T3"),
                   _token_count("T4", inp=60, cached=10, out=6, reasoning=0, total_in=110)]
        conv = _parse_one(tmp_path, records)
        resp = conv.prompts[0].responses[0]
        assert resp.usage.input_tokens == 110
        assert resp.usage.output_tokens == 11
        assert _stored_totals(conv)["input_tokens"] == 110

    def test_null_info_and_missing_last_usage_add_nothing(self, tmp_path):
        records = [_meta(), _user(), _assistant("T2"),
                   {"type": "event_msg", "timestamp": "T3", "payload": {"type": "token_count", "info": None}},
                   {"type": "event_msg", "timestamp": "T4", "payload": {
                       "type": "token_count", "info": {"total_token_usage": {"input_tokens": 9}}}},
                   _token_count("T5", inp=7, cached=0, out=1, reasoning=0, total_in=7)]
        conv = _parse_one(tmp_path, records)
        resp = conv.prompts[0].responses[0]
        assert resp.usage.input_tokens == 7 and resp.usage.output_tokens == 1

    def test_reemitted_token_count_is_not_counted_twice(self, tmp_path):
        """A token_count repeating the previous cumulative total adds nothing.

        Two real calls (100 then 50) plus a re-emission of the second. Correct: 150.
        Last-wins gives 50 (the first call is lost); accumulate-without-dedupe gives
        200. A single real call would let last-wins pass by coincidence.
        """
        first = _token_count("T3", inp=100, cached=10, out=5, reasoning=1, total_in=100)
        second = _token_count("T5", inp=50, cached=20, out=3, reasoning=0, total_in=150)
        # The re-emission lands while a call is pending again (after a new function_call).
        records = [_meta(), _user(), _assistant("T2"), first, _call("T4", "c2"), second,
                   _call("T6", "c3"), dict(second, timestamp="T7")]
        conv = _parse_one(tmp_path, records)
        assert _stored_totals(conv)["input_tokens"] == 150
        assert _stored_totals(conv)["cached_input_tokens"] == 30
        assert _stored_totals(conv)["output_tokens"] == 8

    def test_same_last_usage_with_moved_total_is_a_new_call(self, tmp_path):
        """Identical per-call numbers are two calls when the cumulative total moved."""
        records = [_meta(), _user(), _assistant("T2"),
                   _token_count("T3", inp=100, cached=0, out=5, reasoning=0, total_in=100),
                   _token_count("T4", inp=100, cached=0, out=5, reasoning=0, total_in=200)]
        conv = _parse_one(tmp_path, records)
        assert conv.prompts[0].responses[0].usage.input_tokens == 200

    def test_usage_with_no_stored_response_gets_a_synthetic_one(self, tmp_path):
        """Usage in a file with no message or call is kept on a synthetic response."""
        records = [_meta(),
                   _token_count("T1", inp=80, cached=0, out=8, reasoning=0, total_in=80)]
        conv = _parse_one(tmp_path, records)
        assert _stored_totals(conv)["input_tokens"] == 80
        assert _stored_totals(conv)["output_tokens"] == 8

    def test_assistant_before_user_is_not_a_usage_target(self, tmp_path):
        """The detached (never-stored) assistant message does not swallow usage."""
        records = [_meta(), _assistant("T0", "early"),
                   _token_count("T1", inp=30, cached=0, out=3, reasoning=0, total_in=30),
                   _user("T2"), _assistant("T3")]
        conv = _parse_one(tmp_path, records)
        assert len(conv.prompts) == 1
        assert conv.prompts[0].responses[0].usage.input_tokens == 30

    def test_token_usage_record_is_ignored(self, tmp_path):
        """token_usage_record mirrors token_count for the same calls; it is not a second source."""
        mirror = {"type": "token_usage_record", "timestamp": "T3", "payload": {
            "response_id": "resp_x", "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                                                "output_tokens": 5, "reasoning_output_tokens": 1}}}
        records = [_meta(), _user(), _assistant("T2"),
                   _token_count("T3", inp=100, cached=10, out=5, reasoning=1, total_in=100), mirror]
        conv = _parse_one(tmp_path, records)
        assert _stored_totals(conv)["input_tokens"] == 100

    def test_peek_agrees_with_parse(self, tmp_path):
        """Peek reads through the same re-emission rule, so its sums match parse."""
        original = _token_count("T4", inp=100, cached=10, out=5, reasoning=1, total_in=140)
        records = [_meta(), _user(), _call("T2", "c1"),
                   _token_count("T3", inp=40, cached=0, out=2, reasoning=0, total_in=40),
                   _user("T5", "more"), original, dict(original, timestamp="T6"),
                   _assistant("T7"), _token_count("T8", inp=25, cached=5, out=3, reasoning=0, total_in=165)]
        path = write_jsonl(tmp_path, records, "peek.jsonl")
        conv = _parse_one(tmp_path, records, "peek.jsonl")
        exchanges = codex_cli.peek_exchanges(path, last_n=10**6)
        # 40 + 100 + 25 = 165, the final cumulative total; the re-emitted 100 is not counted again.
        assert sum(e.input_tokens for e in exchanges) == _stored_totals(conv)["input_tokens"] == 165
        assert sum(e.output_tokens for e in exchanges) == 10

    def test_total_only_token_count_does_not_mask_the_next_call(self, tmp_path):
        """A token_count with a total and no last_token_usage is not a call.

        The next event carries the same total plus a real 100-token call. That
        call must count: the total-only event did not move the watermark.
        """
        call = _token_count("T4", inp=100, cached=0, out=1, reasoning=0, total_in=5)
        # Same cumulative total as the call, which is what makes it a repeat if it advanced the watermark.
        total_only = {"type": "event_msg", "timestamp": "T3", "payload": {
            "type": "token_count", "info": {"total_token_usage": call["payload"]["info"]["total_token_usage"]}}}
        records = [_meta(), _user(), _assistant("T2"), total_only, call]
        path = write_jsonl(tmp_path, records, "total_only.jsonl")
        conv = _parse_one(tmp_path, records, "total_only.jsonl")
        assert _stored_totals(conv)["input_tokens"] == 100
        assert sum(e.input_tokens for e in codex_cli.peek_exchanges(path, last_n=10**6)) == 100

    def test_peek_exchange_attribution_matches_parse_prompts(self, tmp_path):
        """Usage before the first prompt goes to the same exchange parse stores it on.

        Compared per prompt/exchange, not only in total: a leading token_count that
        peek drops (or attaches to a prompt-less exchange) would keep the sum equal
        only by accident in a single-prompt file.
        """
        records = [_meta(),
                   _token_count("T0", inp=50, cached=0, out=5, reasoning=0, total_in=50),
                   _user("T1", "a"), _assistant("T2"),
                   _token_count("T3", inp=10, cached=0, out=1, reasoning=0, total_in=60),
                   _user("T4", "b"), _assistant("T5"),
                   _token_count("T6", inp=7, cached=0, out=1, reasoning=0, total_in=67)]
        path = write_jsonl(tmp_path, records, "leading.jsonl")
        conv = _parse_one(tmp_path, records, "leading.jsonl")
        parsed = [sum((r.usage.input_tokens or 0) for r in p.responses if r.usage) for p in conv.prompts]
        peeked = [e.input_tokens for e in codex_cli.peek_exchanges(path, last_n=10**6)]
        assert parsed == peeked == [60, 7]

    def test_peek_attributes_usage_before_a_prompts_first_response_like_parse(self, tmp_path):
        """A user message with no response yet does not take the previous turn's usage.

        Codex injects user-role notices mid-turn. A token_count that follows one
        belongs to the latest stored response (turn a), not to the notice's
        exchange (b), which has no response until later. Per exchange, parse and
        peek must agree; the corpus has six files where a per-prompt rule did not.
        """
        records = [_meta(), _user("T1", "a"), _assistant("T2"),
                   _token_count("T3", inp=10, cached=0, out=1, reasoning=0, total_in=10),
                   _user("T4", "notice"),
                   _token_count("T5", inp=20, cached=0, out=2, reasoning=0, total_in=30),
                   _assistant("T6"),
                   _token_count("T7", inp=5, cached=0, out=1, reasoning=0, total_in=35)]
        path = write_jsonl(tmp_path, records, "notice.jsonl")
        conv = _parse_one(tmp_path, records, "notice.jsonl")
        parsed = [sum((r.usage.input_tokens or 0) for r in p.responses if r.usage) for p in conv.prompts]
        peeked = [e.input_tokens for e in codex_cli.peek_exchanges(path, last_n=10**6)]
        assert parsed == peeked == [30, 5]

    def test_peek_tool_call_without_text_takes_usage_like_parse(self, tmp_path):
        """A function_call is a stored response even with no assistant text.

        parse() creates a response for it, so the next token_count goes there,
        not to the following prompt's first response.
        """
        records = [_meta(), _user("T1", "a"), _call("T2", "c1"),
                   _token_count("T3", inp=10, cached=0, out=1, reasoning=0, total_in=10),
                   _user("T4", "b"),
                   _token_count("T5", inp=20, cached=0, out=2, reasoning=0, total_in=30),
                   _assistant("T6")]
        path = write_jsonl(tmp_path, records, "call_only.jsonl")
        conv = _parse_one(tmp_path, records, "call_only.jsonl")
        parsed = [sum((r.usage.input_tokens or 0) for r in p.responses if r.usage) for p in conv.prompts]
        peeked = [e.input_tokens for e in codex_cli.peek_exchanges(path, last_n=10**6)]
        assert parsed == peeked == [30, 0]

    def test_peek_drops_prompt_less_assistant_like_parse(self, tmp_path):
        """An assistant message before the first user message is never stored by parse.

        Peek must not show it as an exchange, or the usage parse attaches to the
        first stored response lands on that phantom exchange instead.
        """
        records = [_meta(), _assistant("T0", "early"),
                   _token_count("T1", inp=30, cached=0, out=3, reasoning=0, total_in=30),
                   _user("T2"), _assistant("T3")]
        path = write_jsonl(tmp_path, records, "phantom.jsonl")
        conv = _parse_one(tmp_path, records, "phantom.jsonl")
        exchanges = codex_cli.peek_exchanges(path, last_n=10**6)
        assert len(exchanges) == len(conv.prompts) == 1
        assert exchanges[0].prompt_text is not None
        assert exchanges[0].input_tokens == conv.prompts[0].responses[0].usage.input_tokens == 30
