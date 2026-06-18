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
    ToolCallSummary,
    Turn,
)
from siftd.output.html_fmt import render_folio

_FID = Fidelity(depth=2, visible=frozenset({"text"}), chars=0)


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
