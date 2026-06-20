"""Tests for siftd.output.table — the one width-budgeted painted table.

Guards the width-policy decisions that an adversarial review surfaced:
all-numeric tables must not balloon a quantity to full width, narrow tables
must not balloon at all, genuine overflow must ellipsize (with a marker), and
a table too wide for the terminal must render natural (lossless) rather than
silently clipping columns.
"""

import pytest

from siftd.output.table import (
    Col,
    _is_numeric_col,
    render_string_table,
    render_table,
)


def _text(block) -> str:
    return "\n".join(
        "".join(cell.char for cell in block.row(y)).rstrip() for y in range(block.height)
    )


# --- numeric detection (drives right-align + fill exclusion) ----------------


@pytest.mark.parametrize(
    "cells,expected",
    [
        (["1234", "56"], True),
        (["1,234", "9"], True),
        (["$2.18"], True),
        (["46.6B"], True),
        (["-3", "+4"], True),
        (["1,2,3"], False),  # list-ish, not grouped thousands
        (["12,"], False),  # trailing comma artifact
        (["12,34"], False),  # mis-grouped
        (["2026-06-20"], False),  # date
        (["v11"], False),  # version
        (["1.2.3"], False),  # dotted
        ([], False),  # no cells
    ],
)
def test_is_numeric_col(cells, expected):
    assert _is_numeric_col(cells) is expected


# --- balloon avoidance ------------------------------------------------------


def test_all_numeric_table_does_not_balloon():
    # Every column numeric → no Fill column → natural compact width, well under
    # the budget (regression: a numeric column used to flex to the full width).
    block = render_string_table(
        ["conversations", "prompts"], [["13829", "43707"], ["9", "10"]], width=100
    )
    assert block.width < 40  # compact, not stretched toward 100
    assert "13829" in _text(block)


def test_narrow_table_does_not_balloon():
    # A narrow text column that already fits must stay compact, not stretch to
    # fill the terminal just because a budget was supplied.
    block = render_string_table(["day", "n"], [["2026-06-20", "5"]], width=80)
    assert block.width < 30


# --- genuine overflow → ellipsize with a marker -----------------------------


def test_overflow_fill_right_ellipsizes_with_marker():
    block = render_string_table(["k", "desc"], [["a", "X" * 200]], width=40)
    assert block.width == 40  # budget engaged
    assert "…" in _text(block)  # truncation is signaled, not silent


def test_too_many_columns_render_natural_not_clipped():
    # When the bounded columns alone exceed the budget the fill cannot absorb
    # the overflow; render natural (lossless soft-wrap) rather than clipping
    # trailing columns out of existence.
    block = render_string_table(
        ["aa", "bb", "cc", "dd"], [["x" * 30, "y" * 30, "z" * 30, "w" * 5]], width=40
    )
    assert block.width > 40  # not clipped to the budget
    header = _text(block).splitlines()[0]
    for h in ("aa", "bb", "cc", "dd"):
        assert h in header  # no column silently dropped


# --- left-ellipsis keeps the path leaf --------------------------------------


def test_fill_left_ellipsis_keeps_leaf():
    cols = [
        Col("id", lambda r: r[0]),
        Col("workspace", lambda r: r[1], fill=True, min_width=10, ellipsis_left=True),
    ]
    block = render_table(cols, [["01ABCDEF1234", "-Users-kaygee-Code-siftd--7"]], width=36)
    assert block.width == 36
    row = _text(block).splitlines()[2]
    assert "…" in row
    assert row.rstrip().endswith("siftd--7")  # leaf survived; head was trimmed
