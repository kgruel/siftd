"""Unit tests for the Swiss transcript folio renderer (output/html_fmt.render_folio).

Base lane (no litestar): renders real ConversationDetail/Turn dataclasses, so a
passing assertion proves the fragment is built from data get_conversation
already returns — no new data surface. The route wiring is covered separately
in the serve lane (test_serve_swiss_shell.py).
"""

from __future__ import annotations

from painted import Fidelity

from siftd.api.conversations import (
    ConversationDetail,
    NarrativeBlock,
    ToolCallDetail,
    ToolCallSummary,
    Turn,
)
from siftd.domain.search_types import SearchView
from siftd.output.html_fmt import render_folio, render_search, render_search_context

_FID = Fidelity(depth=2, visible=frozenset({"text"}), chars=0)
# Trace mode needs a tools/thinking-visible fidelity (the route resolves this);
# walk_narrative keys inline tool_content off fidelity.shows("tools").
_FID_TRACE = Fidelity(depth=3, visible=frozenset({"text", "tools", "thinking"}), chars=0)


def _trace_detail() -> ConversationDetail:
    """One exchange whose assistant turn interleaves prose → tool call → prose."""
    turn = Turn(
        timestamp="2026-03-15T10:30:00Z",
        prompt_text="run the thing",
        total_input_tokens=10,
        total_output_tokens=5,
        narrative=[
            NarrativeBlock(block_type="thinking", content="weighing the options"),
            NarrativeBlock(block_type="text", content="before the tool"),
            NarrativeBlock(
                block_type="tool_calls",
                tool_calls=[ToolCallDetail(
                    tool_name="Read", status="success",
                    input="path/x.py", result="file contents",
                )],
            ),
            NarrativeBlock(block_type="text", content="after the tool"),
        ],
        _tool_call_summaries=[ToolCallSummary("Read", "success", 1)],
    )
    return ConversationDetail(
        id="01TRACE00000000000",
        workspace_path="/proj",
        model="claude-opus",
        started_at="2026-03-15T10:30:00Z",
        total_input_tokens=10,
        total_output_tokens=5,
        turns=[turn],
    )


def _detail() -> ConversationDetail:
    """Two turns; Read appears in both (×2 then ×1 → ledger total 3), Bash once."""
    t1 = Turn(
        timestamp="2026-03-15T10:30:00Z",
        prompt_text="hello <b>danger</b>",
        total_input_tokens=10,
        total_output_tokens=5,
        narrative=[NarrativeBlock(block_type="text", content="first answer")],
        _tool_call_summaries=[ToolCallSummary("Read", "success", 2)],
    )
    t2 = Turn(
        timestamp="2026-03-15T10:31:00Z",
        prompt_text="again",
        total_input_tokens=20,
        total_output_tokens=8,
        narrative=[NarrativeBlock(block_type="text", content="second answer")],
        _tool_call_summaries=[
            ToolCallSummary("Bash", "success", 1),
            ToolCallSummary("Read", "success", 1),
        ],
    )
    return ConversationDetail(
        id="01ABCDEF0123456789",
        workspace_path="/proj",
        model="claude-opus",
        started_at="2026-03-15T10:30:00Z",
        total_input_tokens=30,
        total_output_tokens=13,
        turns=[t1, t2],
    )


def test_folio_three_regions_and_head_metadata():
    html = render_folio(_detail(), _FID)
    # Three CSS-grid regions.
    assert 'class="folio"' in html
    assert 'class="folio__nav"' in html
    assert 'class="folio__body"' in html
    assert 'class="folio__ledger"' in html
    # Head metadata for enhance.js (chrome head + active nav follow the swap).
    assert 'data-view="transcript"' in html
    assert 'data-title="Transcript"' in html
    # Count is rail items: 2 exchanges × (user + assistant) = 4 turns.
    assert 'data-count="4"' in html


def test_folio_rail_has_user_and_assistant_items():
    html = render_folio(_detail(), _FID)
    # 2 turns, each with prompt + response → 4 rail items.
    assert html.count('class="turn-item"') == 4
    assert 'data-role="user"' in html
    assert 'data-role="assistant"' in html
    # Body turns carry anchors the rail links into.
    assert 'id="t-1"' in html and 'href="#t-1"' in html


def test_folio_ledger_counts_tools_across_turns_descending():
    html = render_folio(_detail(), _FID)
    # Read = 2 + 1 = 3 (rendered before Bash = 1).
    assert 'data-n="3"' in html
    assert 'data-n="1"' in html
    assert html.index('data-n="3"') < html.index('data-n="1"')
    # Tool total = 4 lives in the ledger header navmeta (the foot now shows cost).
    assert '<span class="micro">Tool ledger</span><span class="folio__navmeta">4</span>' in html
    assert "tool-call" not in html  # no inline tool I/O in the folio body


def test_folio_turn_tools_chip_and_token_total():
    html = render_folio(_detail(), _FID)
    assert 'class="turn__tools"' in html
    assert "Read" in html and "&times;2" in html  # collapsed count surfaces
    # Token foot = 30 + 13 = 43 (fmt_tokens passes small values through).
    assert "43" in html


def test_folio_ledger_foot_shows_cost_when_known():
    detail = _detail()
    detail.cost = 1.2345  # rollup's canonical per-conversation cost
    html = render_folio(detail, _FID)
    assert '<span class="micro">Cost</span>' in html
    assert "$1.2345" in html


def test_folio_ledger_foot_shows_dash_when_cost_unknown():
    # _detail() leaves cost=None (no priced usage) — render an em dash, never $0.
    html = render_folio(_detail(), _FID)
    assert '<span class="micro">Cost</span>' in html
    assert "&mdash;" in html
    assert "$0.00" not in html


def test_folio_escapes_user_prompt():
    html = render_folio(_detail(), _FID)
    assert "&lt;b&gt;danger" in html
    assert "<b>danger</b>" not in html


def test_folio_renders_user_prompt_markdown():
    # A spawned sub-agent's "user" turn is an orchestration-authored markdown
    # document (headers, lists, fenced code), so user prompts render as markdown
    # like the assistant narrative — not as escaped plaintext. Raw HTML stays
    # neutralized by mistune escape=True, so the new path opens no XSS hole.
    turn = Turn(
        timestamp="2026-03-15T10:30:00Z",
        prompt_text=(
            "## Task\n\n- step one\n- step two\n\n"
            "```py\nprint(1)\n```\n\n<script>alert(1)</script>"
        ),
        total_input_tokens=10,
        total_output_tokens=0,
        narrative=[],  # no assistant turn → any markdown must come from the prompt
        _tool_call_summaries=[],
    )
    detail = ConversationDetail(
        id="01SUBAGENT00000000",
        workspace_path="/proj",
        model="claude-opus",
        started_at="2026-03-15T10:30:00Z",
        total_input_tokens=10,
        total_output_tokens=0,
        turns=[turn],
    )
    html = render_folio(detail, _FID)
    assert "<h2>Task</h2>" in html
    assert "<li>step one</li>" in html
    assert 'class="language-py"' in html  # fenced code → Prism-highlightable
    # Markdown rendering did not bypass HTML escaping.
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_folio_empty_conversation_renders_without_crash():
    detail = ConversationDetail(
        id="01EMPTY00000000000",
        workspace_path=None,
        model=None,
        started_at=None,
        total_input_tokens=0,
        total_output_tokens=0,
        turns=[],
    )
    html = render_folio(detail, _FID)
    assert 'class="folio"' in html
    assert 'data-count="0"' in html
    assert "no tool calls" in html  # ledger empty-state, not a crash


def test_folio_curation_foot_hosts_tags_and_export():
    """The folio is the single detail surface, so it carries the tag/export
    affordances the two-pane detail used to own. The tag section keeps the
    stable #tags-<id> the /tag route swaps via outerHTML."""
    detail = _detail()
    detail.tags = ["shell:file", "decision"]
    html = render_folio(
        detail,
        _FID,
        interactive_tags=True,
        tag_action_url="/tag",
        tag_suggest_url="/tags/suggest",
        export_base_url="/export",
    )
    assert 'class="ledger__curation"' in html
    assert 'id="tags-' in html  # stable swap target for the /tag route
    assert 'hx-post="/tag"' in html  # remove buttons + add form
    assert "format=md" in html and "format=json" in html  # export links


def test_folio_without_context_renders_passive_tags_only():
    # CLI html export path: no routes to offer — tags render as plain pills,
    # no forms, no export links.
    detail = _detail()
    detail.tags = ["decision"]
    html = render_folio(detail, _FID)
    assert 'class="tag">decision' in html
    assert "hx-post" not in html
    assert "export-actions" not in html


# ---------------------------------------------------------------------------
# Reading ↔ trace mode (Phase 1): the body emitter flips; the frame stays shared
# ---------------------------------------------------------------------------


def test_folio_reading_mode_is_default_and_drops_inline_tools():
    # Reading view: tool I/O is not in the body (the ledger + per-turn chip own
    # it), and the article advertises the mode for the chrome + the toggle.
    html = render_folio(_trace_detail(), _FID)
    assert 'data-mode="reading"' in html
    assert "tool-call" not in html
    assert 'class="turn__tools"' in html  # chip stands in for the inlined tools


def test_folio_trace_mode_inlines_tool_calls_in_sequence():
    # Trace view: the same exchange now inlines the tool call between the prose
    # segments, and the redundant per-turn chip is suppressed.
    html = render_folio(_trace_detail(), _FID_TRACE, mode="trace")
    assert 'data-mode="trace"' in html
    assert 'class="tool-call"' in html
    assert 'class="turn__tools"' not in html
    assert "before the tool" in html and "after the tool" in html


def test_folio_trace_mode_shows_thinking_collapsed():
    # Trace inlines the agent's reasoning (collapsed, not open). Guards the
    # render side of the route's thinking=trace fidelity resolution — if that
    # were dropped, the reasoning would silently vanish from the trace.
    html = render_folio(_trace_detail(), _FID_TRACE, mode="trace")
    assert '<details class="thinking">' in html
    assert '<details class="thinking" open>' not in html
    assert "weighing the options" in html


def test_reading_emitter_drops_present_tools_independent_of_fidelity():
    # Isolate the emitter choice from the fidelity gate: render reading mode at a
    # TOOLS-VISIBLE fidelity. The tool data is present (would be fetched), but the
    # reading emitter must still keep it out of the body (the ledger owns it) and
    # not emit the trace's inline classes.
    from siftd.output.html_fmt import _render_turn_blocks

    body, _rail, _n, _counter = _render_turn_blocks(
        _trace_detail().turns, _FID_TRACE, id_prefix="t", mode="reading",
    )
    html = "".join(body)
    assert "tool-call" not in html
    assert 'class="turn__tools"' in html
    assert '<details class="thinking">' not in html  # HtmlEmitter's class, not used


def test_folio_trace_mode_keeps_the_ledger():
    # The ledger is a different lens (frequency) than the inline trace
    # (sequence); it stays in trace mode.
    html = render_folio(_trace_detail(), _FID_TRACE, mode="trace")
    assert 'class="folio__ledger"' in html
    assert 'class="ledger__name">Read' in html


def test_folio_mode_toggle_marks_the_active_mode():
    reading = render_folio(_trace_detail(), _FID)
    assert 'class="folio-mode"' in reading
    # Both buttons re-fetch /folio with the mode on the URL; the active one
    # (reading, the default) carries is-active.
    assert (
        'folio-mode__btn is-active"'
        ' hx-get="/folio?id=01TRACE00000000000&mode=reading"' in reading
    )
    assert 'hx-get="/folio?id=01TRACE00000000000&mode=trace"' in reading

    trace = render_folio(_trace_detail(), _FID_TRACE, mode="trace")
    assert (
        'folio-mode__btn is-active"'
        ' hx-get="/folio?id=01TRACE00000000000&mode=trace"' in trace
    )


def test_folio_unknown_mode_falls_back_to_reading():
    html = render_folio(_trace_detail(), _FID, mode="bogus")
    assert 'data-mode="reading"' in html
    assert "tool-call" not in html


def test_search_context_unfold_renders_the_trace():
    # The unfold IS the trace (Q2): it inlines tool I/O rather than reusing the
    # folio's prose-only body. The matched exchange is flagged is-anchor.
    detail = _trace_detail()
    html = render_search_context(
        detail, _FID_TRACE,
        conv_id="01TRACE00000000000", at=0, w=2, anchor_pos=0,
    )
    assert 'class="hit-context__slice"' in html
    assert 'class="tool-call"' in html
    assert "is-anchor" in html
    assert "turn__tools" not in html


def _xss_trace_detail() -> ConversationDetail:
    """A tool call whose input + result carry markup — agent/user content the
    trace inlines into the served HTML body for the first time."""
    turn = Turn(
        timestamp="2026-03-15T10:30:00Z",
        prompt_text="go",
        total_input_tokens=10,
        total_output_tokens=5,
        narrative=[
            NarrativeBlock(block_type="thinking",
                           content="<script>alert('think')</script>"),
            NarrativeBlock(
                block_type="tool_calls",
                tool_calls=[ToolCallDetail(
                    tool_name="Read", status="success",
                    input='{"file_path": "<script>alert(1)</script>.py"}',
                    result="<script>alert('xss')</script>",
                )],
            ),
        ],
        _tool_call_summaries=[ToolCallSummary("Read", "success", 1)],
    )
    return ConversationDetail(
        id="01XSS0000000000000",
        workspace_path="/p",
        model="m",
        started_at="2026-03-15T10:30:00Z",
        total_input_tokens=10,
        total_output_tokens=5,
        turns=[turn],
    )


def test_folio_trace_mode_escapes_inlined_tool_io():
    # Trace is a NEW XSS surface — the reading view never put tool I/O in the
    # body. Inlined tool input/result/headline must be escaped.
    html = render_folio(_xss_trace_detail(), _FID_TRACE, mode="trace")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_search_context_unfold_escapes_inlined_tool_io():
    # Same surface via the search unfold (always trace).
    html = render_search_context(
        _xss_trace_detail(), _FID_TRACE,
        conv_id="01XSS0000000000000", at=0, w=2, anchor_pos=0,
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_folio_trace_mode_shows_full_tool_result_not_truncated():
    # The trace's tool result sits behind a collapsed <details>; expanding it
    # must reveal the FULL result, not the 120-char/6-line compact preview.
    long_result = "\n".join(f"RESULTLINE{i:02d}" for i in range(20))
    turn = Turn(
        timestamp="2026-03-15T10:30:00Z", prompt_text="go",
        total_input_tokens=1, total_output_tokens=1,
        narrative=[NarrativeBlock(
            block_type="tool_calls",
            tool_calls=[ToolCallDetail(
                tool_name="custom.tool", status="success",
                input="x", result=long_result,
            )],
        )],
        _tool_call_summaries=[ToolCallSummary("custom.tool", "success", 1)],
    )
    detail = ConversationDetail(
        id="01FULL000000000000", workspace_path="/p", model="m",
        started_at="2026-03-15T10:30:00Z",
        total_input_tokens=1, total_output_tokens=1, turns=[turn],
    )
    html = render_folio(detail, _FID_TRACE, mode="trace")
    assert "RESULTLINE00" in html
    assert "RESULTLINE19" in html  # the last line survives → result not cut
    assert "more lines" not in html  # no overflow stub when full


# ---------------------------------------------------------------------------
# Event-precise "open in folio" jump (Phase 2 / slice 2). The trace anchors each
# response (and each prompt) by its event ULID; a search hit jumps to mode=trace
# targeting one, so the folio lands ON the match (data-scroll-to + is-target),
# not the folio top. Pure render-layer — the event_id is already threaded through
# walk_narrative; this proves the anchors/jump markup ride that substrate.
# ---------------------------------------------------------------------------


def _anchored_detail() -> ConversationDetail:
    """A trace conv whose blocks carry event_ids — the anchor substrate the jump
    targets. One response (01RESP) spans thinking → text → tool → text (one
    event, many blocks); the prompt is its own event (01PROMPT)."""
    turn = Turn(
        timestamp="2026-03-15T10:30:00Z",
        prompt_text="run it",
        prompt_id="01PROMPT",
        response_ids=["01RESP"],
        total_input_tokens=10,
        total_output_tokens=5,
        narrative=[
            NarrativeBlock(block_type="thinking", content="hmm", event_id="01RESP"),
            NarrativeBlock(block_type="text", content="before", event_id="01RESP"),
            NarrativeBlock(
                block_type="tool_calls", event_id="01RESP",
                tool_calls=[ToolCallDetail(
                    tool_name="Read", status="success",
                    input="x.py", result="body",
                )],
            ),
            NarrativeBlock(block_type="text", content="after", event_id="01RESP"),
        ],
        _tool_call_summaries=[ToolCallSummary("Read", "success", 1)],
    )
    return ConversationDetail(
        id="01TRACE00000000000", workspace_path="/proj", model="claude-opus",
        started_at="2026-03-15T10:30:00Z",
        total_input_tokens=10, total_output_tokens=5, turns=[turn],
    )


def test_trace_anchors_each_response_once():
    # One response = many blocks under one event_id → exactly one data-event-id
    # for it, so the anchor (and the jump's target) is unique, not duplicated
    # across every block of the response.
    html = render_folio(_anchored_detail(), _FID_TRACE, mode="trace")
    assert html.count('data-event-id="01RESP"') == 1
    # The prompt is its own event, anchored on the user div.
    assert 'data-event-id="01PROMPT"' in html


def test_trace_target_marks_is_target_and_scroll_hint():
    html = render_folio(
        _anchored_detail(), _FID_TRACE, mode="trace", target_event_id="01RESP",
    )
    assert 'data-scroll-to="01RESP"' in html  # enhance.js consumes this once
    assert "is-target" in html                # the landed element is marked


def test_no_target_means_no_scroll_hint():
    html = render_folio(_anchored_detail(), _FID_TRACE, mode="trace")
    assert "data-scroll-to" not in html
    assert "is-target" not in html


def test_reading_mode_anchors_prompt_but_not_response_body():
    # Reading uses the FolioEmitter (tools live in the ledger), which does not
    # anchor response blocks; only the prompt div (rendered by _render_turn_blocks)
    # is anchored. Response anchoring is a trace-mode affordance — the jump's
    # target — so the search jump opens trace, not reading.
    html = render_folio(_anchored_detail(), _FID_TRACE, mode="reading")
    assert 'data-event-id="01PROMPT"' in html
    assert 'data-event-id="01RESP"' not in html


def _chunk(**over: object) -> dict:
    base = {
        "conversation_id": "01CONV", "display_label": "ASSISTANT", "score": 0.9,
        "_workspace": "p", "_started_at": "2026-06-19", "text": "match",
        "turn_index": 3, "event_id": "01EVT",
    }
    base.update(over)
    return base


def test_search_chunk_jump_opens_trace_at_event():
    html = render_search(
        SearchView(results=[_chunk()], view="chunks"),
        _FID, query="q", detail_base="/folio", shell_base="/",
    )
    # The hit's folio jump is a trace jump anchored at the matched event.
    assert "mode=trace" in html and "event=01EVT" in html
    # The unfold trigger carries the event so the in-place rings stay event-aware.
    assert "/find/context?id=01CONV&at=3&w=2&event=01EVT" in html


def test_search_chunk_without_event_still_opens_trace():
    chunk = _chunk()
    del chunk["event_id"]
    html = render_search(
        SearchView(results=[chunk], view="chunks"),
        _FID, query="q", detail_base="/folio", shell_base="/",
    )
    assert "mode=trace" in html   # the entry-point rule (search → trace) still holds
    assert "event=" not in html   # but there's no matched event to anchor


def test_unfold_last_ring_jump_is_event_precise():
    html = render_search_context(
        _anchored_detail(), _FID_TRACE,
        conv_id="01CONV", at=0, w=10, anchor_pos=0, event="01EVT",
    )
    assert "open in folio" in html
    assert "mode=trace" in html and "event=01EVT" in html
