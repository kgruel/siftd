"""Tests for siftd.output.listing — the aligned key:value report atom.

Guards the contract the deferred StatusReport will build on: labels align to the
widest label's *display* width (not ``len()``, so CJK/wide labels stay flush),
values render verbatim, and the block is one row per pair. The styling (label
takes the accent ``label`` role, value plain) is theme-applied, not hand-coded.
"""

import io

from painted import print_block, use_theme

from siftd.output.listing import definitions, heading
from siftd.output.theme import domain_styles, siftd_theme


def _text(block) -> str:
    return "\n".join(
        "".join(cell.char for cell in block.row(y)).rstrip() for y in range(block.height)
    )


def test_pairs_align_to_widest_label():
    block = definitions([("a", "1"), ("longer", "2"), ("mid", "3")])
    lines = _text(block).splitlines()
    # Every value starts at the same column (label padded to "longer" == 6).
    cols = [ln.index(v) for ln, v in zip(lines, ["1", "2", "3"])]
    assert cols[0] == cols[1] == cols[2]
    assert lines == ["  a       1", "  longer  2", "  mid     3"]


def test_alignment_uses_display_width_not_len():
    # "日本語" is 3 code points but 6 display columns; a len()-based pad would
    # misalign the ASCII rows against it.
    block = definitions([("ascii", "x"), ("日本語", "y")])
    lines = _text(block).splitlines()
    assert lines[0].index("x") == lines[1].index("y")


def test_dict_and_tuples_are_equivalent():
    pairs = [("k1", "v1"), ("k2", "v2")]
    assert _text(definitions(pairs)) == _text(definitions(dict(pairs)))


def test_one_row_per_pair():
    block = definitions([("a", "1"), ("b", "2"), ("c", "3")])
    assert block.height == 3


def test_values_render_verbatim():
    block = definitions([("Conversations", "1 new, 2 replaced, 3 skipped")])
    assert "1 new, 2 replaced, 3 skipped" in _text(block)


def test_empty_input_renders_nothing():
    block = definitions([])
    # An empty section contributes nothing — no crash, no stray blank row.
    assert block.height == 0
    assert _text(block) == ""


def test_newlines_in_cells_collapse_to_one_line():
    # A value carrying a newline must not split a row or break alignment.
    block = definitions([("a", "line1\nline2"), ("b", "v")])
    assert block.height == 2  # still one row per pair
    assert "line1 line2" in _text(block)


def test_indent_and_gutter_are_honoured():
    block = definitions([("k", "v")], indent=4, gutter=3)
    # 4-space indent, label "k", 3-space gutter, value.
    assert _text(block).splitlines()[0] == "    k   v"


def _ansi(block) -> str:
    buf = io.StringIO()
    print_block(block, buf, use_ansi=True)
    return buf.getvalue()


def test_labels_take_the_accent_role_by_default():
    # The house style: the left column takes the accent `label` role, rhyming
    # with the table header. The default renders identically to passing that
    # role explicitly, and differs from an explicit-plain override.
    from painted import Style

    with use_theme(siftd_theme):
        default = _ansi(definitions([("label", "value")]))
        explicit_accent = _ansi(definitions([("label", "value")], label_style=domain_styles().label))
        explicit_plain = _ansi(definitions([("label", "value")], label_style=Style()))
    assert default == explicit_accent
    assert default != explicit_plain
    assert default.index("label") < default.index("value")


def test_label_style_can_opt_out_to_plain():
    # A machine-ish surface can pass a plain style to drop the accent.
    from painted import Style

    with use_theme(siftd_theme):
        accented = _ansi(definitions([("label", "value")]))
        plain = _ansi(definitions([("label", "value")], label_style=Style()))
    assert accented != plain


def test_heading_is_accent_and_single_line():
    # A section title carries the accent role and collapses any newline.
    with use_theme(siftd_theme):
        block = heading("row counts:\nleak")
        rendered = _ansi(block)
    assert _text(block) == "row counts: leak"
    assert "\x1b[" in rendered  # accent escape present
