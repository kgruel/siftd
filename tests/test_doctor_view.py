"""Tests for the painted doctor progress view (siftd/doctor/view.py).

Regression coverage for the TTY crash: under a degenerate terminal that reports
0 columns (e.g. a pty opened by `script`), the two-column progress layout
derived negative widths and painted raised
``ValueError: Block row 0 width 0 != block width -10``. The width is now floored
so the layout stays coherent and never crashes.
"""

import pytest

from siftd.doctor.checks import Finding
from siftd.doctor.view import render_progress_block

pytest.importorskip("painted")


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
