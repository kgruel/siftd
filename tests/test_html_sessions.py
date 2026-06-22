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


def _summary(cid, ext, *, parent=None, tokens=10, cost=None, agent_type=None):
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
        agent_type=agent_type,
    )


def test_sessions_nests_subagents_under_parent():
    parent = _summary("c-root", "claude_code::r1")
    k1 = _summary("c-a1", "claude_code::r1::agent::a1", parent="claude_code::r1")
    k2 = _summary("c-a2", "claude_code::r1::agent::a2", parent="claude_code::r1")
    html = render_sessions([], [parent, k1, k2], **_CTX)
    # One top-level entry (exact class="entry"); two nested sub entries.
    assert html.count('class="entry"') == 1
    assert html.count("entry--sub") == 2
    assert html.count("row--sub") == 2  # legacy hook retained for the toggle CSS
    # Parent carries an agent-count chip in its own cell (not jammed into name).
    assert "2 agents</span>" in html
    # Parent is an expandable group; children collapse under it by default.
    assert "row__toggle" in html  # legacy hook beside the new entry__toggle class
    assert 'data-group="c-root"' in html
    assert html.count('data-parent="c-root" hidden') == 2
    # Leaf totals count roots as sessions, sub-agents separately.
    assert '<span class="total__k">Sessions</span><span class="total__n">1</span>' in html
    assert '<span class="total__k">Sub-agents</span><span class="total__n">2</span>' in html
    # All three remain reachable detail rows (children not dropped).
    assert "c-root" in html and "c-a1" in html and "c-a2" in html


def test_sessions_orphan_subagent_flagged_at_top_level():
    # Parent fell outside the page (n=50) — the sub-agent still renders, flagged
    # as a sub row, never silently dropped.
    orphan = _summary(
        "c-orphan", "claude_code::r9::agent::a9", parent="claude_code::r9"
    )
    html = render_sessions([], [orphan], **_CTX)
    assert "entry--sub" in html
    assert "c-orphan" in html
    # Orphan stays visible (no parent row to nest under) — not collapsed, not a child.
    assert " hidden>" not in html
    assert "data-parent" not in html
    assert "row__toggle" not in html


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
    assert "entry--sub" not in html
    assert "Sub-agents" not in html
    assert html.count('class="entry"') == 2


def test_sessions_child_shows_agent_type_not_workspace():
    # A child identifies by its agent type + spawn time, never by the parent's
    # workspace name (which every sibling would otherwise repeat).
    parent = _summary("c-root", "claude_code::r1")
    kid = _summary(
        "c-a1", "claude_code::r1::agent::a1", parent="claude_code::r1",
        agent_type="Explore",
    )
    html = render_sessions([], [parent, kid], **_CTX)
    assert "Explore" in html
    assert 'class="entry__time">' in html  # spawn-time rides the gutter
    # Workspace basename appears once — on the parent, never echoed onto the child.
    assert html.count(">proj<") == 1


def test_sessions_child_strips_plugin_namespace():
    parent = _summary("c-root", "claude_code::r1")
    kid = _summary(
        "c-a1", "claude_code::r1::agent::a1", parent="claude_code::r1",
        agent_type="feature-dev:code-reviewer",
    )
    html = render_sessions([], [parent, kid], **_CTX)
    assert "code-reviewer" in html
    assert "feature-dev:code-reviewer" not in html


def test_sessions_child_falls_back_to_time_without_agent_type():
    # No sidecar type (historical / rotated-off) — the spawn time still replaces
    # the repeated workspace so siblings stay distinguishable.
    parent = _summary("c-root", "claude_code::r1")
    kid = _summary("c-a1", "claude_code::r1::agent::a1", parent="claude_code::r1")
    html = render_sessions([], [parent, kid], **_CTX)
    assert 'class="entry__time">' in html
    assert html.count(">proj<") == 1
