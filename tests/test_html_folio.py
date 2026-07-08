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


def test_folio_curation_bar_hosts_tags_and_export():
    """The folio is the single detail surface, so it carries the tag/export
    affordances the two-pane detail used to own — now in the command bar's
    actions group. The tag section keeps the stable #tags-<id> the /tag route
    swaps via outerHTML."""
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
    assert 'class="folio__bar"' in html
    assert "folio__bargroup--actions" in html  # tags/export live in the bar now
    assert 'id="tags-' in html  # stable swap target for the /tag route
    assert 'hx-post="/tag"' in html  # remove buttons + add form
    assert "format=md" in html and "format=json" in html  # export links


def test_folio_body_renders_element_tag_chips_and_affordance():
    """WS4: each turn carries its own element-tag section inside the body — chips
    for existing tags + a hover-revealed add form scoped to that element. The
    prompt block tags on its prompt event; the assistant block on its primary
    response event. Data rides ConversationDetail.event_tags (the WS3 batch
    fetch), threaded through render_folio."""
    detail = _detail()
    detail.turns[0].prompt_id = "01PROMPT0000000000"
    detail.turns[0].response_ids = ["01RESP000000000000"]
    detail.event_tags = {
        "01PROMPT0000000000": [("needs-followup", "prompt")],
        "01RESP000000000000": [("good-answer", "response")],
    }
    html = render_folio(
        detail,
        _FID,
        interactive_tags=True,
        tag_action_url="/tag",
        tag_suggest_url="/tags/suggest",
    )
    # Chips render for both the prompt and the response events.
    assert 'class="tag">needs-followup' in html or "needs-followup" in html
    assert "good-answer" in html
    # Element sections carry the hover-reveal class + the resolved entity_type so
    # the form re-posts against the right target kind.
    assert "tag-section--elem" in html
    assert 'name="entity_type" value="prompt"' in html
    assert 'name="entity_type" value="response"' in html
    # The add form posts to /tag with a per-element stable swap id.
    assert 'hx-post="/tag"' in html


def test_folio_prompt_exchange_chip_removes_as_exchange_not_prompt():
    """A prompt section unions its 'exchange'-kind tags into its chips. The remove
    button must post the chip's OWN kind — an exchange chip removes the exchange
    assignment, not a nonexistent prompt one. The (name, kind) pair on each chip
    is what makes the remove target the assignment the user actually clicked."""
    import json
    from html import escape

    detail = _detail()
    detail.turns[0].prompt_id = "01PROMPT0000000000"
    detail.event_tags = {
        "01PROMPT0000000000": [("prompt-tag", "prompt"), ("exch-tag", "exchange")],
    }
    html = render_folio(
        detail, _FID, interactive_tags=True,
        tag_action_url="/tag", tag_suggest_url="/tags/suggest",
    )
    # Both chips' remove hx-vals appear (HTML-escaped), each carrying its own kind
    # plus the hosting section's kind (so the fragment re-renders as the section).
    assert escape(json.dumps(
        {"action": "remove", "id": "01PROMPT0000000000", "tag": "exch-tag",
         "entity_type": "exchange", "section_type": "prompt"}
    )) in html
    assert escape(json.dumps(
        {"action": "remove", "id": "01PROMPT0000000000", "tag": "prompt-tag",
         "entity_type": "prompt", "section_type": "prompt"}
    )) in html


def test_folio_interactive_offers_affordance_even_when_untagged():
    """The hover-reveal add form is offered on every element with an id, tagged or
    not (that IS the write affordance) — an untagged element still gets its
    section, just with no chips."""
    detail = _detail()
    detail.turns[0].prompt_id = "01PROMPT0000000000"
    html = render_folio(
        detail, _FID, interactive_tags=True,
        tag_action_url="/tag", tag_suggest_url="/tags/suggest",
    )
    assert "tag-section--elem" in html
    assert 'name="entity_type" value="prompt"' in html


def test_folio_non_interactive_untagged_element_stays_chip_free():
    """No routes + no tags → element sections are suppressed entirely (the CLI
    html export path stays clean; nothing to reveal, nothing to show)."""
    detail = _detail()
    detail.turns[0].prompt_id = "01PROMPT0000000000"
    html = render_folio(detail, _FID)  # non-interactive, no event_tags
    assert "tag-section--elem" not in html


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

    body, _rail, _n, _counter, _seq = _render_turn_blocks(
        _trace_detail().turns, _FID_TRACE, id_prefix="t", mode="reading",
    )
    html = "".join(body)
    assert "tool-call" not in html
    assert 'class="turn__tools"' in html
    assert '<details class="thinking">' not in html  # HtmlEmitter's class, not used


def test_folio_trace_mode_shows_activity_sequence():
    # Trace replaces the frequency ledger with the chronological Activity run:
    # one row per tool call, in order, each linking to its inline .tool-call[id]
    # (enhance.js scroll-spy mirrors the reading position off those ids).
    html = render_folio(_trace_detail(), _FID_TRACE, mode="trace")
    assert 'class="folio__ledger"' in html
    assert 'class="tool-seq"' in html
    assert 'class="tool-seq__name">Read' in html
    # The row anchors the inline tool-call by a folio-unique id.
    assert 'href="#evt-1"' in html and 'id="evt-1"' in html


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


def test_search_context_unfold_renders_reading_preview():
    # Slice 2a: the unfold is a READING preview (prose around the match), not the
    # trace — tool I/O is NOT inlined (it's the chip/ledger's job); the matched
    # exchange is flagged is-anchor.
    detail = _trace_detail()
    html = render_search_context(
        detail, _FID_TRACE,
        conv_id="01TRACE00000000000", at=0, w=2, anchor_pos=0,
    )
    assert 'class="hit-context__slice"' in html
    assert "is-anchor" in html
    assert 'class="tool-call"' not in html  # reading body: tools not inlined


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


def test_unfold_empty_window_still_threads_event():
    # A successful fetch whose window has no renderable turns (prompt-less,
    # narrative-less) falls back to the collapsed trigger — which must still carry
    # the event so a re-unfold stays event-precise. Regression guard: the two
    # collapsed-trigger early returns in render_search_context must be identical.
    empty_turn = Turn(
        timestamp=None, prompt_text=None, narrative=[],
        total_input_tokens=0, total_output_tokens=0, _tool_call_summaries=[],
    )
    detail = ConversationDetail(
        id="01CONV", workspace_path=None, model=None, started_at=None,
        total_input_tokens=0, total_output_tokens=0, turns=[empty_turn],
    )
    html = render_search_context(
        detail, _FID_TRACE, conv_id="01CONV", at=2, w=5, anchor_pos=0, event="01EVT",
    )
    assert "/find/context?id=01CONV&at=2&w=2&event=01EVT" in html


def test_reading_mode_ignores_event_target():
    # The event target is a trace-mode affordance. A reading-mode folio with a
    # target must not emit is-target or an unscrollable data-scroll-to, even
    # though the prompt div still carries its (always-laid) data-event-id anchor.
    html = render_folio(
        _anchored_detail(), _FID_TRACE, mode="reading", target_event_id="01PROMPT",
    )
    assert "data-scroll-to" not in html
    assert "is-target" not in html
    assert 'data-event-id="01PROMPT"' in html


def test_search_thread_tier2_jump_opens_trace_at_event():
    # Thread-view tier2 (compact) hits are SearchChunks too — their folio jump
    # must be event-precise, same as the chunks view.
    sv = SearchView(
        results=[], view="thread", tier1=[],
        tier2=[_chunk(conversation_id="01CONV2", event_id="01EVT2")],
    )
    html = render_search(sv, _FID, query="q", detail_base="/folio", shell_base="/")
    assert "search-hit compact" in html
    assert "mode=trace" in html and "event=01EVT2" in html


def test_search_chunk_jump_carries_search_event_id():
    # A captured search's chunks-view hit threads search_event_id onto the
    # folio jump, so the web-click open-signal (docs/dev/search-log-design-
    # 2026-07-07.md) can bind precisely rather than falling back to the
    # session/window heuristic.
    sv = SearchView(results=[_chunk()], view="chunks", search_event_id="01SEARCHEVT")
    html = render_search(sv, _FID, query="q", detail_base="/folio", shell_base="/")
    assert "search_event_id=01SEARCHEVT" in html


def test_search_thread_tier2_jump_carries_search_event_id():
    sv = SearchView(
        results=[], view="thread", tier1=[],
        tier2=[_chunk(conversation_id="01CONV2", event_id="01EVT2")],
        search_event_id="01SEARCHEVT",
    )
    html = render_search(sv, _FID, query="q", detail_base="/folio", shell_base="/")
    assert "search_event_id=01SEARCHEVT" in html


def test_search_conversations_view_row_carries_search_event_id():
    sv = SearchView(
        results=[{"conversation_id": "01CONV", "_started_at": "2026-06-19", "_workspace": "p"}],
        view="conversations", search_event_id="01SEARCHEVT",
    )
    html = render_search(sv, _FID, query="q", detail_base="/folio", shell_base="/")
    assert "search_event_id=01SEARCHEVT" in html


def test_search_without_capture_omits_search_event_id():
    # No captured search (search.log disabled, capture failed, or empty query)
    # → SearchView.search_event_id stays None, so the jump carries no param —
    # get_conversation then falls back to the CLI-style heuristic, unharmed.
    sv = SearchView(results=[_chunk()], view="chunks")
    html = render_search(sv, _FID, query="q", detail_base="/folio", shell_base="/")
    assert "search_event_id=" not in html


def test_unfold_trigger_carries_search_event_id():
    # The initial unfold trigger's ring URL must carry search_event_id so the
    # context-expansion round trip can preserve it (and the last ring's folio
    # jump records a precise web-click open, not the heuristic fallback).
    sv = SearchView(results=[_chunk()], view="chunks", search_event_id="01SEARCHEVT")
    html = render_search(sv, _FID, query="q", detail_base="/folio", shell_base="/")
    assert (
        "/find/context?id=01CONV&at=3&w=2&event=01EVT&search_event_id=01SEARCHEVT"
        in html
    )


def test_unfold_more_context_ring_threads_search_event_id():
    # The 'more context' widen button re-fetches the next ring; it must keep the
    # search_event_id so it survives every step of the expansion.
    html = render_search_context(
        _anchored_detail(), _FID_TRACE,
        conv_id="01CONV", at=0, w=2, anchor_pos=0, event="01EVT",
        search_event_id="01SEARCHEVT",
    )
    assert "more context" in html
    assert "&event=01EVT&search_event_id=01SEARCHEVT" in html


def test_unfold_last_ring_jump_carries_search_event_id():
    # The last ring's 'open in folio' jump must carry search_event_id, so a
    # context-expanded open is attributed to its search the same way the initial
    # hit link is (else it silently falls back to the CLI heuristic).
    html = render_search_context(
        _anchored_detail(), _FID_TRACE,
        conv_id="01CONV", at=0, w=10, anchor_pos=0, event="01EVT",
        search_event_id="01SEARCHEVT",
    )
    assert "open in folio" in html
    assert "search_event_id=01SEARCHEVT" in html


def test_unfold_collapse_link_threads_search_event_id():
    # The COLLAPSE control (w=0) is part of the same round trip: collapse →
    # re-unfold → open in folio must keep precise attribution, so its ring URL
    # carries search_event_id like every other _ctx_attrs call site.
    html = render_search_context(
        _anchored_detail(), _FID_TRACE,
        conv_id="01CONV", at=0, w=2, anchor_pos=0, event="01EVT",
        search_event_id="01SEARCHEVT",
    )
    assert "collapse" in html
    assert (
        "/find/context?id=01CONV&at=0&w=0&event=01EVT&search_event_id=01SEARCHEVT"
        in html
    )


def test_unfold_empty_window_threads_search_event_id():
    # The collapsed-trigger fallback (window with no renderable turns) must keep
    # search_event_id too, so a re-unfold from there stays attributed.
    empty_turn = Turn(
        timestamp=None, prompt_text=None, narrative=[],
        total_input_tokens=0, total_output_tokens=0, _tool_call_summaries=[],
    )
    detail = ConversationDetail(
        id="01CONV", workspace_path=None, model=None, started_at=None,
        total_input_tokens=0, total_output_tokens=0, turns=[empty_turn],
    )
    html = render_search_context(
        detail, _FID_TRACE, conv_id="01CONV", at=2, w=5, anchor_pos=0,
        event="01EVT", search_event_id="01SEARCHEVT",
    )
    assert "search_event_id=01SEARCHEVT" in html
