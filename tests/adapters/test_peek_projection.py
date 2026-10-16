"""Peek built from the assembler: the projection's own contract, and claude_code's hooks over it.

Each test that names a defect asserts the corrected behavior and says in its
docstring what the record fold did instead. The hooks under test are built by
:func:`siftd.adapters.peek_projection.make_peek_hooks_from_decoder` over the
real assembler. claude_code's decoder is the real one; the tests that script an
exact record stream use a synthetic decoder, so they pin the projection and not
an adapter.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import ClaudeSession

from siftd.adapters import claude_code, pi_agent
from siftd.adapters.assembly import AssemblyError, assemble, read_jsonl
from siftd.adapters.peek_projection import (
    make_peek_hooks_from_decoder,
    peek_exchanges_from_assembly,
    peek_scan_from_assembly,
)
from siftd.adapters.records import (
    LineageRec,
    PromptRec,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
)
from siftd.adapters.sdk import make_peek_hooks
from siftd.domain import ContentBlock, Harness, Usage
from siftd.domain.source import Source

NAME = "synthetic_claude"
HARNESS = Harness(name=NAME, source="test", log_format="jsonl")


def _claude_hooks():
    """Hooks over claude_code's decoder with its aliases, without the sidecar enrichment of its peek_scan."""
    return make_peek_hooks_from_decoder(
        read=read_jsonl,
        decoder=claude_code.ClaudeDecoder,
        harness=claude_code.HARNESS,
        default_session_for=lambda path: Session(external_id=f"{claude_code.NAME}::{path.stem}"),
        tool_aliases=claude_code.TOOL_ALIASES,
    )


class _Identity:
    """Units are already records: lets a test script an exact record stream."""

    def decode(self, unit: Any) -> list:
        return [unit]


def _hooks_over(records: list):
    """Peek hooks over an exact record stream, as (scan, exchanges, tail)."""
    return make_peek_hooks_from_decoder(
        read=lambda source: records,
        decoder=lambda source: _Identity(),
        harness=HARNESS,
        default_session_for=lambda path: Session(external_id=f"{NAME}::s1"),
    )


def _scripted(tmp_path: Path, records: list):
    """Hooks over an exact record stream, with the assembler's default session."""
    scan, exchanges, _ = _hooks_over(records)
    path = tmp_path / "scripted.jsonl"
    return scan(path), exchanges(path, 10)


def _text(s: str) -> list[ContentBlock]:
    return [ContentBlock(block_type="text", content={"text": s})]


def _write_jsonl(tmp_path: Path, records: list[dict], name: str = "s.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- well-formed sessions: what the projection shows for claude_code ----------


class TestWellFormedSessions:
    @pytest.mark.parametrize(
        "exchanges, tools",
        [(1, []), (3, []), (3, ["Read", "Bash"])],
        ids=["one-plain", "three-plain", "three-with-tools"],
    )
    def test_scan(self, tmp_path, exchanges, tools):
        path = (
            ClaudeSession(tmp_path, exchanges=exchanges, name="s.jsonl", session_id="sess-1", cwd="/p")
            .with_tools(tools)
            .build()
        )
        records = _records(path)
        scan = _claude_hooks()[0](path)
        assert scan is not None
        assert (scan.session_id, scan.workspace_path, scan.exchange_count) == ("sess-1", "/p", exchanges)
        assert scan.model == "claude-3-opus-20240229"
        assert scan.started_at == min(r["timestamp"] for r in records)
        assert scan.last_activity_at == max(r["timestamp"] for r in records)
        assert scan.parent_session_id is None

    @pytest.mark.parametrize(
        "exchanges, tools",
        [(1, []), (3, []), (3, ["Read", "Bash"])],
        ids=["one-plain", "three-plain", "three-with-tools"],
    )
    @pytest.mark.parametrize("last_n", [5, 2, 0])
    def test_exchanges(self, tmp_path, exchanges, tools, last_n):
        path = ClaudeSession(tmp_path, exchanges=exchanges, name="s.jsonl").with_tools(tools).build()
        tokens = [
            (r["message"]["usage"]["input_tokens"], r["message"]["usage"]["output_tokens"])
            for r in _records(path)
            if r["type"] == "assistant"
        ]
        shown = _claude_hooks()[1](path, last_n)
        want = min(max(last_n, 1), exchanges)
        assert [(e.input_tokens, e.output_tokens) for e in shown] == tokens[-want:]
        assert all(e.prompt_text and e.response_text for e in shown)
        assert all(e.tool_calls == [(claude_code.TOOL_ALIASES[t], 1) for t in tools] for e in shown)

    def test_subagent_scan_and_exchanges(self, tmp_path):
        path = (
            ClaudeSession(tmp_path, exchanges=2, name="agent.jsonl", session_id="parent-session-1")
            .with_subagent("sub-agent-1")
            .with_tools(["Read"])
            .build()
        )
        scan = _claude_hooks()[0](path)
        assert scan is not None
        assert scan.session_id == "parent-session-1:sub-agent-1"
        assert scan.parent_session_id == "parent-session-1"
        assert scan.exchange_count == 2
        shown = _claude_hooks()[1](path, 5)
        assert len(shown) == 2 and all(e.tool_calls == [("file.read", 1)] for e in shown)

    def test_tool_calls_and_external_ids_come_from_the_assembled_calls(self, tmp_path):
        path = ClaudeSession(tmp_path, exchanges=1, name="s.jsonl").with_tools(["Read"]).build()
        (ex,) = _claude_hooks()[1](path, 5)
        assert ex.tool_calls == [("file.read", 1)]
        assert ex.tool_use_ids == ["tool-0-Read"]
        assert ex.narrative[-1].block_type == "tool_calls"
        assert ex.narrative[-1].tool_calls[0].tool_name == "file.read"
        assert ex.narrative[-1].tool_calls[0].external_id == "tool-0-Read"
        assert ex.prompt_external_id == "claude_code::u-0"
        assert ex.response_external_ids == ["claude_code::a-0"]

    def test_the_tail_is_the_raw_last_lines(self, tmp_path):
        path = _write_jsonl(tmp_path, [{"n": 1}, {"n": 2}, {"n": 3}])
        assert list(_claude_hooks()[2](path, 2)) == [{"n": 2}, {"n": 3}]

    def test_hook_signatures_match_make_peek_hooks(self, tmp_path):
        path = ClaudeSession(tmp_path, exchanges=1, name="s.jsonl").build()
        old_scan, old_exchanges, old_tail = make_peek_hooks(pi_agent.normalize_record)
        new_scan, new_exchanges, new_tail = _claude_hooks()
        for old, new in ((old_scan, new_scan), (old_exchanges, new_exchanges), (old_tail, new_tail)):
            assert str(inspect.signature(old)) == str(inspect.signature(new))
        assert new_scan(path) is not None


# --- corrected behavior: each test names the defect it replaces --------------


class TestIdentityAndNames:
    def test_subagent_is_parent_colon_agent_not_parent_agent_agent(self, tmp_path):
        """The first projection replaced every ``::`` in the external id, so a subagent showed as ``parent:agent:sub-agent-1``. Today's peek, and so the id prefixes that resolve, show ``parent:sub-agent-1``."""
        scan = _claude_hooks()[0]
        path = (
            ClaudeSession(tmp_path, exchanges=1, name="agent.jsonl", session_id="parent-session-1")
            .with_subagent("sub-agent-1")
            .build()
        )
        result = scan(path)
        assert result is not None
        assert result.session_id == "parent-session-1:sub-agent-1"
        assert result.parent_session_id == "parent-session-1"

    def test_tool_names_are_the_adapter_aliases_in_counts_and_narrative(self, tmp_path):
        """Without the adapter's aliases the projection showed raw names (``Read``) where the record fold, which the adapter's hooks configure with TOOL_ALIASES, showed canonical ones (``file.read``)."""
        path = ClaudeSession(tmp_path, exchanges=1, name="s.jsonl").with_tools(["Read", "Bash"]).build()
        (ex,) = _claude_hooks()[1](path, 5)
        assert ex.tool_calls == [("file.read", 1), ("shell.execute", 1)]
        names = [c.tool_name for c in ex.narrative[-1].tool_calls]
        assert names == ["file.read", "shell.execute"]

    def test_aliases_are_an_explicit_input_and_unmapped_names_stay_raw(self, tmp_path):
        """The assembler keeps raw names, so a hook built without aliases shows them raw; nothing is canonicalized by default."""
        path = ClaudeSession(tmp_path, exchanges=1, name="s.jsonl").with_tools(["Read"]).build()
        _, exchanges, _ = make_peek_hooks_from_decoder(
            read=read_jsonl,
            decoder=claude_code.ClaudeDecoder,
            harness=HARNESS,
            default_session_for=lambda p: Session(external_id=f"{claude_code.NAME}::{p.stem}"),
        )
        (ex,) = exchanges(path, 5)
        assert ex.tool_calls == [("Read", 1)]


class TestCorrections:
    def test_assistant_only_session_is_seen_by_the_scan(self, tmp_path):
        """The record fold's scan returned None for an assistant-only session (it required a user record) while its exchanges listed one exchange."""
        scan, exchanges = _scripted(
            tmp_path,
            [ResponseRec(at="2026-01-01T00:00:01Z", content=_text("hello"), key="m1")],
        )
        assert scan is not None
        assert scan.exchange_count == 1
        assert len(exchanges) == 1
        assert exchanges[0].prompt_text is None
        assert exchanges[0].response_text == "hello"

    def test_leading_response_is_a_promptless_first_exchange_and_scan_agrees(self, tmp_path):
        """The record fold counted a leading assistant through a sentinel (exchange_count == 0 and no user seen), so its count only matched the exchange list by accident. The projection counts the preamble as one exchange, the same count the exchange list shows."""
        scan, exchanges = _scripted(
            tmp_path,
            [
                ResponseRec(at="2026-01-01T00:00:00Z", content=_text("preamble"), key="p1"),
                PromptRec(at="2026-01-01T00:00:01Z", content=_text("question"), key="u1"),
                ResponseRec(at="2026-01-01T00:00:02Z", content=_text("answer"), key="a1"),
            ],
        )
        assert scan is not None and scan.exchange_count == 2 == len(exchanges)
        assert [e.prompt_text for e in exchanges] == [None, "question"]
        assert [e.response_text for e in exchanges] == ["preamble", "answer"]

    def test_pi_style_tool_call_with_no_tool_use_block_is_visible(self, tmp_path):
        """The record fold saw only ``tool_use`` content blocks, so a tool call a decoder keeps as a separate call record (pi's ``toolCall`` block) was invisible in peek."""
        scan, exchanges = _scripted(
            tmp_path,
            [
                PromptRec(at="2026-01-01T00:00:01Z", content=_text("read it"), key="u1"),
                ResponseRec(at="2026-01-01T00:00:02Z", content=_text("reading"), key="a1"),
                ToolCallRec(at="2026-01-01T00:00:02Z", name="file.read", input={"path": "/a/b/c.py"}, call_id="t1"),
                ToolResultRec(at="2026-01-01T00:00:03Z", call_id="t1", result={"content": "x"}),
            ],
        )
        assert scan is not None and scan.exchange_count == 1
        (ex,) = exchanges
        assert ex.tool_calls == [("file.read", 1)]
        assert ex.tool_use_ids == ["t1"]
        assert ex.narrative[-1].block_type == "tool_calls"
        assert ex.narrative[-1].tool_calls[0].input == "/a/b/c.py"
        # The unmatched call still shows its placeholder, after the content, as a matched one does.
        assert ex.response_text == "reading\n[tool: file.read]"

    def test_a_tool_only_response_has_no_response_text(self, tmp_path):
        """Parity with the record fold: a response of only tool placeholders is not shown as response text."""
        _, exchanges = _scripted(
            tmp_path,
            [
                PromptRec(at="2026-01-01T00:00:01Z", content=_text("go"), key="u1"),
                ResponseRec(
                    at="2026-01-01T00:00:02Z",
                    content=[
                        ContentBlock(
                            block_type="tool_use", content={"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
                        )
                    ],
                    key="a1",
                ),
                ToolCallRec(at="2026-01-01T00:00:02Z", name="Bash", input={}, call_id="t1"),
            ],
        )
        assert exchanges[0].response_text is None
        assert exchanges[0].tool_calls == [("Bash", 1)]

    def test_streamed_chunks_of_one_message_count_usage_once(self, tmp_path):
        """The record fold summed usage over every streamed record of a message, double-counting the snapshot each chunk repeats (the class of #97). The projection reads the message's final usage snapshot."""
        scan, exchanges = _scripted(
            tmp_path,
            [
                PromptRec(at="2026-01-01T00:00:01Z", content=_text("go"), key="u1"),
                ResponseRec(at="2026-01-01T00:00:02Z", content=_text("a"), key="m1", usage=Usage(10, 5)),
                ResponseRec(at="2026-01-01T00:00:03Z", content=_text("b"), key="m1", usage=Usage(12, 40)),
            ],
        )
        assert (exchanges[0].input_tokens, exchanges[0].output_tokens) == (12, 40)

    def test_model_is_the_latest_responses_across_prompts(self, tmp_path):
        scan, _ = _scripted(
            tmp_path,
            [
                PromptRec(at="2026-01-01T00:00:01Z", content=_text("one"), key="u1"),
                ResponseRec(at="2026-01-01T00:00:02Z", content=_text("a"), key="a1", model="model-old"),
                PromptRec(at="2026-01-01T00:00:03Z", content=_text("two"), key="u2"),
                ResponseRec(at="2026-01-01T00:00:04Z", content=_text("b"), key="a2", model="model-new"),
            ],
        )
        assert scan is not None and scan.model == "model-new"

    def test_parent_session_comes_from_the_lineage_record(self, tmp_path):
        """Parent identity is the assembler's lineage record, not a per-record session id, and is shaped as a session id.

        A subagent lineage also spells the session ``parent:own-id``, as a subagent is shown everywhere in peek.
        """
        scan, _ = _scripted(
            tmp_path,
            [
                LineageRec(at=None, kind="subagent", parent=f"{NAME}::parent-sid"),
                PromptRec(at="2026-01-01T00:00:01Z", content=_text("go"), key="u1"),
            ],
        )
        assert scan is not None
        assert scan.parent_session_id == "parent-sid"
        assert scan.session_id == "parent-sid:s1"

    def test_thinking_appears_only_when_asked(self, tmp_path):
        records = [
            PromptRec(at="2026-01-01T00:00:01Z", content=_text("go"), key="u1"),
            ResponseRec(
                at="2026-01-01T00:00:02Z",
                content=[
                    ContentBlock(block_type="thinking", content={"type": "thinking", "thinking": "pondering"}),
                    ContentBlock(block_type="text", content={"type": "text", "text": "done"}),
                ],
                key="a1",
            ),
        ]
        _, plain = _scripted(tmp_path, records)
        assert plain[0].response_text == "[thinking]\ndone"
        assert [b.block_type for b in plain[0].narrative] == ["text"]

        _, thinking_exchanges, _ = _hooks_over(records)
        (ex,) = thinking_exchanges(tmp_path / "scripted.jsonl", 10, include_thinking=True)
        assert ex.response_text == "[thinking] pondering\ndone"
        assert [b.block_type for b in ex.narrative] == ["thinking", "text"]

    def test_two_conversations_in_one_file_is_an_error_not_a_guess(self, tmp_path):
        records = [
            Session(external_id=f"{NAME}::one"),
            PromptRec(at="2026-01-01T00:00:01Z", content=_text("a"), key="u1"),
            Session(external_id=f"{NAME}::two"),
            PromptRec(at="2026-01-01T00:00:02Z", content=_text("b"), key="u2"),
        ]
        scan, exchanges, _ = _hooks_over(records)
        with pytest.raises(AssemblyError, match="one conversation per file"):
            scan(tmp_path / "scripted.jsonl")
        with pytest.raises(AssemblyError, match="one conversation per file"):
            exchanges(tmp_path / "scripted.jsonl", 10)

    def test_a_file_with_no_conversation_is_none_and_empty(self, tmp_path):
        scan, exchanges, _ = _hooks_over([Unknown()])
        assert scan(tmp_path / "scripted.jsonl") is None
        assert exchanges(tmp_path / "scripted.jsonl", 10) == []


# --- the pure projection, on an assembly built directly ------------------------


def test_projection_functions_take_an_assembly_directly(tmp_path):
    records = [
        PromptRec(at="2026-01-01T00:00:01Z", content=_text("go"), key="u1"),
        ResponseRec(at="2026-01-01T00:00:02Z", content=_text("ok"), key="a1", model="m"),
    ]
    assembly = assemble(
        Source(kind="file", location=tmp_path / "x.jsonl"),
        read=lambda source: records,
        decoder=lambda source: _Identity(),
        harness=HARNESS,
        default_session=Session(external_id=f"{NAME}::s1"),
    )
    scan = peek_scan_from_assembly(assembly)
    assert scan is not None and (scan.session_id, scan.model, scan.exchange_count) == ("s1", "m", 1)
    (ex,) = peek_exchanges_from_assembly(assembly, 5, include_thinking=False)
    assert (ex.prompt_text, ex.response_text) == ("go", "ok")
