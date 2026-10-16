"""Conservation probe (scripts/probe.py): native raw-file counts vs parse() output.

The probe is a script under scripts/, so it is loaded by path the same way
test_gen_docs_*.py loads gen_docs.py. Native counters are checked against the
golden adapter inputs, with expected values read from those inputs by hand (not
copied from probe output). The claude split-message case is a real capture: one
assistant message spans several streamed records, and the probe must count it
once per message.id, while parse() counts it once per record.

Nothing here reads the real home-directory corpus. Measurement is driven with
fixture paths and a fake adapter module.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from siftd.adapters import copilot_cli, pi_agent, vscode
from siftd.domain import Source
from siftd.domain.models import Conversation, Harness, Prompt, Response, ToolCall, Usage

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROBE = _REPO_ROOT / "scripts" / "probe.py"
_GOLDEN = _REPO_ROOT / "tests" / "fixtures" / "adapters"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe", _PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules["probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _golden_input(adapter: str, case: str) -> Path:
    candidates = [p for p in (_GOLDEN / adapter / case).iterdir() if p.is_file() and p.name != "expected.json"]
    assert len(candidates) == 1, candidates
    return candidates[0]


def _tally_fields(tally) -> dict:
    return {k: v for k, v in tally.to_dict().items() if k != "records"}


# ---------------------------------------------------------------------------
# Coverage ratchet
# ---------------------------------------------------------------------------


def test_native_counters_cover_every_builtin_adapter():
    """Every built-in adapter has a native counter, so no adapter is silently absent."""
    from siftd.adapters.registry import load_builtin_adapters

    builtin = {info.name for info in load_builtin_adapters()}
    assert builtin == set(probe.NATIVE_COUNTERS)


# ---------------------------------------------------------------------------
# Native counters against golden inputs (expected values read from the fixtures)
# ---------------------------------------------------------------------------


def test_claude_code_minimal_native():
    t = probe._native_claude_code(_golden_input("claude_code", "minimal"))
    # msg-002 and msg-004 are assistant records with no message.id; uuid keys them.
    assert _tally_fields(t) == {
        "user": 1,
        "assistant": 2,
        "tool_calls": 1,
        "tool_results": 1,
        "input_tokens": 250,  # 100 + 150
        "output_tokens": 70,  # 50 + 20
        "cache_tokens": 10,  # cache_creation on msg-002
        "conversations": 0,
        "conversations_no_prompts": 0,
        "conversations_no_responses": 0,
        "tool_calls_no_result": 0,
    }
    assert dict(t.records) == {"user": 2, "assistant": 2}


def test_claude_code_subagent_native():
    t = probe._native_claude_code(_golden_input("claude_code", "subagent"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 1, 1)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (200, 55, 0)


def test_claude_code_split_message_counts_each_message_once():
    """The defect this probe exists to expose: one message.id across several records.

    The fixture is a real capture (redacted). Message Y7iMb9 spans two records
    and kTFZNC spans three, each record repeating the message's usage. Native
    must count 2 responses and one usage per message. Per message: input 9+8,
    output 418+1972, cache 42987 (Y7iMb9) + 42987+5697 (kTFZNC).
    """
    t = probe._native_claude_code(_golden_input("claude_code", "split_message"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 2, 2)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (17, 2390, 91671)


def test_claude_mixed_tool_result_and_text_is_one_prompt(tmp_path):
    """A user record with a tool_result AND a text block is a prompt, not only a result.

    The real capture has four such records, all carrying extra user text beside
    the tool result. Native must count them as user messages and as results.
    """
    path = tmp_path / "session.jsonl"
    records = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "plain prompt"}},
        {
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
                    {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {}},
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                    {"type": "text", "text": "neutral follow-up text"},
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u3",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "again"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    t = probe._native_claude_code(path)
    # Two prompts: the plain string record and the mixed record. The tool-result-only record is not one.
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (2, 1, 2, 2)


def test_split_message_over_count_shows_as_negative_diff():
    """Diff arithmetic for an adapter that yields one Response per record.

    The split-message capture has 2 native responses. A per-record adapter yields
    5 (the record count); the probe must report that as a negative diff, not
    as a drop. Parsed values are synthetic so this test does not pin the
    parser's current over-count as a contract: when parse() is fixed, nothing here changes.
    """
    per_record = [Response(timestamp="t", usage=Usage(input_tokens=1, output_tokens=1)) for _ in range(5)]
    parsed = probe.parsed_tally([_conversation(per_record)])
    native = probe._native_claude_code(_golden_input("claude_code", "split_message"))
    report = probe.AdapterReport(name="claude_code", records_usage=True, native=native, parsed=parsed)
    diff = report.diff()
    assert diff["assistant"] == native.assistant - 5 == -3
    assert diff["output_tokens"] == native.output_tokens - 5 == 2385


def test_codex_cli_minimal_native():
    t = probe._native_codex_cli(_golden_input("codex_cli", "minimal"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 1, 1, 1)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (120, 45, 10)
    assert dict(t.records) == {"session_meta": 1, "turn_context": 1, "response_item": 4, "event_msg": 1}


def test_codex_cli_null_info_token_events_are_not_usage(tmp_path):
    """token_count events with info null carry no usage; only non-null ones count."""
    path = tmp_path / "rollout.jsonl"
    events = [
        {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 7, "output_tokens": 3, "cached_input_tokens": 2}},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    t = probe._native_codex_cli(path)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (7, 3, 2)


def test_antigravity_planner_tokens_are_native_usage(tmp_path):
    """Usage lives on PLANNER_RESPONSE records as top-level token fields."""
    path = tmp_path / "transcript_full.jsonl"
    steps = [
        {"type": "USER_INPUT", "content": "x"},
        {
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "run_command", "args": {}}],
            "input_tokens": 10,
            "cache_read_tokens": 90,
            "output_tokens": 4,
        },
        {"type": "RUN_COMMAND", "content": "ok"},
        {"type": "SYSTEM_MESSAGE", "content": "notice"},
        {"type": "PLANNER_RESPONSE", "input_tokens": 5, "cache_read_tokens": 0, "output_tokens": 6},
    ]
    path.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    t = probe._native_antigravity_cli(path)
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 1, 1)
    # Input is compared in siftd's stored convention for a google-source row:
    # native input plus cache reads (10 + 90 + 5 + 0).
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (105, 10, 90)


def test_antigravity_error_and_reminder_steps_are_not_tool_results(tmp_path):
    """ERROR_MESSAGE and EPHEMERAL_MESSAGE are not tool results (corpus: 658 and 198 steps).

    Only the GENERIC step is a result. The error and the system reminder are
    counted under records, so tool_results does not inflate by the number of
    stream errors and reminders.
    """
    path = tmp_path / "transcript_full.jsonl"
    steps = [
        {"type": "USER_INPUT", "content": "<USER_REQUEST>\nq\n</USER_REQUEST>"},
        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "list_dir", "args": {}}]},
        {"type": "GENERIC", "status": "DONE", "content": "listing"},
        {"type": "ERROR_MESSAGE", "status": "DONE", "error": "API error (attempt 1): request failed"},
        {"type": "EPHEMERAL_MESSAGE", "status": "DONE", "content": "system reminder, not sent by the user"},
        {"type": "PLANNER_RESPONSE", "content": "done"},
    ]
    path.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    t = probe._native_antigravity_cli(path)
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 1, 1)
    assert t.records["ERROR_MESSAGE"] == 1 and t.records["EPHEMERAL_MESSAGE"] == 1


def test_antigravity_minimal_native():
    t = probe._native_antigravity_cli(_golden_input("antigravity_cli", "minimal"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 1, 1)
    assert dict(t.records) == {
        "CONVERSATION_HISTORY": 1,
        "USER_INPUT": 1,
        "PLANNER_RESPONSE": 2,
        "CHECKPOINT": 1,
        "LIST_DIRECTORY": 1,
        "SYSTEM_MESSAGE": 1,
    }


def test_gemini_cli_minimal_native():
    t = probe._native_gemini_cli(_golden_input("gemini_cli", "minimal"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 1, 1, 1)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (50, 30, 0)


def test_pi_agent_minimal_native():
    t = probe._native_pi_agent(_golden_input("pi_agent", "minimal"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 1, 1)
    # a1b2c3d7: 500/120/cacheRead 50 + cacheWrite 10; a1b2c3d9: 700/30/100 + 0.
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (1200, 150, 160)
    assert dict(t.records) == {"session": 1, "model_change": 1, "thinking_level_change": 1, "message": 4}


def test_pi_standalone_usage_record_counts_tokens_not_messages(tmp_path):
    """A cache-warming usage record is billed tokens with no message behind it.

    Its tokens are summed, but it is not an assistant message. The assistant
    message next to it contributes its own usage once.
    """
    path = tmp_path / "session.jsonl"
    records = [
        {"type": "session", "id": "s1", "timestamp": "2026-10-06T19:00:00Z", "cwd": "/Users/user/work"},
        {
            "type": "message",
            "id": "a1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input": 10, "output": 4, "cacheRead": 100, "cacheWrite": 0},
            },
        },
        {
            "type": "usage",
            "id": "u1",
            "kind": "cache_warm",
            "usage": {"input": 2, "output": 1, "cacheRead": 219453, "cacheWrite": 7},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    t = probe._native_pi_agent(path)
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (0, 1, 0, 0)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (12, 5, 100 + 219453 + 7)
    assert t.records["usage"] == 1


def test_copilot_cli_minimal_native():
    t = probe._native_copilot_cli(_golden_input("copilot_cli", "minimal"))
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 2, 1, 1)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (0, 0, 0)


def test_opencode_tool_use_native(tmp_path):
    setup = (_GOLDEN / "opencode" / "tool_use" / "setup.sql").read_text(encoding="utf-8")
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(setup)
    conn.close()
    t = probe._native_opencode(db)
    assert dict(t.records) == {"session": 1, "message": 2, "part": 3}
    assert (t.user, t.assistant, t.tool_calls, t.tool_results) == (1, 1, 1, 1)
    # tokens: input 500, output 120, cache read 50 + write 10.
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (500, 120, 60)


@pytest.mark.parametrize("case", ["json_format", "jsonl_tool_calls", "jsonl_whole_request", "jsonl_empty_window"])
def test_vscode_native_counts_match_parse_on_real_captures(case):
    """The probe's own reading of a real-shaped capture agrees with the adapter's
    parse(): two independent readings of the same file, so a drift in either shows."""
    path = _golden_input("vscode", case)
    native = probe._native_vscode(path)
    parsed = probe.parsed_tally(vscode.parse(Source(kind="file", location=path)))
    assert (native.user, native.assistant) == (parsed.user, parsed.assistant)


def test_codex_reemitted_token_count_is_not_another_call(tmp_path):
    usage = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 60}
    event = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": usage, "total_token_usage": {"input_tokens": 100}}}}
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [event, event]) + "\n", encoding="utf-8")
    t = probe._native_codex_cli(path)
    assert (t.input_tokens, t.output_tokens, t.cache_tokens) == (100, 10, 60)


def test_aider_multi_conversation_native():
    t = probe._native_aider(_golden_input("aider", "multi_conversation"))
    # Two session headers; three prompt blocks; three assistant text blocks;
    # three tool-output blocks. Tool output is not modelled as tool_results.
    assert (t.user, t.assistant, t.tool_results) == (3, 3, 0)
    assert dict(t.records) == {"session": 2, "tool_block": 3}
    # Tokens: 2.1k/256, 3.2k/512, 4.5k/1.2k.
    assert (t.input_tokens, t.output_tokens) == (9800, 1968)


def test_native_counts_malformed_lines_instead_of_hiding_them(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":"user.message","data":{}}\nnot json\n[1,2]\n', encoding="utf-8")
    t = probe._native_copilot_cli(path)
    assert t.user == 1
    assert t.records["<malformed>"] == 1
    assert t.records["<non-object>"] == 1


# ---------------------------------------------------------------------------
# Parsed side
# ---------------------------------------------------------------------------


def _conversation(responses: list[Response], *, prompts: int = 1) -> Conversation:
    harness = Harness(name="x", source="x", log_format="x")
    conversation = Conversation(external_id="x", harness=harness, started_at="2024-01-01T00:00:00Z")
    for _ in range(prompts):
        conversation.prompts.append(Prompt(timestamp="2024-01-01T00:00:00Z", responses=list(responses)))
    return conversation


def test_parsed_tally_counts_unresulted_calls_and_empty_conversations():
    with_result = ToolCall(tool_name="bash", input={}, result={"content": "ok"})
    without = ToolCall(tool_name="bash", input={})
    response = Response(timestamp="t", usage=Usage(input_tokens=3, output_tokens=4), tool_calls=[with_result, without])
    t = probe.parsed_tally([_conversation([response]), _conversation([], prompts=0)])
    assert (t.conversations, t.conversations_no_prompts, t.conversations_no_responses) == (2, 1, 1)
    assert (t.user, t.assistant, t.tool_calls, t.tool_results, t.tool_calls_no_result) == (1, 1, 2, 1, 1)
    assert (t.input_tokens, t.output_tokens) == (3, 4)


def test_parsed_tally_falls_back_to_aider_approx_attributes():
    """Aider keeps approximate tokens as attributes; the table must not show a phantom Usage gap."""
    response = Response(timestamp="t", attributes={"approx_input_tokens": "2100", "approx_output_tokens": "256"})
    t = probe.parsed_tally([_conversation([response])])
    assert (t.input_tokens, t.output_tokens) == (2100, 256)


def test_parsed_tally_sums_cache_attributes_without_double_counting_codex():
    """Codex writes cached_input_tokens and cache_read_input_tokens; only one is summed."""
    response = Response(
        timestamp="t",
        attributes={"cache_read_input_tokens": "10", "cached_input_tokens": "10", "cache_creation_input_tokens": "5"},
    )
    t = probe.parsed_tally([_conversation([response])])
    assert t.cache_tokens == 15


# ---------------------------------------------------------------------------
# Measurement: deduplication, limits, error capture, output shape
# ---------------------------------------------------------------------------


@dataclass
class _FakeModule:
    """Stands in for an adapter module: discover() yields fixed sources, parse() is real."""

    sources: list
    parse: object
    NAME: str = "fake"

    def discover(self, locations=None):
        yield from self.sources


def test_measure_adapter_deduplicates_by_resolved_path_and_honours_limit():
    pi_file = _golden_input("pi_agent", "minimal")
    source = Source(kind="file", location=pi_file)
    fake = _FakeModule(sources=[source, source, source], parse=pi_agent.parse)
    report = probe.measure_adapter("pi_agent", fake)
    assert report.sources == 1
    assert report.duplicate_yields == 2
    assert report.native.user == 1 and report.parsed.user == 1
    assert report.diff()["user"] == 0


def test_measure_adapter_records_errors_without_stopping():
    missing = Source(kind="file", location=Path("/nonexistent/probe/fixture.jsonl"))
    fake = _FakeModule(sources=[missing], parse=pi_agent.parse)
    report = probe.measure_adapter("pi_agent", fake)
    assert report.sources == 1
    assert report.native_errors == 1
    assert report.parse_errors == 1
    assert len(report.error_samples) == 2


def test_limit_caps_unique_sources(tmp_path):
    original = _golden_input("pi_agent", "minimal")
    copy = tmp_path / "copy.jsonl"
    copy.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    first = Source(kind="file", location=original)
    second = Source(kind="file", location=copy)
    fake = _FakeModule(sources=[first, second], parse=pi_agent.parse)
    report = probe.measure_adapter("pi_agent", fake, limit=1)
    assert report.sources == 1


def test_diff_is_none_for_token_fields_when_format_records_no_usage():
    copilot = _FakeModule(
        sources=[Source(kind="file", location=_golden_input("copilot_cli", "minimal"))], parse=copilot_cli.parse
    )
    report = probe.measure_adapter("copilot_cli", copilot)
    diff = report.diff()
    assert diff["input_tokens"] is None and diff["output_tokens"] is None and diff["cache_tokens"] is None
    assert diff["user"] == 0


def test_render_json_shape_and_text_mentions_every_adapter():
    fake = _FakeModule(
        sources=[Source(kind="file", location=_golden_input("pi_agent", "minimal"))], parse=pi_agent.parse
    )
    report = probe.measure_adapter("pi_agent", fake)
    payload = json.loads(probe.render_json([report]))
    entry = payload["adapters"]["pi_agent"]
    assert set(entry) >= {"sources", "native", "parsed", "diff", "native_errors", "parse_errors"}
    assert entry["diff"]["output_tokens"] == 0
    assert "pi_agent" in probe.render_text([report])


def test_main_rejects_unknown_adapter(capsys):
    with pytest.raises(SystemExit) as excinfo:
        probe.main(["--adapter", "not_an_adapter"])
    assert excinfo.value.code == 2
    assert "unknown adapter" in capsys.readouterr().err


def test_main_rejects_non_positive_limit(capsys):
    with pytest.raises(SystemExit) as excinfo:
        probe.main(["--limit", "0"])
    assert excinfo.value.code == 2
    assert "--limit" in capsys.readouterr().err
