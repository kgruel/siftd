"""Tests for siftd.output.table — the one width-budgeted painted table.

Guards the width-policy decisions that an adversarial review surfaced:
all-numeric tables must not balloon a quantity to full width, narrow tables
must not balloon at all, genuine overflow must ellipsize (with a marker), and
a table too wide for the terminal must render natural (lossless) rather than
silently clipping columns.
"""

import io

import pytest

from siftd.output.table import (
    Col,
    _is_numeric_col,
    print_table,
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


# --- ASCII degradation of the header rule -----------------------------------


def test_header_rule_degrades_to_ascii():
    # The lone Unicode glyph the gutter border draws is the ─ header rule; on a
    # non-Unicode target (print_table decides it at the stream) it becomes -.
    uni = _text(render_string_table(["k", "n"], [["a", "1"]], width=None))
    asc = _text(render_string_table(["k", "n"], [["a", "1"]], width=None, as_ascii=True))
    assert "─" in uni and "─" not in asc
    assert "-" in asc  # the rule is still drawn, just ASCII


# --- print_table's stream-driven degradation decision -----------------------


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_print_table_never_emits_unicode_on_an_incapable_tty(monkeypatch):
    # A non-UTF-8 TTY (LANG=C): print_table must emit NO Unicode. The rule
    # degrades to '-', AND the budget is dropped so painted draws no '…'
    # truncation marker (which as_ascii can't reach and would crash a strict
    # ASCII stream). The over-budget value survives losslessly instead.
    from siftd.output import table

    buf = _TTY()
    monkeypatch.setattr("sys.stdout", buf)
    monkeypatch.setattr(table, "prefers_ascii", lambda *a, **k: True)
    monkeypatch.setattr(table, "term_width", lambda *a, **k: 20)
    table.print_table(["workspace", "n"], [["/a/very/deep/workspace/path/overflows", "1234"]])
    out = buf.getvalue()
    assert "…" not in out  # no painted ellipsis (no budget → no truncation)
    assert "─" not in out  # rule degraded
    assert "-" in out  # ...to ASCII
    assert "overflows" in out  # natural sizing kept the value whole


def test_print_table_keeps_budget_and_ellipsis_on_a_capable_tty(monkeypatch):
    # The positive control: a UTF-8 TTY keeps the width budget, so an over-budget
    # column ellipsizes (…) and the rule stays Unicode (─).
    from siftd.output import table

    buf = _TTY()
    monkeypatch.setattr("sys.stdout", buf)
    monkeypatch.setattr(table, "prefers_ascii", lambda *a, **k: False)
    monkeypatch.setattr(table, "term_width", lambda *a, **k: 24)
    table.print_table(["workspace", "n"], [["/a/very/deep/workspace/path/overflows", "1234"]])
    out = buf.getvalue()
    assert "─" in out  # Unicode rule kept
    assert "…" in out  # budget engaged → truncation marker drawn


def test_print_table_degrades_the_rule_when_piped(capsys):
    # A pipe (capsys stdout is not a TTY) → prefers_ascii → '-' rule, never the
    # Unicode '─'. Pins the deliberate piped-output change so a regression that
    # reverted print_table to always-Unicode can't pass silently.
    print_table(["k", "n"], [["a", "1"]])
    out = capsys.readouterr().out
    assert "─" not in out and "-" in out


def test_table_budget_drops_budget_on_an_incapable_tty(monkeypatch):
    # The single decision behind print_table AND the block-returning render_table
    # callers (query/peek lists, ingest summary): a non-UTF-8 LANG=C TTY drops the
    # width budget to None — so painted draws no '…' to crash a strict-ASCII
    # stream — and asks for the ASCII rule.
    from siftd.output import table

    monkeypatch.setattr("sys.stdout", _TTY())
    monkeypatch.setattr(table, "prefers_ascii", lambda *a, **k: True)
    monkeypatch.setattr(table, "term_width", lambda *a, **k: 20)
    assert table.table_budget() == (None, True)


def test_table_budget_keeps_budget_on_a_capable_tty(monkeypatch):
    # A UTF-8 TTY keeps the terminal width budget and the Unicode rule.
    from siftd.output import table

    monkeypatch.setattr("sys.stdout", _TTY())
    monkeypatch.setattr(table, "prefers_ascii", lambda *a, **k: False)
    monkeypatch.setattr(table, "term_width", lambda *a, **k: 80)
    assert table.table_budget() == (80, False)


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
