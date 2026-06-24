"""Tests for the painted doctor progress view (siftd/doctor/view.py).

Regression coverage for the TTY crash: under a degenerate terminal that reports
0 columns (e.g. a pty opened by `script`), the two-column progress layout
derived negative widths and painted raised
``ValueError: Block row 0 width 0 != block width -10``. The width is now floored
so the layout stays coherent and never crashes.
"""

import io

import pytest

from siftd.doctor.checks import Finding
from siftd.doctor.view import render_findings_block, render_progress_block, severity_glyph
from siftd.output.status import severity_rank

pytest.importorskip("painted")


def _text(block) -> str:
    """Render a Block to plain (no-ANSI) text for content assertions."""
    from painted import print_block

    sink = io.StringIO()
    print_block(block, sink, use_ansi=False)
    return sink.getvalue()


def test_severity_glyph_contract():
    """The single severity-glyph source: Unicode by default, ASCII on request.

    Pins the four severities, the pass/all-clear (None) glyph, and the neutral
    marker for an unrecognized severity (e.g. the declared-but-unused "hint") —
    which must NOT alias onto the pass glyph.
    """
    assert severity_glyph("error") == ("✗", "error")
    assert severity_glyph("warning") == ("⚠", "warning")
    assert severity_glyph("info") == ("ℹ", "muted")
    assert severity_glyph(None) == ("✓", "success")  # pass / all-clear

    assert severity_glyph("error", as_ascii=True) == ("x", "error")
    assert severity_glyph("warning", as_ascii=True) == ("!", "warning")
    assert severity_glyph("info", as_ascii=True) == ("i", "muted")
    assert severity_glyph(None, as_ascii=True) == ("+", "success")

    # Unknown / "hint" -> neutral marker, never the pass glyph (regression guard).
    assert severity_glyph("hint") == ("?", "muted")
    assert severity_glyph("hint", as_ascii=True) == ("?", "muted")
    assert severity_glyph("nonsense", as_ascii=True) == ("?", "muted")


def _findings():
    return {
        "schema-version": [],  # passed
        "ingest-errors": [
            Finding(
                check="ingest-errors",
                severity="warning",
                message="claude_code: 8 file(s) failed",
                fix_available=True,
                fix_command="siftd doctor --verbose",
            )
        ],
        # "pricing-provenance" intentionally absent from completed -> pending
    }


@pytest.mark.parametrize("reported_cols", [0, -5, 1, 10, 40, 120])
def test_render_progress_block_survives_any_terminal_width(reported_cols, monkeypatch):
    """The progress block renders (never raises) regardless of reported width.

    0/negative/tiny widths are the degenerate-pty case that used to crash; 40+
    are normal terminals. All must produce a non-empty Block.
    """
    monkeypatch.setattr("siftd.doctor.view.term_width", lambda: reported_cols)

    block = render_progress_block(
        ["schema-version", "ingest-errors", "pricing-provenance"],
        _findings(),
    )

    assert block.height >= 1
    # The no-tear invariant: the panel fits the terminal exactly (tw floored 20),
    # never overflowing it — overflow would scroll the viewport and shred the
    # in-place repaint, the failure class the single-column design dissolves.
    assert block.width == max(reported_cols, 20)


@pytest.mark.parametrize("tw", [20, 30, 60, 100])
def test_progress_panel_never_overflows_with_long_names_and_messages(tw, monkeypatch):
    """Force the width-steal branch (check names past the 24-col cap), a 200-char
    finding message, and a wide-char (CJK) ``current`` label — the panel must still
    fit ``tw`` exactly (the bar label clip, the issue-row clip, and bar_row's
    display-width padding all holding the bound)."""
    monkeypatch.setattr("siftd.doctor.view.term_width", lambda: tw)

    names = [f"an-extremely-long-check-name-number-{i}-well-past-the-cap" for i in range(3)]
    completed = {
        names[0]: [Finding(check=names[0], severity="error", message="x" * 200, fix_available=False)],
    }
    block = render_progress_block(names, completed, current="非常に長い日本語のチェック名です")

    assert block.width == tw  # fits exactly — never wider than the terminal


def test_clip_truncates_label_to_display_width():
    from painted.core._text_width import display_width

    from siftd.doctor.view import _clip

    assert _clip("short", 20) == "short"  # already fits → unchanged
    assert display_width(_clip("a-very-long-check-name", 10)) <= 10
    assert display_width(_clip("日本語チェック名です", 6)) <= 6  # wide chars
    assert display_width(_clip("anything", 1)) <= 1  # width 1 → bare ellipsis


def test_fit_truncates_row_to_width():
    from siftd.doctor.view import _fit

    assert _fit([("x" * 100, None)], 20).width <= 20  # long row clipped to width
    assert _fit([("short", None)], 20).width <= 20  # short row stays natural


def test_render_progress_block_zero_width_no_issues(monkeypatch):
    """The all-passed path (empty left column) also survives a 0-width pty."""
    monkeypatch.setattr("siftd.doctor.view.term_width", lambda: 0)

    block = render_progress_block(
        ["schema-version", "fts-stale"],
        {"schema-version": [], "fts-stale": []},
    )

    assert block.height >= 1


# --- severity vocabulary -----------------------------------------------------


def test_severity_rank_orders_worst_first():
    """error < warning < info < anything-else; unknown / hint sort last (tie)."""
    assert severity_rank("error") < severity_rank("warning") < severity_rank("info")
    assert severity_rank("hint") == severity_rank("nonsense") == severity_rank(None)
    assert severity_rank("info") < severity_rank("hint")


# --- the live progress panel -------------------------------------------------


def test_progress_panel_bar_carries_current_and_count_over_issue_feed(monkeypatch):
    """The live panel is a single activity bar (current check + done/total count)
    over an accumulating issue feed — no passed/pending lists, no running tally
    (those belong to the settled report)."""
    monkeypatch.setattr("siftd.doctor.view.term_width", lambda: 100)

    completed = {
        "schema-version": [],  # a clean check — does NOT get its own line
        "cost-coverage": [],
        "ingest-errors": [
            Finding(
                check="ingest-errors", severity="warning",
                message="8 file(s) failed", fix_available=True,
                fix_command="siftd doctor --verbose",
            )
        ],
    }
    # 5 names total, 3 completed → the bar reads 3/5; the just-resolved check is
    # the bar's label.
    block = render_progress_block(
        ["schema-version", "cost-coverage", "ingest-errors", "fts-stale", "config-valid"],
        completed,
        current="cost-coverage",
    )
    text = _text(block)

    assert "cost-coverage" in text  # the current check rides the bar label
    assert "3/5" in text  # the done/total count
    assert "ingest-errors: 8 file(s) failed" in text  # the issue, one-line shape
    # No passed/pending lists and no running tally in the live panel.
    assert "pending" not in text
    assert "passed" not in text
    # A clean check never gets its own feed line.
    assert "schema-version" not in text


def test_progress_panel_no_issue_feed_when_clean(monkeypatch):
    monkeypatch.setattr("siftd.doctor.view.term_width", lambda: 80)
    block = render_progress_block(["a", "b"], {"a": [], "b": []}, current="b")
    text = _text(block)
    assert "2/2" in text  # the bar count
    assert "↳" not in text  # nothing failed → no issue lines


# --- the settled findings report --------------------------------------------


def test_findings_report_all_passed_message():
    text = _text(render_findings_block([], show_fixes=False, total_checks=17))
    assert "All 17 checks passed." in text


def test_findings_report_lists_issues_tally_and_fixes():
    findings = [
        Finding(check="db-fk", severity="error", message="2 orphaned rows",
                fix_available=True, fix_command="siftd doctor fix"),
        Finding(check="ingest-errors", severity="warning", message="8 file(s) failed",
                fix_available=False),
    ]
    text = _text(render_findings_block(findings, show_fixes=True, total_checks=10))

    # Issues, worst-first (error before warning), as "check: message".
    assert text.index("db-fk:") < text.index("ingest-errors:")
    assert "2 orphaned rows" in text
    # Severity tally: 1 error, 1 warning, and 10 - 2-with-findings = 8 passed.
    assert "1 error" in text and "1 warning" in text and "8 passed" in text
    # The "To fix" section lists the deduped fix command.
    assert "To fix" in text
    assert "siftd doctor fix" in text


def test_findings_report_hides_fix_continuation_in_show_fixes_mode():
    """With show_fixes the fix command moves to the 'To fix' section, so the
    inline ``↳ fix_command`` continuation is suppressed (no duplication)."""
    findings = [
        Finding(check="x", severity="warning", message="m",
                fix_available=True, fix_command="siftd fix-it"),
    ]
    inline = _text(render_findings_block(findings, show_fixes=False, total_checks=1))
    sectioned = _text(render_findings_block(findings, show_fixes=True, total_checks=1))
    assert "↳" in inline and "siftd fix-it" in inline  # inline continuation
    assert "↳" not in sectioned  # suppressed; the command is in "To fix"
    assert "To fix" in sectioned and "siftd fix-it" in sectioned
