"""The shared assembler places every record and accounts for every one.

Each test names one placement rule from ``siftd.adapters.assembly`` and the
adapter defect it replaces. The last tests are the conservation property: for
any record stream, every record gets exactly one disposition.
"""

from __future__ import annotations

import itertools

import pytest

from siftd.adapters.assembly import (
    END_OF_STREAM,
    Assembler,
    AssemblyError,
    assemble,
    decode,
    read_jsonl,
)
from siftd.adapters.records import (
    Disposition,
    Ignored,
    LineageRec,
    Malformed,
    Meta,
    PatchRec,
    PromptRec,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
    UsageRec,
)
from siftd.domain import ContentBlock, Harness, Lineage, Usage
from siftd.domain.source import Source

H = Harness(name="t", source="test", log_format="jsonl")
S = Session(external_id="t::s1")
DATED = Session(external_id="t::dated", started_at="2026-01-01T00:00:00Z")


def text(s: str) -> list[ContentBlock]:
    return [ContentBlock(block_type="text", content={"text": s})]


def run(*records, default_session=None):
    a = Assembler(H, default_session=default_session)
    for r in records:
        a.feed(r)
    return a.finish()


def only(assembly):
    assert len(assembly.conversations) == 1
    return assembly.conversations[0]


def d(assembly, disposition):
    return assembly.ledger.count(disposition)


# --- the tree ----------------------------------------------------------------


def test_prompt_response_call_result_build_one_exchange():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z", content=text("hi"), external_id="p1"),
        ResponseRec(at="2026-01-01T00:00:01Z", content=text("hello"), external_id="r1", model="m"),
        ToolCallRec(at="2026-01-01T00:00:02Z", name="Bash", input={"command": "ls"}, call_id="c1"),
        ToolResultRec(at="2026-01-01T00:00:03Z", call_id="c1", result={"output": "a"}),
    )
    conv = only(out)
    (prompt,) = conv.prompts
    (response,) = prompt.responses
    (call,) = response.tool_calls
    assert (prompt.external_id, response.external_id, response.model) == ("p1", "r1", "m")
    assert (call.tool_name, call.external_id, call.result, call.status) == ("Bash", "c1", {"output": "a"}, "success")
    assert call.timestamp == "2026-01-01T00:00:03Z"  # completion; the call was made at its response's time
    assert conv.started_at == "2026-01-01T00:00:00Z"
    assert conv.ended_at == "2026-01-01T00:00:03Z"  # the last thing that happened: the tool finishing
    assert d(out, Disposition.EVENT) == 3 and d(out, Disposition.PATCH) == 1


# --- orphans: the #93 decision, made once ------------------------------------


def test_response_before_any_prompt_is_preamble_not_dropped_or_given_a_prompt():
    # Eight adapters dropped this response; two invented an empty prompt for it.
    out = run(
        S,
        ResponseRec(at="2026-01-01T00:00:00Z", content=text("continuing"), external_id="r0"),
        PromptRec(at="2026-01-01T00:00:01Z", content=text("next")),
        ResponseRec(at="2026-01-01T00:00:02Z", content=text("ok")),
    )
    conv = only(out)
    assert [r.external_id for r in conv.preamble] == ["r0"]
    assert len(conv.prompts) == 1 and len(conv.prompts[0].responses) == 1
    assert d(out, Disposition.EVENT) == 3 and out.ledger.synthesized == 0


def test_a_conversation_of_only_preamble_is_kept():
    # The fork-transcript case: a sub-agent file that never writes a prompt.
    conv = only(run(S, ResponseRec(at="2026-01-01T00:00:00Z", content=text("x"))))
    assert conv.prompts == [] and len(conv.preamble) == 1


def test_tool_call_with_no_response_gets_a_counted_container():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z", content=text("go")),
        ToolCallRec(at="2026-01-01T00:00:01Z", name="Read", input={}, call_id="c1"),
        ToolCallRec(at="2026-01-01T00:00:02Z", name="Read", input={}, call_id="c2"),
    )
    (response,) = only(out).prompts[0].responses
    assert [c.external_id for c in response.tool_calls] == ["c1", "c2"]
    assert response.content == [] and out.ledger.synthesized == 1


def test_tool_call_before_any_prompt_lands_in_the_preamble():
    out = run(S, ToolCallRec(at="2026-01-01T00:00:00Z", name="Read", input={}, call_id="c1"))
    conv = only(out)
    assert conv.prompts == [] and conv.preamble[0].tool_calls[0].external_id == "c1"


def test_a_new_prompt_ends_the_previous_response_for_tool_calls():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z", content=text("a")),
        PromptRec(at="2026-01-01T00:00:02Z"),
        ToolCallRec(at="2026-01-01T00:00:03Z", name="Read", input={}),
    )
    first, second = only(out).prompts
    assert first.responses[0].tool_calls == []
    assert len(second.responses) == 1 and second.responses[0].tool_calls[0].tool_name == "Read"


# --- multiplicity: the #97 / #98 decision, made once ---------------------------


def test_chunks_sharing_a_key_merge_and_keep_the_last_usage_snapshot():
    # Claude Code repeats the whole message's usage on every streamed chunk (#97).
    usage = Usage(input_tokens=10, output_tokens=40)
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(
            at="2026-01-01T00:00:01Z",
            key="msg_1",
            external_id="u1",
            content=text("thinking"),
            usage=usage,
            usage_details={"cache_read_input_tokens": 5},
        ),
        ToolCallRec(at="2026-01-01T00:00:01Z", name="Bash", input={}, call_id="c1", parent_key="msg_1"),
        ResponseRec(
            at="2026-01-01T00:00:02Z",
            key="msg_1",
            external_id="u2",
            content=text("done"),
            usage=usage,
            usage_details={"cache_read_input_tokens": 5},
        ),
    )
    (response,) = only(out).prompts[0].responses
    assert response.external_id == "u1" and response.timestamp == "2026-01-01T00:00:01Z"
    assert [b.content["text"] for b in response.content] == ["thinking", "done"]
    assert response.usage == usage
    assert response.attributes["cache_read_input_tokens"] == "5"
    assert len(response.tool_calls) == 1
    assert d(out, Disposition.MERGE) == 1


def test_usage_increments_sum_onto_the_latest_response():
    # Codex reports usage once per API call; last-wins kept ~20% of it (#98).
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z"),
        UsageRec(
            at="2026-01-01T00:00:02Z",
            usage=Usage(input_tokens=100, output_tokens=10),
            usage_details={"cached_input_tokens": 60},
        ),
        UsageRec(
            at="2026-01-01T00:00:03Z",
            usage=Usage(input_tokens=200, output_tokens=20),
            usage_details={"cached_input_tokens": 150},
        ),
    )
    (response,) = only(out).prompts[0].responses
    assert response.usage == Usage(input_tokens=300, output_tokens=30)
    assert response.attributes["cached_input_tokens"] == "210"
    assert d(out, Disposition.PATCH) == 2


def test_usage_before_any_response_waits_for_the_next_one():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        UsageRec(at="2026-01-01T00:00:01Z", usage=Usage(input_tokens=7, output_tokens=1)),
        ResponseRec(at="2026-01-01T00:00:02Z", usage=None),
    )
    assert only(out).prompts[0].responses[0].usage == Usage(input_tokens=7, output_tokens=1)
    assert d(out, Disposition.PATCH) == 1 and d(out, Disposition.ORPHAN) == 0


def test_usage_and_patches_with_nothing_to_attach_to_are_orphans_not_silent():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        UsageRec(at="2026-01-01T00:00:01Z", usage=Usage(input_tokens=7)),
        PatchRec(at="2026-01-01T00:00:01Z", attributes={"duration_ms": "5"}),
    )
    assert d(out, Disposition.ORPHAN) == 2
    assert out.ledger.reasons[(Disposition.ORPHAN, "nothing to attach to")] == 2


def test_patch_targets_a_key_or_the_latest_response():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z", key="a"),
        ResponseRec(at="2026-01-01T00:00:02Z", key="b"),
        PatchRec(at=None, attributes={"x": "1"}, target_key="a"),
        PatchRec(at=None, attributes={"y": "2"}),
    )
    a, b = only(out).prompts[0].responses
    assert a.attributes == {"x": "1"} and b.attributes == {"y": "2"}


# --- pairing -----------------------------------------------------------------


def test_a_result_written_before_its_call_still_pairs():
    # Claude Code writes some parallel tool results before the call record.
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z"),
        ToolResultRec(at="2026-01-01T00:00:02Z", call_id="c1", result={"r": 1}, status="error"),
        ToolCallRec(at="2026-01-01T00:00:03Z", name="Bash", input={}, call_id="c1"),
    )
    (call,) = only(out).prompts[0].responses[0].tool_calls
    assert (call.result, call.status) == ({"r": 1}, "error")
    assert d(out, Disposition.PATCH) == 1 and d(out, Disposition.ORPHAN) == 0


def test_a_result_whose_call_never_appears_is_a_named_orphan():
    out = run(S, PromptRec(at="2026-01-01T00:00:00Z"), ToolResultRec(at=None, call_id="ghost", result={}))
    assert out.ledger.reasons[(Disposition.ORPHAN, "tool result with no call")] == 1


def test_results_pair_with_the_earliest_unresulted_call_of_that_id():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z"),
        ToolCallRec(at=None, name="A", input={}, call_id="dup"),
        ToolCallRec(at=None, name="B", input={}, call_id="dup"),
        ToolResultRec(at=None, call_id="dup", result={"n": 1}),
        ToolResultRec(at=None, call_id="dup", result={"n": 2}),
    )
    a, b = only(out).prompts[0].responses[0].tool_calls
    assert (a.result, b.result) == ({"n": 1}, {"n": 2})


def test_a_call_with_no_result_stays_pending():
    out = run(S, PromptRec(at="2026-01-01T00:00:00Z"), ToolCallRec(at=None, name="A", input={}, call_id="c"))
    assert only(out).prompts[0].responses[0].tool_calls[0].status == "pending"


# --- identity ------------------------------------------------------------------


def test_content_before_the_session_record_belongs_to_it():
    out = run(PromptRec(at="2026-01-01T00:00:00Z"), Session(external_id="t::late", workspace_path="/w"))
    conv = only(out)
    assert conv.external_id == "t::late" and conv.workspace_path == "/w"


def test_a_new_session_id_opens_the_next_conversation():
    out = run(
        Session(external_id="t::a"),
        PromptRec(at="2026-01-01T00:00:00Z"),
        Session(external_id="t::b"),
        PromptRec(at="2026-01-02T00:00:00Z"),
        Session(external_id="t::b", workspace_path="/later"),
    )
    a, b = out.conversations
    assert (a.external_id, b.external_id) == ("t::a", "t::b")
    assert b.workspace_path == "/later" and len(a.prompts) == len(b.prompts) == 1


def test_events_with_no_session_and_no_default_fail_loudly():
    with pytest.raises(AssemblyError, match="no Session"):
        run(PromptRec(at="2026-01-01T00:00:00Z"))


def test_the_default_session_names_a_source_without_one():
    out = run(PromptRec(at="2026-01-01T00:00:00Z"), default_session=Session(external_id="t::file"))
    assert only(out).external_id == "t::file"


def test_a_session_with_no_events_is_counted_not_yielded():
    out = run(S, Meta(workspace_path="/w"))
    assert out.conversations == [] and out.ledger.empty_conversations == 1
    assert d(out, Disposition.META) == 2


def test_meta_workspace_first_wins_and_model_is_context_for_later_responses():
    out = run(
        S,
        Meta(workspace_path="/first", model="m1"),
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z"),
        Meta(workspace_path="/second", model="m2"),
        ResponseRec(at="2026-01-01T00:00:02Z"),
        ResponseRec(at="2026-01-01T00:00:03Z", model="explicit"),
    )
    conv = only(out)
    assert conv.workspace_path == "/first"
    assert [r.model for r in conv.prompts[0].responses] == ["m1", "m2", "explicit"]


def test_lineage_becomes_the_conversations_lineage_and_a_second_parent_is_ignored():
    out = run(
        S,
        LineageRec(at=None, kind="fork", parent="sess-parent", anchor="rec-9"),
        LineageRec(at=None, kind="resume", parent="other"),
        ResponseRec(at="2026-01-01T00:00:00Z"),
    )
    conv = only(out)
    assert conv.lineage == (Lineage(kind="fork", parent="sess-parent", anchor="rec-9"),)
    assert not any(k.startswith("lineage.") for k in conv.attributes)
    assert d(out, Disposition.LINEAGE) == 1
    assert out.ledger.reasons[(Disposition.IGNORED, "conversation already has a parent")] == 1


def test_a_subagent_that_is_also_a_fork_keeps_both_parents():
    """Containment (where it ran) and ancestry (what it continues) are different relations,
    so a forked subagent keeps one link of each, in either arrival order."""
    for order in (("fork", "subagent"), ("subagent", "fork")):
        recs = {
            "fork": LineageRec(at=None, kind="fork", parent="sess-a", anchor="rec-9"),
            "subagent": LineageRec(at=None, kind="subagent", parent="sess-b"),
        }
        out = run(S, *(recs[k] for k in order), ResponseRec(at="2026-01-01T00:00:00Z"))
        conv = only(out)
        assert conv.parent("fork") == Lineage(kind="fork", parent="sess-a", anchor="rec-9")
        assert conv.parent("subagent") == Lineage(kind="subagent", parent="sess-b")
        assert conv.parent("resume") is None
        assert d(out, Disposition.LINEAGE) == 2
        assert out.ledger.reasons.get((Disposition.IGNORED, "conversation already has a parent")) is None


# --- time ----------------------------------------------------------------------


def test_missing_timestamps_come_from_neighbours_never_the_clock():
    records = [
        S,
        PromptRec(at=None, content=text("first, undated")),
        ResponseRec(at="2026-01-01T00:00:05Z"),
        PromptRec(at=None),
        ResponseRec(at=None),
    ]
    first, second = run(*records), run(*records)
    p1, p2 = only(first).prompts
    assert p1.timestamp == "2026-01-01T00:00:05Z"  # from its successor
    assert p2.timestamp == p2.responses[0].timestamp == "2026-01-01T00:00:05Z"  # from its predecessor
    assert only(first) == only(second)


def test_started_at_is_the_earliest_time_not_the_first_record():
    conv = only(run(S, PromptRec(at="2026-01-01T00:00:09Z"), ResponseRec(at="2026-01-01T00:00:01Z")))
    assert (conv.started_at, conv.ended_at) == ("2026-01-01T00:00:01Z", "2026-01-01T00:00:09Z")


def test_a_conversation_with_no_timestamps_uses_the_session_start():
    conv = only(run(Session(external_id="t::x", started_at="2026-02-02T00:00:00Z"), PromptRec(at=None)))
    assert conv.prompts[0].timestamp == conv.started_at == "2026-02-02T00:00:00Z"


# --- review round 1: explicit keys, shrinking snapshots, evidence, interim results ---


def test_an_explicit_target_key_is_never_redirected_to_another_response():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        UsageRec(at=None, usage=Usage(input_tokens=9), target_key="b"),
        ResponseRec(at="2026-01-01T00:00:01Z", key="a"),
        ResponseRec(at="2026-01-01T00:00:02Z", key="b"),
        PatchRec(at=None, attributes={"x": "1"}, target_key="never"),
    )
    a, b = only(out).prompts[0].responses
    assert a.usage is None and b.usage == Usage(input_tokens=9, output_tokens=None)
    assert out.ledger.reasons[(Disposition.ORPHAN, "target key never appeared")] == 1


def test_a_tool_call_naming_an_unseen_parent_waits_for_it():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at="2026-01-01T00:00:01Z", key="other"),
        ToolCallRec(at=None, name="A", input={}, call_id="c", parent_key="later"),
        ResponseRec(at="2026-01-01T00:00:02Z", key="later"),
        ToolCallRec(at=None, name="B", input={}, parent_key="ghost"),
    )
    other, later = only(out).prompts[0].responses
    assert other.tool_calls == [] and [c.tool_name for c in later.tool_calls] == ["A"]
    assert out.ledger.reasons[(Disposition.ORPHAN, "parent key never appeared")] == 1


def test_a_replacement_snapshot_drops_details_it_no_longer_carries_but_keeps_increments():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at=None, key="m", usage=Usage(input_tokens=1), usage_details={"cache_read_input_tokens": 3}),
        UsageRec(at=None, usage=Usage(input_tokens=1), usage_details={"reasoning_output_tokens": 4}, target_key="m"),
        ResponseRec(at=None, key="m", usage=Usage(input_tokens=2)),
    )
    (r,) = only(out).prompts[0].responses
    assert "cache_read_input_tokens" not in r.attributes
    assert r.attributes["reasoning_output_tokens"] == "4" and r.usage.input_tokens == 3


def test_timestamps_from_merged_chunks_and_results_are_evidence():
    out = run(
        S,
        PromptRec(at=None),
        ResponseRec(at=None, key="m"),
        ResponseRec(at="2026-01-01T00:00:05Z", key="m"),
        ToolCallRec(at=None, name="A", input={}, call_id="c"),
        ToolResultRec(at="2026-01-01T00:00:09Z", call_id="c", result={}),
    )
    conv = only(out)
    assert conv.prompts[0].timestamp == "2026-01-01T00:00:05Z"
    assert (conv.started_at, conv.ended_at) == ("2026-01-01T00:00:05Z", "2026-01-01T00:00:09Z")


def test_a_restated_session_supplies_bounds_it_learned_late():
    conv = only(run(S, ResponseRec(at=None), Session(external_id="t::s1", started_at="2026-03-03T00:00:00Z")))
    assert conv.started_at == conv.preamble[0].timestamp == "2026-03-03T00:00:00Z"


def test_default_session_metadata_sits_beneath_the_stream():
    default = Session(external_id="t::d", workspace_path="/w", branch="main", attributes={"a": "1", "b": "1"})
    conv = only(run(Meta(attributes={"b": "2"}), PromptRec(at="2026-01-01T00:00:00Z"), default_session=default))
    assert (conv.external_id, conv.workspace_path, conv.branch) == ("t::d", "/w", "main")
    assert conv.attributes == {"a": "1", "b": "2"}


def test_interim_results_update_the_call_and_leave_it_open():
    # Antigravity: RUNNING output, then the SYSTEM_MESSAGE that completes it.
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ToolResultRec(at=None, call_id="c", result={"out": "early"}, status="pending", final=False),
        ToolCallRec(at=None, name="RUN", input={}, call_id="c"),
        ToolResultRec(at=None, call_id="c", result={"out": "running"}, status="pending", final=False),
        ToolResultRec(at=None, call_id="c", result={"out": "done"}),
    )
    (call,) = only(out).prompts[0].responses[0].tool_calls
    assert (call.result, call.status) == ({"out": "done"}, "success")
    assert d(out, Disposition.PATCH) == 3 and d(out, Disposition.ORPHAN) == 0


# --- review round 2: arrival order, inherited context, unknown ends ----------------


def test_a_call_waiting_for_its_parent_keeps_its_place_in_pairing_order():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at=None, key="b"),
        ToolCallRec(at=None, name="A", input={}, call_id="c", parent_key="a"),
        ToolCallRec(at=None, name="B", input={}, call_id="c", parent_key="b"),
        ToolResultRec(at=None, call_id="c", result={"for": "A"}),
        ResponseRec(at=None, key="a"),
    )
    b, a = only(out).prompts[0].responses
    assert a.tool_calls[0].result == {"for": "A"} and b.tool_calls[0].result is None


def test_lineage_and_waiting_calls_lend_their_time_at_arrival():
    conv = only(run(S, PromptRec(at=None), LineageRec(at="2026-01-01T00:00:04Z", kind="fork", parent="p")))
    assert conv.prompts[0].timestamp == "2026-01-01T00:00:04Z"
    out = run(
        S,
        ToolCallRec(at="2026-01-01T00:00:01Z", name="A", input={}, parent_key="a"),
        PromptRec(at=None),
        ResponseRec(at="2026-01-01T00:00:09Z", key="a"),
    )
    assert only(out).prompts[0].timestamp == "2026-01-01T00:00:01Z"


def test_an_explicit_model_on_a_later_chunk_replaces_inherited_context():
    out = run(
        S,
        Meta(model="old", provider="old"),
        PromptRec(at="2026-01-01T00:00:00Z"),
        ResponseRec(at=None, key="m"),
        ResponseRec(at=None, key="m", model="actual", provider="actual"),
        ResponseRec(at=None, key="n", model="own"),
        ResponseRec(at=None, key="n", model="later"),
    )
    m, n = only(out).prompts[0].responses
    assert (m.model, m.provider) == ("actual", "actual")
    assert n.model == "own"  # an explicit first value is not overwritten


def test_a_session_that_cannot_know_its_end_keeps_it_unset():
    # Aider stamps only session starts; ended_at=None is what makes ingest
    # re-check the session when its file grows.
    rec = Session(external_id="t::aider", started_at="2026-01-01T00:00:00Z", infer_end=False)
    one = only(run(rec, PromptRec(at=None)))
    two = only(run(rec, PromptRec(at=None), PromptRec(at=None)))
    assert one.ended_at is None and two.ended_at is None
    assert only(run(S, PromptRec(at="2026-01-01T00:00:00Z"))).ended_at == "2026-01-01T00:00:00Z"


def test_waiting_records_apply_in_arrival_order_across_queues():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        PatchRec(at=None, attributes={"x": "first"}, target_key="m"),
        PatchRec(at=None, attributes={"x": "second"}),
        ResponseRec(at=None, key="m"),
    )
    assert only(out).prompts[0].responses[0].attributes["x"] == "second"


# --- review round 3 ------------------------------------------------------------------


@pytest.mark.parametrize("result_first", [False, True])
def test_a_result_on_a_call_that_is_never_placed_is_an_orphan_not_a_patch(result_first):
    call = ToolCallRec(at=None, name="A", input={}, call_id="c", parent_key="missing")
    result = ToolResultRec(at=None, call_id="c", result={})
    stream = [result, call] if result_first else [call, result]
    out = run(S, PromptRec(at="2026-01-01T00:00:00Z"), *stream)
    assert only(out).prompts[0].responses == []
    assert out.ledger.reasons[(Disposition.ORPHAN, "parent key never appeared")] == 1
    assert out.ledger.reasons[(Disposition.ORPHAN, "its call was never placed")] == 1
    assert d(out, Disposition.PATCH) == 0


def test_a_result_on_a_call_placed_later_counts_once_it_is_placed():
    out = run(
        S,
        PromptRec(at="2026-01-01T00:00:00Z"),
        ToolCallRec(at=None, name="A", input={}, call_id="c", parent_key="a"),
        ToolResultRec(at=None, call_id="c", result={"ok": 1}),
        ResponseRec(at=None, key="a"),
    )
    assert only(out).prompts[0].responses[0].tool_calls[0].result == {"ok": 1}
    assert d(out, Disposition.PATCH) == 1


def test_an_end_only_session_dates_an_undated_conversation():
    for records in (
        [Session(external_id="t::e", ended_at="2026-05-05T00:00:00Z"), PromptRec(at=None)],
        [Session(external_id="t::e"), PromptRec(at=None), Session(external_id="t::e", ended_at="2026-05-05T00:00:00Z")],
    ):
        assert only(run(*records)).prompts[0].timestamp == "2026-05-05T00:00:00Z"


def test_read_jsonl_counts_a_line_truncated_mid_character(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"t": "p"}\n{"t": "caf\xc3')
    units = list(read_jsonl(Source(kind="file", location=path)))
    assert units[0] == {"t": "p"} and type(units[1]).__name__ == "Malformed"


# --- decoding is total -----------------------------------------------------------


class _Decoder:
    """Decodes ``{"t": ...}`` dicts; returns nothing for anything else."""

    def __init__(self, source=None):
        self.finished = False

    def decode(self, unit):
        if unit.get("t") == "p":
            return [PromptRec(at="2026-01-01T00:00:00Z")]
        if unit.get("t") == "skip":
            return [Ignored(reason="echo")]
        return []

    def finish(self):
        self.finished = True
        return [Ignored(reason="end")]


def test_decoding_turns_silence_and_malformed_units_into_counted_unknowns():
    units = [{"t": "p"}, {"t": "???"}, Malformed(line=3, error="truncated"), {"t": "skip"}]
    pairs = list(decode(units, _Decoder()))
    assert pairs == [
        (0, PromptRec(at="2026-01-01T00:00:00Z")),
        (1, Unknown()),
        (2, Unknown(reason="malformed")),
        (3, Ignored(reason="echo")),
        (END_OF_STREAM, Ignored(reason="end")),
    ]


def test_assemble_reports_units_and_reasons():
    out = assemble(
        Source(kind="file", location="/x"),
        read=lambda _s: [{"t": "p"}, {"t": "???"}, Malformed(line=3, error="bad"), {"t": "skip"}],
        decoder=_Decoder,
        harness=H,
        default_session=S,
    )
    assert out.ledger.units == 4 and out.ledger.records == 5
    assert out.ledger.reasons[(Disposition.UNKNOWN, "unrecognized")] == 1
    assert out.ledger.reasons[(Disposition.UNKNOWN, "malformed")] == 1
    assert out.ledger.reasons[(Disposition.IGNORED, "echo")] == 1


def test_read_jsonl_yields_malformed_lines_instead_of_skipping(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"t": "p"}\n\n{"t": "p"\n[1, 2]\n{"t": "skip"}\n')
    units = list(read_jsonl(Source(kind="file", location=path)))
    assert units[0] == {"t": "p"} and units[3] == {"t": "skip"}
    assert [(u.line, type(u).__name__) for u in units[1:3]] == [(3, "Malformed"), (4, "Malformed")]


def test_a_non_record_from_a_decoder_is_a_loud_bug():
    with pytest.raises(AssemblyError, match="non-record"):
        run(S, {"not": "a record"})


# --- conservation ----------------------------------------------------------------

_ALPHABET = [
    PromptRec(at="2026-01-01T00:00:00Z", key="k"),
    PromptRec(at=None),
    ResponseRec(at="2026-01-01T00:00:01Z", key="m"),
    ResponseRec(at=None),
    ToolCallRec(at=None, name="A", input={}, call_id="c"),
    ToolCallRec(at=None, name="B", input={}),
    ToolResultRec(at=None, call_id="c", result={}),
    ToolResultRec(at=None, call_id="c", result={}, final=False),
    UsageRec(at=None, usage=Usage(input_tokens=1, output_tokens=1)),
    UsageRec(at=None, usage=Usage(input_tokens=1, output_tokens=1), target_key="m"),
    PatchRec(at=None, attributes={"a": "b"}),
    LineageRec(at=None, kind="fork", parent="p"),
    Meta(model="m"),
    Ignored(reason="r"),
    Unknown(),
]


@pytest.mark.parametrize("length", [1, 2, 3])
def test_every_record_gets_exactly_one_disposition(length):
    # Exhaustive over short streams: every ordering of every combination,
    # with and without a leading Session. finish() raises if the ledger is
    # out of balance, so reaching the assertion is most of the test.
    for combo in itertools.product(_ALPHABET, repeat=length):
        # Dated sessions: a conversation with no timestamp anywhere fails
        # loudly by design (test_a_conversation_with_no_timestamps_...).
        for records in ((DATED, *combo), combo):
            out = run(*records, default_session=Session(external_id="t::default", started_at="2026-01-01T00:00:00Z"))
            assert out.ledger.disposed == out.ledger.records == len(records)
            # Every placed increment (1 input token each) is in a stored response.
            stored = sum(
                r.usage.input_tokens or 0
                for c in out.conversations
                for r in [*c.preamble, *(r for p in c.prompts for r in p.responses)]
                if r.usage
            )
            assert stored == out.ledger.dispositions[("UsageRec", Disposition.PATCH)]


def test_tokens_in_equal_tokens_out_across_placements():
    # Usage is conserved whatever arrives first: increments before, between
    # and after responses, a snapshot repeated on merged chunks.
    out = run(
        S,
        UsageRec(at=None, usage=Usage(input_tokens=1, output_tokens=1)),
        PromptRec(at="2026-01-01T00:00:00Z"),
        UsageRec(at=None, usage=Usage(input_tokens=2, output_tokens=2)),
        ResponseRec(at=None, key="m", usage=Usage(input_tokens=10, output_tokens=10)),
        ResponseRec(at=None, key="m", usage=Usage(input_tokens=10, output_tokens=10)),
        UsageRec(at=None, usage=Usage(input_tokens=4, output_tokens=4)),
    )
    conv = only(out)
    total = sum(r.usage.input_tokens for p in conv.prompts for r in p.responses if r.usage)
    total += sum(r.usage.input_tokens for r in conv.preamble if r.usage)
    # The snapshot (10) counts once; increments 1 + 2 + 4 all land.
    assert total == 17


# --- identity that a re-keyed conversation carries ---------------------------------


def test_superseded_keys_a_session_names_carry_to_the_conversation():
    """A decoder that re-keys a conversation names the old key; ingest adopts the stored row only through it."""
    conv = only(run(Session(external_id="t::new", superseded_external_ids=("t::old",)), PromptRec(at="2026-01-01T00:00:00Z")))
    assert conv.superseded_external_ids == ("t::old",)


def test_superseded_keys_from_restated_sessions_accumulate_once_each():
    out = run(
        Session(external_id="t::new", superseded_external_ids=("t::a",)),
        PromptRec(at="2026-01-01T00:00:00Z"),
        Session(external_id="t::new", superseded_external_ids=("t::a", "t::b")),
    )
    assert only(out).superseded_external_ids == ("t::a", "t::b")
