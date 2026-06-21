"""Tests for siftd.output.row — the shared row atom.

``row_line`` is the single definition the table/listing/live/doctor row builders
delegate to. These guard the contract they all rely on — segments concatenate in
order, empty-text segments drop, a ``None`` style is plain, an ``indent``
prefixes plain spaces, and widths are wcwidth-correct — so a future change to the
atom can't silently shift one surface's output.
"""

import io

from painted import Style, print_block

from siftd.output.row import row_line


def _text(line) -> str:
    block = line.to_block(line.width)
    return "".join(cell.char for cell in block.row(0)) if block.height else ""


def _ansi(line) -> str:
    buf = io.StringIO()
    print_block(line.to_block(line.width), buf, use_ansi=True)
    return buf.getvalue()


def test_segments_concatenate_in_order():
    assert _text(row_line([("a", None), ("b", None), ("c", None)])) == "abc"


def test_empty_text_segments_drop_without_a_gap():
    # Optional parts collapse cleanly — the reason callers can pass conditional
    # ("", style) segments instead of branching.
    line = row_line([("a", None), ("", Style()), ("b", None)])
    assert _text(line) == "ab"
    assert line.width == 2


def test_indent_prefixes_plain_spaces():
    assert _text(row_line([("x", None)], indent="  ")) == "  x"


def test_none_style_is_equivalent_to_plain():
    # The contract that lets the doctor/painted_bridge builders (which always pass
    # a concrete Style) and live.text_row (which passes None) share one atom.
    assert _ansi(row_line([("x", None)])) == _ansi(row_line([("x", Style())]))


def test_meaningful_style_survives_or_plain():
    # The load-bearing invariant of `style or plain`: a CONCRETE style must never
    # be substituted by plain (that would silently drop colour on the
    # doctor/folio/search surfaces). A styled segment renders distinctly from
    # plain and identically to a directly-built span — guarding the dangerous
    # direction the None==plain test alone doesn't exercise.
    from painted import Line, Span

    styled = _ansi(row_line([("x", Style(fg=2))]))
    assert styled != _ansi(row_line([("x", None)]))  # not flattened to plain
    direct = Line(spans=(Span("x", Style(fg=2)),))
    buf = io.StringIO()
    print_block(direct.to_block(direct.width), buf, use_ansi=True)
    assert styled == buf.getvalue()  # identical to the raw span — no substitution


def test_width_is_wcwidth_correct():
    # A wide (CJK) glyph counts two columns — the measure every aligner trusts.
    assert row_line([("日", None)]).width == 2


def test_all_empty_is_a_zero_width_line():
    assert row_line([]).width == 0
    assert row_line([("", None)]).width == 0
