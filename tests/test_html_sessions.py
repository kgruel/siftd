"""Unit tests for the Swiss Sessions renderer (output/html_fmt.render_sessions).

Base lane (no litestar): renders real ConversationSummary dataclasses, proving
sub-agent nesting is built from the parent_external_id that list_conversations
already derives from external_id — no new data surface, no schema column.
"""

from __future__ import annotations

from siftd.api.conversations import ConversationSummary
from siftd.output.html_fmt import render_sessions

_CTX = dict(
    live_enabled=False, detail_base="/folio", shell_base="/", follow_base="/follow"
)


def _summary(cid, ext, *, parent=None, tokens=10, cost=None):
    return ConversationSummary(
        id=cid,
        workspace_path="/proj",
        model="claude-opus",
        started_at="2026-03-15T10:30:00Z",
        prompt_count=1,
        response_count=1,
        total_tokens=tokens,
        cost=cost,
        external_id=ext,
        parent_external_id=parent,
    )


def test_sessions_nests_subagents_under_parent():
    parent = _summary("c-root", "claude_code::r1")
    k1 = _summary("c-a1", "claude_code::r1::agent::a1", parent="claude_code::r1")
    k2 = _summary("c-a2", "claude_code::r1::agent::a2", parent="claude_code::r1")
    html = render_sessions([], [parent, k1, k2], **_CTX)
    # One top-level row (exact class="row"); two nested sub rows.
    assert html.count('class="row"') == 1
    assert html.count("row--sub") == 2
    # Parent carries an agent-count chip in its own cell (not jammed into name).
    assert "2 agents</span>" in html
    # Parent is an expandable group; children collapse under it by default.
    assert 'class="row__toggle"' in html
    assert 'data-group="c-root"' in html
    assert html.count('data-parent="c-root" hidden') == 2
    # Day head counts roots as sessions, sub-agents separately.
    assert "1 sessions" in html
    assert "2 sub-agents" in html
    # All three remain reachable detail rows (children not dropped).
    assert "c-root" in html and "c-a1" in html and "c-a2" in html


def test_sessions_orphan_subagent_flagged_at_top_level():
    # Parent fell outside the page (n=50) — the sub-agent still renders, flagged
    # as a sub row, never silently dropped.
    orphan = _summary(
        "c-orphan", "claude_code::r9::agent::a9", parent="claude_code::r9"
    )
    html = render_sessions([], [orphan], **_CTX)
    assert "row--sub" in html
    assert "c-orphan" in html
    # Orphan stays visible (no parent row to nest under) — not collapsed, not a child.
    assert " hidden>" not in html
    assert "data-parent" not in html
    assert 'class="row__toggle"' not in html


def test_sessions_day_totals_fold_in_subagents():
    parent = _summary("c-root", "claude_code::r1", tokens=100, cost=1.0)
    kid = _summary(
        "c-a1", "claude_code::r1::agent::a1", parent="claude_code::r1",
        tokens=50, cost=0.5,
    )
    html = render_sessions([], [parent, kid], **_CTX)
    # 100 + 50 tokens, $1.00 + $0.50 — sub-agent work still counts toward the day.
    assert "150" in html
    assert "$1.50" in html


def test_sessions_flat_without_subagents():
    a = _summary("c-1", "claude_code::r1")
    b = _summary("c-2", "claude_code::r2")
    html = render_sessions([], [a, b], **_CTX)
    assert "row--sub" not in html
    assert "sub-agents" not in html
    assert html.count('class="row"') == 2
