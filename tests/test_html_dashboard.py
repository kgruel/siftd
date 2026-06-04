"""Unit tests for the Swiss Stats dashboard renderer (output/html_fmt.render_dashboard).

Base lane (no litestar): renders real api dataclasses, so a passing assertion
proves the fragment is built from data the four stats reads already return — no
new data surface, no new api type. Route wiring + owner-scoping are covered in
the serve lane (test_serve_swiss_shell.py).

The load-bearing assertions are the cost-honesty ones: an unpriced group renders
an em dash, never a fabricated $0 — the same NULL=unpriced rule the folio and
ConversationDetail.cost carry.
"""

from __future__ import annotations

from siftd.api.stats import (
    DatabaseStats,
    GroupUsage,
    TableCounts,
    TokenCoverage,
    UsageSummary,
)
from siftd.output.html_fmt import render_dashboard
from siftd.storage.conversation_stats import CostCoverage


def _counts() -> TableCounts:
    return TableCounts(
        conversations=2, prompts=4, responses=6, tool_calls=9, harnesses=1,
        workspaces=2, tools=3, models=2, ingested_files=5,
    )


def _stats() -> DatabaseStats:
    from pathlib import Path

    return DatabaseStats(
        db_path=Path("/x.db"),
        db_size_bytes=1024,
        counts=_counts(),
        harnesses=[],
        harness_counts=[],
        top_workspaces=[],
        models=["claude-x", "claude-y"],
        top_tools=[],
        top_tags=[],
        token_coverage=TokenCoverage(responses=6, with_tokens=5, pct_with_tokens=83.3, by_harness=[]),
        activity_window=("2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        last_ingest_at="2026-03-02T00:00:00Z",
    )


def _render(*, usage, by_model, by_workspace, coverage=None, stats=None, owner=None) -> str:
    return render_dashboard(
        usage=usage,
        by_model=by_model,
        by_workspace=by_workspace,
        coverage=coverage if coverage is not None else CostCoverage(5, 4, 1, 80.0),
        stats=stats if stats is not None else _stats(),
        owner=owner,
    )


def test_dashboard_has_three_regions_and_chrome_contract():
    html = _render(
        usage=UsageSummary(2, 1_000_000, 500_000, 12.5),
        by_model=[GroupUsage("claude-x", 2, 1_000_000, 500_000, 12.5)],
        by_workspace=[GroupUsage("/proj", 2, 1_000_000, 500_000, 12.5)],
    )
    # Fragment root carries the chrome-sync contract enhance.js reads.
    assert 'class="dash"' in html
    assert 'data-view="stats"' in html
    assert 'data-title="Stats"' in html
    assert 'data-count="2"' in html
    # Three regions.
    assert 'class="dash__head"' in html
    assert "Model mix" in html and "Workspace mix" in html
    assert "Corpus" in html
    # Corpus footnotes from get_stats.
    assert "Token coverage" in html and "83.3%" in html
    assert "Cost coverage" in html and "80.0%" in html


def test_dashboard_headline_and_row_cost_when_priced():
    html = _render(
        usage=UsageSummary(2, 1_000_000, 500_000, 12.5),
        by_model=[GroupUsage("claude-x", 2, 1_000_000, 500_000, 12.3456)],
        by_workspace=[GroupUsage("/proj", 2, 1_000_000, 500_000, 12.3456)],
    )
    assert "$12.50" in html  # headline, 2dp
    assert "$12.35" in html  # per-row, 2dp (money column, not 4dp folio precision)
    assert "$12.3456" not in html
    assert "ledger--usage" in html


def test_dashboard_row_cost_unpriced_is_dash_not_zero():
    """A model with no priced usage (cost=None) renders an em dash, never $0 —
    the folio/ConversationDetail honesty rule, carried into the aggregate."""
    html = _render(
        usage=UsageSummary(1, 1_000_000, 0, 0.0),
        by_model=[GroupUsage("claude-x", 1, 1_000_000, 0, None)],
        by_workspace=[GroupUsage("/proj", 1, 1_000_000, 0, None)],
        coverage=CostCoverage(0, 0, 1, 0.0),
    )
    assert "&mdash;" in html
    assert "$0.00" not in html and "$0.0000" not in html
    # Tokens stay exact even when cost is unknown (fmt_tokens → "1000.0k").
    assert "1000.0k" in html


def test_dashboard_headline_dash_when_no_priced_usage():
    """No priced usage anywhere → headline cost is an em dash, not a $0.00 lie."""
    html = _render(
        usage=UsageSummary(1, 1_000_000, 0, 0.0),
        by_model=[GroupUsage("claude-x", 1, 1_000_000, 0, None)],
        by_workspace=[GroupUsage("/proj", 1, 1_000_000, 0, None)],
        coverage=CostCoverage(0, 0, 1, 0.0),
    )
    # The headline Cost stat shows the em dash, no fabricated grand total.
    assert "$0.00" not in html


def test_dashboard_coverage_does_not_round_up_to_false_100():
    """99.87% coverage must not display as a false '100%' — the live DB has
    603,506/604,299 token-bearing responses (99.87%); rounding up would claim a
    completeness it doesn't have. Floors to 1dp instead."""
    stats = _stats()
    stats.token_coverage = TokenCoverage(604_299, 603_506, 99.87, [])
    html = _render(
        usage=UsageSummary(2, 1_000_000, 500_000, 12.5),
        by_model=[GroupUsage("claude-x", 2, 1_000_000, 500_000, 12.5)],
        by_workspace=[GroupUsage("/proj", 2, 1_000_000, 500_000, 12.5)],
        stats=stats,
    )
    assert "99.8%" in html
    assert ">100%" not in html and ">100.0%" not in html


def test_dashboard_empty_corpus_renders_without_error():
    html = _render(
        usage=UsageSummary(0, 0, 0, 0.0),
        by_model=[],
        by_workspace=[],
        coverage=None,
    )
    assert 'class="dash"' in html
    assert "no usage" in html  # empty ledger rows
