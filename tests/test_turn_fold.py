"""One fold from stored events to turns, and the preamble made visible on every read surface.

Responses that precede every prompt are stored with no parent (a forked
sub-agent's whole transcript, a leading tool announcement) and counted in usage.
The read side used to build turns by walking prompts, so ``show``, ``export``
and the web folio rendered that transcript as empty, and a search hit inside it
had no turn to jump to. These tests pin the round trip the core review asked
for: assemble -> store -> API, with every response and tool call present and in
order, then every surface that shows or counts turns agreeing on them.

The fixtures are the adapters' real-shaped golden cases (a forked Claude Code
sub-agent with a fork+subagent lineage; a session that opens with an assistant
message; a Codex sub-agent), parsed by the real adapters. Ordering and numbering
cases use small hand-built conversations, where the distinctive text and the
equal timestamps are the point.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from conftest import load_golden_input
from painted import Fidelity

from siftd.adapters import claude_code, codex_cli
from siftd.api.conversations import get_conversation, list_conversations
from siftd.api.export import export_document
from siftd.domain import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall
from siftd.storage.sqlite import create_database, ensure_tool_aliases, get_or_create_harness, store_conversation
from siftd.storage.turns import (
    TurnSpan,
    conversation_ids_with_turns,
    event_turn_ordinal,
    exchange_number,
    fold_turns,
    load_turns,
    ordinals_by_event,
    turn_at_exchange,
)

ALL = Fidelity(depth=2, visible=frozenset({"text", "thinking", "tools"}))
ADAPTERS = {"claude_code": claude_code, "codex_cli": codex_cli}
# (adapter, golden case, has a lineage link, prompts)
GOLDEN = [
    ("claude_code", "fork_context_ref", True, 0),
    ("claude_code", "assistant_before_prompt", False, 1),
    ("codex_cli", "preamble_before_prompt", True, 1),
]


def _golden(adapter: str, case: str) -> Conversation:
    (conv,) = ADAPTERS[adapter].parse(load_golden_input(adapter, case))
    assert conv.preamble, f"{adapter}/{case} was chosen for its preamble"
    return conv


def _text(s: str) -> list[ContentBlock]:
    return [ContentBlock(block_type="text", content={"text": s})]


def _resp(key: str, ts: str, *, text: str | None = None, tools: tuple[str, ...] = ()) -> Response:
    return Response(
        timestamp=ts, external_id=key, model="m", content=_text(text or f"answer {key}"),
        tool_calls=[ToolCall(tool_name="Read", input={"file_path": f"/x/{t}.py"}, external_id=t, timestamp=ts) for t in tools],
    )


def _conv(prompts: list[Prompt], preamble: list[Response] | None = None, *, key: str = "c") -> Conversation:
    return Conversation(
        external_id=key, harness=Harness(name="claude_code", source="anthropic", log_format="jsonl"),
        started_at="2024-03-01T10:00:00Z", prompts=prompts, preamble=preamble or [],
    )


def _with_preamble() -> Conversation:
    """A preamble announcing a tool, then two prompts: turns [pre, p1, p2]."""
    return _conv(
        [
            Prompt(timestamp="2024-03-01T10:00:05Z", external_id="p1", content=_text("first question"),
                   responses=[_resp("r1", "2024-03-01T10:00:06Z", tools=("t1",))]),
            Prompt(timestamp="2024-03-01T10:00:07Z", external_id="p2", content=_text("second question"),
                   responses=[_resp("r2", "2024-03-01T10:00:08Z")]),
        ],
        preamble=[_resp("pre", "2024-03-01T09:00:00Z", text="zebra preamble words", tools=("tp",))],
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = create_database(path)
    ensure_tool_aliases(conn, get_or_create_harness(conn, "claude_code", source="anthropic"), {"Read": "file.read"})
    conn.commit()
    yield path, conn
    conn.close()


def _ids(conn, cid: str) -> dict[str, str]:
    return {r["external_id"]: r["id"] for r in conn.execute(
        "SELECT id, external_id FROM events WHERE conversation_id = ?", (cid,))}


def _external(conn, ids: list[str]) -> list[str | None]:
    return [conn.execute("SELECT external_id FROM events WHERE id = ?", (i,)).fetchone()[0] for i in ids]


# ---------------------------------------------------------------------------
# The fold itself
# ---------------------------------------------------------------------------


class TestFold:
    def test_a_turn_is_its_prompt_and_what_hangs_from_it(self):
        spans = fold_turns(
            ["p1", "p2"],
            [("r1", "p1"), ("r2", "p1"), ("r3", "p2")],
            [("t1", "r1"), ("t2", "r3"), ("t3", "r3")],
        )
        assert spans == [
            TurnSpan("p1", ("r1", "r2"), ("t1",)),
            TurnSpan("p2", ("r3",), ("t2", "t3")),
        ]

    def test_responses_with_no_prompt_are_the_first_turn_and_their_calls_ride_along(self):
        spans = fold_turns(["p1"], [("pre", None), ("r1", "p1")], [("tp", "pre")])
        assert spans[0] == TurnSpan(None, ("pre",), ("tp",)) and spans[0].is_preamble
        assert spans[1] == TurnSpan("p1", ("r1",), ())

    def test_a_conversation_of_only_a_preamble_is_one_turn(self):
        assert fold_turns([], [("a", None), ("b", None)]) == [TurnSpan(None, ("a", "b"), ())]

    def test_no_preamble_turn_exists_without_a_preamble(self):
        assert [t.prompt_id for t in fold_turns(["p1", "p2"], [])] == ["p1", "p2"]

    def test_membership_is_the_parent_link_not_the_order_events_arrive_in(self):
        """A response listed before its prompt's turn is still its prompt's."""
        spans = fold_turns(["p1", "p2"], [("r2", "p2"), ("r1", "p1")])
        assert [(t.prompt_id, t.response_ids) for t in spans] == [("p1", ("r1",)), ("p2", ("r2",))]

    def test_event_ids_and_ordinals(self):
        spans = fold_turns(["p1"], [("pre", None), ("r1", "p1")], [("t1", "r1")])
        assert spans[1].event_ids == ("p1", "r1", "t1")
        assert ordinals_by_event(spans) == {"pre": 0, "p1": 1, "r1": 1, "t1": 1}


# ---------------------------------------------------------------------------
# Assemble -> store -> API: nothing stored is missing, and it is in order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("adapter", "case", "has_lineage", "n_prompts"), GOLDEN)
class TestRoundTrip:
    def _stored(self, db, adapter, case):
        path, conn = db
        parsed = _golden(adapter, case)
        cid = store_conversation(conn, parsed, commit=True)
        return parsed, cid, get_conversation(cid, fidelity=ALL, db_path=path)

    def test_every_response_and_tool_call_comes_back_in_recorded_order(self, db, adapter, case, has_lineage, n_prompts):
        _, conn = db
        parsed, cid, detail = self._stored(db, adapter, case)
        responses = [*parsed.preamble, *(r for p in parsed.prompts for r in p.responses)]

        assert [r for t in detail.turns for r in _external(conn, t.response_ids)] == [r.external_id for r in responses]
        assert [c for t in detail.turns for c in _external(conn, t.tool_call_ids)] == [
            c.external_id for r in responses for c in r.tool_calls
        ]
        assert (detail.total_input_tokens, detail.total_output_tokens) == (
            sum(r.usage.input_tokens for r in responses if r.usage),
            sum(r.usage.output_tokens for r in responses if r.usage),
        )

    def test_the_preamble_is_the_first_turn_and_the_prompts_follow(self, db, adapter, case, has_lineage, n_prompts):
        parsed, _, detail = self._stored(db, adapter, case)
        first, *rest = detail.turns
        assert first.preamble and first.prompt_text is None and first.prompt_id is None
        assert first.narrative, "the preamble's text and tool calls are rendered, not skipped"
        assert len(first.response_ids) == len(parsed.preamble)
        assert [t.preamble for t in rest] == [False] * n_prompts
        assert len(detail.turns) == parsed.turn_count == n_prompts + 1

    def test_every_event_maps_to_the_index_of_the_turn_that_shows_it(self, db, adapter, case, has_lineage, n_prompts):
        """Search positions, ``--at-turn`` and the transcript are one numbering."""
        _, conn = db
        _, cid, detail = self._stored(db, adapter, case)
        for index, turn in enumerate(detail.turns):
            for event_id in (turn.prompt_id, *turn.response_ids, *turn.tool_call_ids):
                if event_id is not None:
                    assert event_turn_ordinal(conn, event_id) == index
                    assert exchange_number(conn, event_id) == index + 1

    def test_the_header_names_the_parent_when_there_is_lineage(self, db, adapter, case, has_lineage, n_prompts):
        _, _, detail = self._stored(db, adapter, case)
        first = detail.turns[0]
        if has_lineage:
            assert first.continued_from and "(fork)" in first.continued_from
        else:
            assert first.continued_from is None

    def test_counts_agree_across_surfaces(self, db, adapter, case, has_lineage, n_prompts):
        path, _ = db
        parsed, cid, detail = self._stored(db, adapter, case)
        (summary,) = [s for s in list_conversations(fidelity=ALL, db_path=path) if s.id == cid]
        assert parsed.turn_count == len(detail.turns) == summary.turn_count == len(detail.exchanges)
        assert summary.prompt_count == n_prompts and summary.has_preamble

    def test_the_folio_counts_turns_not_rendered_blocks(self, db, adapter, case, has_lineage, n_prompts):
        """The folio's "Turns N" was the number of user/assistant blocks it drew, so a
        preamble plus one prompt read 2 in the listing and 3 in the folio."""
        import re

        from siftd.output.html_fmt import render_folio

        path, _ = db
        parsed, cid, detail = self._stored(db, adapter, case)
        (summary,) = [s for s in list_conversations(fidelity=ALL, db_path=path) if s.id == cid]
        html = render_folio(detail, ALL)
        assert f'data-count="{summary.turn_count}"' in html
        assert f'<span class="folio__navmeta">{summary.turn_count}</span>' in html
        assert {int(n) for n in re.findall(r'turn-item__n">(\d+)<', html)} <= set(range(1, summary.turn_count + 1))


class TestSynthesizedContainerCalls:
    """The assembler gives a tool call with no response since the last prompt an empty
    response to hold it: stored calls, no content blocks. The narrative only rendered
    calls a ``tool_use`` block named, so these calls survived the round trip as ids and
    vanished from every rendered surface (peek showed them; show, export and the folio
    did not)."""

    @pytest.fixture
    def stored(self, db):
        from siftd.adapters.assembly import Assembler
        from siftd.adapters.records import PromptRec, ResponseRec, Session, ToolCallRec

        path, conn = db
        a = Assembler(Harness(name="claude_code", source="anthropic", log_format="jsonl"))
        for rec in (
            Session(external_id="c::container", started_at="2024-03-01T09:00:00Z"),
            ToolCallRec(at="2024-03-01T09:00:01Z", name="Bash", input={"command": "ls"}, call_id="lead1"),
            ToolCallRec(at="2024-03-01T09:00:02Z", name="Read", input={"file_path": "/x/lead2.py"}, call_id="lead2"),
            PromptRec(at="2024-03-01T09:00:03Z", key="p1", content=_text("hello")),
            ToolCallRec(at="2024-03-01T09:00:04Z", name="Grep", input={"pattern": "x"}, call_id="mid1"),
            ResponseRec(at="2024-03-01T09:00:05Z", key="r1", content=_text("done")),
        ):
            a.feed(rec)
        (parsed,) = a.finish().conversations
        assert a.ledger.synthesized == 2, "the shape under test: two synthesized containers"
        cid = store_conversation(conn, parsed, commit=True)
        return path, cid, get_conversation(cid, fidelity=ALL, db_path=path)

    @staticmethod
    def _names(turn):
        return [t.tool_name for b in turn.narrative if b.block_type == "tool_calls" for t in b.tool_calls]

    def test_the_narrative_renders_every_stored_call_in_order(self, stored):
        _, _, detail = stored
        pre, turn = detail.turns
        assert self._names(pre) == ["Bash", "file.read"] and len(pre.tool_call_ids) == 2
        # the call that precedes the prompt's real response rides in that turn, then the answer
        assert self._names(turn) == ["Grep"]
        assert [b.block_type for b in turn.narrative] == ["tool_calls", "text"]

    def test_markdown_show_export_and_folio_carry_them(self, stored):
        path, cid, detail = stored
        from siftd.output.format_registry import get_format
        from siftd.output.html_fmt import render_folio

        shown = get_format("markdown").render_detail(detail, ALL)
        exported = export_document(fidelity=ALL, format="md", id=[cid], db_path=path).content
        for text in (shown, exported):
            assert all(f"**{n}**" in text for n in ("Bash", "file.read", "Grep"))
        html = render_folio(detail, ALL)
        for name in ("Bash", "file.read", "Grep"):
            assert name in html

    def test_a_filter_still_drops_them(self, stored):
        path, cid, _ = stored
        detail = get_conversation(cid, fidelity=ALL, db_path=path, tool_filter="file.read")
        assert [self._names(t) for t in detail.turns] == [["file.read"], []]


class TestEverySurfaceShowsThePreamble:
    @pytest.fixture
    def fork(self, db):
        path, conn = db
        parsed = _golden("claude_code", "fork_context_ref")
        cid = store_conversation(conn, parsed, commit=True)
        return path, cid, parsed, get_conversation(cid, fidelity=ALL, db_path=path)

    def test_markdown_show_and_export(self, fork):
        path, cid, parsed, detail = fork
        from siftd.output.format_registry import get_format

        shown = get_format("markdown").render_detail(detail, ALL)
        assert "*Continued from claude_code::06194393-48f0-4e17-aee4-c6b228547a1c (fork)*" in shown
        assert "Assistant" in shown
        for tool in ("Agent", "Bash"):  # the two calls the fork made
            assert f"**{tool}**" in shown

        exported = export_document(fidelity=ALL, format="md", id=[cid], db_path=path)
        assert "Continued from" in exported.content and "**Bash**" in exported.content

    def test_json_show_and_export(self, fork):
        path, cid, parsed, detail = fork
        exported = json.loads(export_document(fidelity=ALL, format="json", id=[cid], db_path=path).content)
        (conv,) = exported if isinstance(exported, list) else [exported]
        (turn,) = conv["turns"]
        assert turn["preamble"] is True and turn["prompt"] is None and "prompt_id" not in turn
        assert len(turn["response_ids"]) == len(parsed.preamble)
        assert len(turn["tool_call_ids"]) == 2
        assert turn["continued_from"].endswith("(fork)")

    def test_terminal(self, fork):
        _, _, _, detail = fork
        from siftd.output.painted_bridge import render_query_detail_block

        block = render_query_detail_block(detail, turns=detail.turns, fidelity=ALL)
        text = "\n".join("".join(c.char for c in block.row(y)) for y in range(block.height))
        assert "Continued from claude_code::06194393" in text

    def test_html_transcript_and_folio(self, fork):
        _, _, _, detail = fork
        from siftd.output.html_fmt import render_detail, render_folio

        for html in (render_folio(detail, ALL), render_detail(detail, ALL)):
            assert 'class="turn__preamble"' in html
            assert "Continued from claude_code::06194393" in html
        assert "Assistant" in render_folio(detail, ALL)

    def test_the_live_folio_heads_the_preamble_as_the_stored_one_does(self, fork):
        """``/follow`` renders the folio from peek's exchanges, which carried neither the
        preamble flag nor its lineage: the same fork read live had no "Continued from"
        header, unlike its stored transcript."""
        _, _, parsed, detail = fork
        from siftd.output.html_fmt import folio_detail_from_session, render_folio
        from siftd.domain.peek import SessionInfo, SessionDetail

        source = load_golden_input("claude_code", "fork_context_ref")
        exchanges = claude_code.peek_exchanges(source.location, last_n=5)
        (first,) = exchanges
        assert first.preamble and first.continued_from == "claude_code::06194393-48f0-4e17-aee4-c6b228547a1c (fork)"

        live = SessionDetail(info=SessionInfo(session_id="s", file_path=source.location), exchanges=exchanges)
        html = render_folio(folio_detail_from_session(live), ALL)
        assert 'class="turn__preamble"' in html
        assert "Continued from claude_code::06194393-48f0-4e17-aee4-c6b228547a1c (fork)" in html
        assert "Continued from claude_code::06194393" in render_folio(detail, ALL)  # the stored header, same words

    def test_a_live_preamble_without_lineage_gets_the_neutral_header(self):
        from siftd.output.html_fmt import folio_detail_from_session, render_folio
        from siftd.domain.peek import PeekExchange, PeekNarrativeBlock, SessionDetail, SessionInfo

        ex = PeekExchange(preamble=True, narrative=[PeekNarrativeBlock(block_type="text", content="hello")])
        live = SessionDetail(info=SessionInfo(session_id="s", file_path=Path("s.jsonl")), exchanges=[ex])
        html = render_folio(folio_detail_from_session(live), ALL)
        assert "Before the first prompt" in html and "Continued from" not in html

    def test_the_wire_round_trip_keeps_the_preamble(self, fork):
        _, _, _, detail = fork
        from siftd.api.deserialize import deserialize_conversation_detail
        from siftd.serialization.conversations import serialize_conversation_detail

        back = deserialize_conversation_detail({"conversation": serialize_conversation_detail(detail)})
        assert back is not None
        assert [(t.preamble, t.continued_from) for t in back.turns] == [(True, detail.turns[0].continued_from)]

    def test_listing_summary_wire_carries_has_preamble_only_when_true(self, db):
        path, conn = db
        store_conversation(conn, _with_preamble(), commit=True)
        store_conversation(conn, _conv([Prompt(timestamp="2024-03-01T10:00:05Z", external_id="p", content=_text("q"),
                                               responses=[_resp("r", "2024-03-01T10:00:06Z")])], key="plain"), commit=True)
        from siftd.api.deserialize import deserialize_conversation_summary
        from siftd.serialization.conversations import serialize_conversation_summary

        summaries = {s.external_id: s for s in list_conversations(fidelity=ALL, db_path=path)}
        assert summaries["c"].turn_count == 3 and summaries["plain"].turn_count == 1
        wire = {k: serialize_conversation_summary(s) for k, s in summaries.items()}
        assert wire["c"]["preamble"] is True and "preamble" not in wire["plain"]
        assert deserialize_conversation_summary(wire["c"]).turn_count == 3

    def test_a_neutral_header_when_there_is_no_lineage(self, db):
        path, conn = db
        cid = store_conversation(conn, _with_preamble(), commit=True)
        from siftd.output.format_registry import get_format

        shown = get_format("markdown").render_detail(get_conversation(cid, fidelity=ALL, db_path=path), ALL)
        assert "*Before the first prompt*" in shown and "Continued from" not in shown

    def test_a_stored_parent_is_named_by_its_short_id(self, db):
        path, conn = db
        parsed = _golden("claude_code", "fork_context_ref")
        parent_external = "claude_code::06194393-48f0-4e17-aee4-c6b228547a1c"
        parent = store_conversation(conn, _conv([], key=parent_external), commit=True)
        child = store_conversation(conn, parsed, commit=True)
        detail = get_conversation(child, fidelity=ALL, db_path=path)
        assert detail.turns[0].continued_from == f"{parent[:12]} (fork)"


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------


class TestNumbering:
    def test_a_preamble_is_turn_zero_and_shifts_every_prompt_by_one(self, db):
        path, conn = db
        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        detail = get_conversation(cid, fidelity=ALL, db_path=path)
        assert [t.prompt_text for t in detail.turns] == [None, "first question", "second question"]
        assert [event_turn_ordinal(conn, ids[k]) for k in ("pre", "tp", "p1", "r1", "t1", "p2", "r2")] == [0, 0, 1, 1, 1, 2, 2]

        at = get_conversation(cid, fidelity=ALL, db_path=path, anchor="at_turn", anchor_value=1)
        assert [t.prompt_text for t in at.turns] == ["first question"]
        around = get_conversation(cid, fidelity=ALL, db_path=path, anchor="at_turn", anchor_value=0,
                                  window_start=0, window_end=1)
        assert [t.prompt_text for t in around.turns] == [None, "first question"]

    def test_around_a_phrase_in_the_preamble_lands_on_it(self, db):
        path, conn = db
        cid = store_conversation(conn, _with_preamble(), commit=True)
        detail = get_conversation(cid, fidelity=ALL, db_path=path, anchor="around", anchor_value="zebra")
        assert [t.preamble for t in detail.turns] == [True]

    def test_exchange_numbers_are_turn_numbers_and_prompts_anchor_exchange_tags(self, db):
        _, conn = db
        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        assert turn_at_exchange(conn, cid, 1).prompt_id is None
        assert turn_at_exchange(conn, cid, 2).prompt_id == ids["p1"]
        assert turn_at_exchange(conn, cid, 3).prompt_id == ids["p2"]
        assert turn_at_exchange(conn, cid, 4) is None
        with pytest.raises(ValueError, match=">= 1"):
            turn_at_exchange(conn, cid, 0)

    def test_colon_paths_resolve_and_alias_through_the_same_numbering(self, db):
        _, conn = db
        from siftd.api.target_ref import TargetRef, alias, resolve

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        got = resolve(conn, TargetRef.from_colon_path(f"{cid}:exchange:2"))
        assert (got.target_kind, got.target_id) == ("exchange", ids["p1"])
        assert alias(conn, "exchange", ids["p1"]) == f"{cid[:12]}:exchange:2"
        with pytest.raises(IndexError, match="before the first prompt"):
            resolve(conn, TargetRef.from_colon_path(f"{cid}:exchange:1"))
        # a prompt is addressed by kind: the first prompt is still prompt 1
        assert resolve(conn, TargetRef.from_colon_path(f"{cid}:prompt:1")).target_id == ids["p1"]

    def test_a_pending_exchange_tag_resolves_by_turn_and_refuses_the_promptless_turn(self, db):
        _, conn = db
        from siftd.storage.sessions import _resolve_pending_target

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)

        def row(n):
            return {"last_marker": None, "entity_type": "exchange", "exchange_index": n}

        assert _resolve_pending_target(conn, row(2), cid) == ("exchange", ids["p1"])
        assert _resolve_pending_target(conn, row(3), cid) == ("exchange", ids["p2"])
        assert "before the first prompt" in _resolve_pending_target(conn, row(1), cid)
        assert "not found" in _resolve_pending_target(conn, row(4), cid)

    def test_equal_timestamps_number_turns_by_recorded_sequence(self, db):
        """Every event at one instant: only ``sequence`` can say which turn is which, end to end."""
        path, conn = db
        same = "2024-03-01T10:00:00Z"
        n = 9  # gcd(7, 9) = 1, so the scramble below is a bijection
        cid = store_conversation(conn, _conv([
            Prompt(timestamp=same, external_id=f"p{i}", content=_text(f"question {i}"),
                   responses=[_resp(f"r{i}", same, tools=(f"t{i}",))])
            for i in range(n)
        ]), commit=True)
        # re-record the exchanges out of insertion order: [prompt, response, call] blocks of 3
        conn.execute("UPDATE events SET sequence = 3 * ((7 * (sequence / 3)) % ?) + sequence % 3 "
                     "WHERE conversation_id = ?", (n, cid))
        conn.commit()
        order = sorted(range(n), key=lambda i: (7 * i) % n)

        detail = get_conversation(cid, fidelity=ALL, db_path=path)
        assert [t.prompt_text for t in detail.turns] == [f"question {i}" for i in order]
        ids = _ids(conn, cid)
        for position, i in enumerate(order):
            assert event_turn_ordinal(conn, ids[f"r{i}"]) == position
            assert event_turn_ordinal(conn, ids[f"t{i}"]) == position
            at = get_conversation(cid, fidelity=ALL, db_path=path, anchor="at_turn", anchor_value=position)
            assert [t.prompt_text for t in at.turns] == [f"question {i}"]

    def test_conversations_without_a_preamble_number_exactly_as_the_old_definition(self, db):
        """The old definition: a turn's index is its prompt's position in recorded order.

        Random shapes (0..7 prompts, each with 0..3 responses of 0..2 calls, tied
        and distinct timestamps) against a positional oracle that walks events in
        ``sequence`` order and counts prompts as it goes, which is what search
        positions, ``--at-turn`` and ``exchange N`` all computed before the fold.
        """
        _, conn = db
        checked = 0
        for seed in range(40):
            rng = random.Random(seed)
            tied = rng.random() < 0.5
            prompts = []
            for p in range(rng.randint(1, 7)):
                ts = "2024-03-01T10:00:00Z" if tied else f"2024-03-01T10:{p:02d}:00Z"
                prompts.append(Prompt(timestamp=ts, external_id=f"p{p}", content=_text(f"q{p}"), responses=[
                    _resp(f"r{p}.{r}", ts, tools=tuple(f"t{p}.{r}.{c}" for c in range(rng.randint(0, 2))))
                    for r in range(rng.randint(0, 3))
                ]))
            cid = store_conversation(conn, _conv(prompts, key=f"shape-{seed}"), commit=True)

            oracle: dict[str, int] = {}
            seen = -1
            for row in conn.execute("SELECT id, kind FROM events WHERE conversation_id = ? ORDER BY sequence", (cid,)):
                seen += row["kind"] == "prompt"
                oracle[row["id"]] = seen
            turns = load_turns(conn, cid)
            assert not any(t.is_preamble for t in turns)
            assert ordinals_by_event(turns) == oracle, f"seed {seed}"
            checked += len(oracle)
        assert checked > 200  # the shapes exercised something


# ---------------------------------------------------------------------------
# Search, windows, file references, embeddings, doctor
# ---------------------------------------------------------------------------


class TestSearchAndIndexSeeThePreamble:
    def test_search_positions_for_events_in_and_after_the_preamble(self, db):
        _, conn = db
        from siftd.api.search import _annotate_turn_positions, _events_to_turn_indices
        from siftd.domain import SearchChunk

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        assert _events_to_turn_indices(conn, [ids["pre"], ids["p1"], ids["r2"], "nope"], cid) == [0, 1, 2, None]

        hit = SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="response", event_id=ids["tp"])
        windowed = SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="exchange",
                               source_ids=[ids["pre"], ids["tp"]])
        _annotate_turn_positions(conn, [hit, windowed])
        assert hit.turn_index == 0 and windowed.turn_index == 0 and windowed.event_id == ids["pre"]

    def test_around_window_and_exchanges_include_the_preamble_turn(self, db):
        _, conn = db
        from siftd.api.search import enrich_around_window, enrich_context_window, enrich_exchanges
        from siftd.domain import SearchChunk

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)

        chunk = SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="exchange")
        kept, skipped = enrich_around_window(conn, [chunk], "zebra", 0, 1)
        assert skipped == 0 and kept[0].turn_index == 0
        assert [(pt, is_match) for _, pt, _, is_match in kept[0].context_window] == [("", True), ("first question", False)]
        assert kept[0].context_window[0][0] is None  # no prompt to name

        only_response = SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="response", source_ids=[ids["pre"]])
        enrich_exchanges(conn, [only_response])
        assert only_response.exchanges == [(None, "", "zebra preamble words")]
        enrich_context_window(conn, [only_response], 1)
        assert [m for *_, m in only_response.context_window] == [True, False]

    def test_file_references_of_the_preamble_are_found_by_turn(self, db):
        _, conn = db
        from siftd.api.file_refs import fetch_file_refs, fetch_turn_file_refs
        from siftd.api.search import enrich_file_refs
        from siftd.domain import SearchChunk

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        by_turn = fetch_turn_file_refs(conn, cid, [0, 1, 2, 9])
        assert {o: [r.basename for r in refs] for o, refs in by_turn.items() if o != 9} == {0: ["tp.py"], 1: ["t1.py"], 2: []}
        assert 9 not in by_turn
        assert [r.basename for r in fetch_file_refs(conn, [ids["p1"], ids["pre"]])[ids["p1"]]] == ["t1.py"]

        chunk = SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="response", source_ids=[ids["pre"], ids["tp"]])
        enrich_file_refs(conn, [chunk])
        assert [r.basename for r in chunk.file_refs] == ["tp.py"]  # one turn, counted once

    def test_several_hits_in_one_conversation_fold_it_once(self, db, monkeypatch):
        """Enrichment used to fold and read the whole transcript once per hit (and again
        per fetch); N hits from one long conversation cost N+ full reads. One fold per
        conversation, however many chunks matched, with each chunk still getting exactly
        its own turns."""
        _, conn = db
        from siftd.api import file_refs as file_refs_mod
        from siftd.api import search as search_mod
        from siftd.domain import SearchChunk
        from siftd.storage import queries as queries_mod

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        other = store_conversation(conn, _conv([], preamble=[_resp("o", "2024-03-01T09:00:00Z")], key="other"), commit=True)

        loads: list[str] = []
        real = search_mod.load_turns

        def counting(c, conv_id):
            loads.append(conv_id)
            return real(c, conv_id)

        for mod in (search_mod, queries_mod, file_refs_mod):
            monkeypatch.setattr(mod, "load_turns", counting)

        def chunks():
            return [
                SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="response", source_ids=[ids["pre"]]),
                SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="response", source_ids=[ids["p1"], ids["r2"]]),
                SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="response", source_ids=[ids["p2"]]),
                SearchChunk(conversation_id=other, score=1.0, text="t", chunk_type="response", source_ids=[]),
            ]

        got = chunks()
        search_mod.enrich_exchanges(conn, got)
        assert loads.count(cid) == 1
        assert [[pt for _, pt, _ in c.exchanges] for c in got[:3]] == [
            [""], ["first question", "second question"], ["second question"],
        ]
        assert got[3].exchanges is None  # untouched: nothing to cover

        loads.clear()
        got = chunks()
        search_mod.enrich_file_refs(conn, got)
        assert loads.count(cid) == 1
        assert [[r.basename for r in c.file_refs] for c in got[:3]] == [["tp.py"], ["t1.py"], []]

        loads.clear()
        got = chunks()
        search_mod.enrich_context_window(conn, got, 0)
        assert loads.count(cid) == 1
        assert [[m for *_, m in c.context_window] for c in got[:3]] == [[True], [True, True], [True]]

        loads.clear()
        same = [SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="exchange") for _ in range(3)]
        kept, skipped = search_mod.enrich_around_window(conn, same, "zebra", 0, 1)
        assert (len(kept), skipped) == (3, 0)
        assert loads.count(cid) == 1

    def test_the_chunker_emits_the_preamble_as_a_promptless_exchange(self, db):
        _, conn = db
        from siftd.embeddings.chunker import extract_exchange_window_chunks, extract_typed_exchange_chunks
        from siftd.storage.queries import fetch_conversation_exchanges

        cid = store_conversation(conn, _with_preamble(), commit=True)
        ids = _ids(conn, cid)
        first, *rest = fetch_conversation_exchanges(conn, conversation_id=cid)[cid]
        assert first["prompt_id"] is None and first["event_ids"] == [ids["pre"]]
        assert "zebra preamble words" in first["text"]
        assert [e["prompt_id"] for e in rest] == [ids["p1"], ids["p2"]]

        windows = extract_exchange_window_chunks(conn, conversation_id=cid)
        assert ids["pre"] in windows[0]["source_ids"]
        typed = [c for c in extract_typed_exchange_chunks(conn, conversation_id=cid) if c["text"].startswith("zebra")]
        assert [(c["chunk_type"], c["source_ids"]) for c in typed] == [("response", [ids["pre"]])]

    def test_a_conversation_of_only_a_preamble_has_a_turn_to_index(self, db):
        _, conn = db
        only = store_conversation(conn, _conv([], preamble=[_resp("pre", "2024-03-01T09:00:00Z")], key="only"), commit=True)
        empty = store_conversation(conn, _conv([], key="empty"), commit=True)
        assert conversation_ids_with_turns(conn) == {only}
        assert empty not in conversation_ids_with_turns(conn)

    def test_the_event_tree_has_the_preamble(self, db):
        _, conn = db
        from siftd.storage.events import get_event_tree

        cid = store_conversation(conn, _with_preamble(), commit=True)
        tree = get_event_tree(conn, cid)
        assert _external(conn, [r["id"] for r in tree["preamble"]]) == ["pre"]
        assert [t["tool_calls"][0]["external_id"] for t in tree["preamble"]] == ["tp"]
        assert [p["external_id"] for p in tree["prompts"]] == ["p1", "p2"]


def test_the_golden_cases_are_where_these_tests_expect_them():
    for adapter, case, _, _ in GOLDEN:
        assert (Path("tests/fixtures/adapters") / adapter / case / "expected.json").exists()
