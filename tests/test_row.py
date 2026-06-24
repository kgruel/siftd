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


# --- wrap_spans / wrap_segments -------------------------------------------
# The styled word-wrap promoted here from painted_bridge/markdown_render; now the
# single home shared by the markdown body, search snippets, and the help body.
# These pin the contract all three rely on so a change can't silently shift them.

from painted import Span  # noqa: E402

from siftd.output.row import wrap_spans, wrap_segments  # noqa: E402


def _line_text(line) -> str:
    return "".join(sp.text for sp in line.spans)


def test_wrap_spans_word_wraps_to_width():
    lines = wrap_spans([Span("alpha beta gamma", Style())], 11)
    assert [_line_text(ln).rstrip() for ln in lines] == ["alpha beta", "gamma"]
    assert all(ln.width <= 11 for ln in lines)


def test_wrap_spans_hard_splits_an_overlong_token():
    # A single token wider than the whole line is hard-split, never overflowed —
    # the branch the help usage packer leans on for a too-wide mutex group.
    lines = wrap_spans([Span("a" * 20, Style())], 5)
    assert all(ln.width <= 5 for ln in lines)
    assert "".join(_line_text(ln) for ln in lines) == "a" * 20
    assert len(lines) == 4


def test_wrap_spans_preserves_style_across_boundaries():
    red = Style(fg=1)
    lines = wrap_spans([Span("xxxx yyyy", red)], 4)
    for ln in lines:
        for sp in ln.spans:
            if sp.text.strip():
                assert sp.style == red


def test_wrap_spans_is_wcwidth_correct():
    # Wide (CJK) glyphs count two columns, so a width-4 line holds two of them.
    lines = wrap_spans([Span("日" * 5, Style())], 4)
    assert all(ln.width <= 4 for ln in lines)
    assert "".join(_line_text(ln) for ln in lines) == "日" * 5


def test_wrap_segments_reserves_first_prefix_width():
    # The first prefix's display width is reserved out of the budget so the wrapped
    # run never collides with an aligned key column.
    lines = wrap_segments([("one two three", Style())], 12, [("KEY ", Style())])
    assert _line_text(lines[0]).startswith("KEY ")
    assert all(ln.width <= 12 for ln in lines)


def test_wrap_segments_none_style_does_not_crash():
    # The load-bearing guard the promotion ADDED: the originals passed Span(t, None)
    # which crashes in to_block; wrap_segments coerces None -> plain so a help row
    # (which may hand a None style) renders instead of raising.
    lines = wrap_segments([("plain text here", None)], 8, [("  ", None)])
    assert lines  # rendered, no AttributeError
    block = lines[0].to_block(lines[0].width)  # the call that crashed on Span(_, None)
    assert block.height == 1


def test_wrap_segments_empty_input_is_one_prefixed_line():
    lines = wrap_segments([], 20, [("> ", Style())])
    assert len(lines) == 1
    assert _line_text(lines[0]) == "> "
