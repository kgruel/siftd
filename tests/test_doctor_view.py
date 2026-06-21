"""Tests for the painted doctor progress view (siftd/doctor/view.py).

Regression coverage for the TTY crash: under a degenerate terminal that reports
0 columns (e.g. a pty opened by `script`), the two-column progress layout
derived negative widths and painted raised
``ValueError: Block row 0 width 0 != block width -10``. The width is now floored
so the layout stays coherent and never crashes.
"""

import pytest

from siftd.doctor.checks import Finding
from siftd.doctor.view import render_progress_block, severity_glyph

pytest.importorskip("painted")


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
        0,
    )

    assert block.height >= 1
    assert block.width >= 1


def test_render_progress_block_zero_width_no_issues(monkeypatch):
    """The all-passed path (empty left column) also survives a 0-width pty."""
    monkeypatch.setattr("siftd.doctor.view.term_width", lambda: 0)

    block = render_progress_block(
        ["schema-version", "fts-stale"],
        {"schema-version": [], "fts-stale": []},
        2,
    )

    assert block.height >= 1
