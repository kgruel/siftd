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


def test_heading_is_accent_title_over_an_underline():
    # A section title: accent text on line 0, a ─ rule of matching width on
    # line 1 (the table-header look). Newlines in the title collapse.
    with use_theme(siftd_theme):
        block = heading("row counts:\nleak")
        rendered = _ansi(block)
    lines = _text(block).splitlines()
    assert lines[0] == "row counts: leak"
    assert set(lines[1]) == {"─"}  # an underline rule, nothing else
    assert len(lines[1]) == len(lines[0])  # spans the title width
    assert "\x1b[" in rendered  # accent escape present


def test_heading_underline_degrades_to_ascii():
    # On a non-Unicode target the ─ rule degrades to - via the ambient IconSet —
    # the global icon lever main() flips on a LANG=C stdout, the same control
    # point the rank rail uses (conftest restores the icons after the test).
    from painted import ASCII_ICONS, use_icons

    with use_theme(siftd_theme):
        use_icons(ASCII_ICONS)
        block = heading("title")
    lines = _text(block).splitlines()
    assert lines[0] == "title"
    assert set(lines[1]) == {"-"}  # ASCII rule, not ─
    assert len(lines[1]) == len("title")


# --- rich (segmented) values --------------------------------------------------


def test_value_may_be_styled_segments():
    # A value can carry a styled run — e.g. a coloured severity glyph ahead of
    # plain text — by passing (text, style) segments instead of a string. The
    # rendered text is their concatenation; the colour rides ANSI, invisible here.
    from painted import Style

    block = definitions([("schema version", [("⚠", Style(fg=1)), (" v11 → v12", None)])])
    assert "⚠ v11 → v12" in _text(block)


def test_segmented_value_aligns_like_a_string_value():
    # A segmented value and the equivalent string render to identical columns —
    # the value's shape never perturbs label alignment.
    plain = _text(definitions([("a", "x"), ("longer", "yy")]))
    mixed = _text(definitions([("a", [("x", None)]), ("longer", "yy")]))
    assert plain == mixed


def test_segment_none_style_inherits_the_row_value_style():
    # Within a segmented value a None style takes the row's value_style; an
    # explicit style overrides. Two renders differing only in that override must
    # differ in their ANSI — proof the glyph carries its own colour.
    from painted import Style

    with use_theme(siftd_theme):
        inherit = _ansi(definitions([("k", [("v", None)])], value_style=Style(fg=2)))
        override = _ansi(definitions([("k", [("v", Style(fg=5))])], value_style=Style(fg=2)))
    assert inherit != override


def test_string_value_path_is_byte_stable_including_empty_value():
    # Pin the legacy string-value composition (the db/install/meta previews) so
    # the row_line/_value_segments rewrite — which DROPS empty-text spans where
    # the old fixed-4-span Line emitted them — stays byte-identical. An empty
    # value contributes a label-only row (its dropped value span is a no-op).
    with use_theme(siftd_theme):
        block = definitions(
            [("source", "/p"), ("schema version", "✓ v11 → v12"), ("empty", "")]
        )
    assert _text(block).splitlines() == [
        "  source" + " " * 10 + "/p",
        "  schema version  ✓ v11 → v12",
        "  empty",
    ]


def test_non_segment_sequence_value_is_stringified_not_split():
    # A value that is a sequence but NOT (text, style) segments — e.g. a tuple of
    # strings — must stringify wholesale (the pre-segment total contract), never
    # mis-split into per-character segments (text='a', style='b', ...).
    block = definitions([("k", ("ab", "cd"))])
    assert "('ab', 'cd')" in _text(block)
