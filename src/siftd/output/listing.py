"""Report-structure atoms — an underlined section heading and a key:value listing.

The shapes a status report is made of: titled sections, each a key:value listing
(``definitions``) or a list of lines, introduced by a ``heading``. The deferred
``StatusReport`` will compose them; today they style the CLI's report surfaces
(db dry-runs, ``install``, ``config tag-prefixes``) so they read as one designed
system rather than flat text.

The visual language is **accent-led and table-consistent**: a section heading is
an accent title over a thin ``─`` underline — the same rule the gutter table
draws under its header — so a section and a table read as one family. The left
(key) column of a listing takes the accent ``label`` role; values render plain.
Single-column and border-free, so output wraps cleanly and degrades at any width
(no box frame to break on a narrow or non-Unicode terminal).

Both atoms compose the shared ``output.row`` row atom + ``join_vertical`` (no
painted change), return a ``Block`` (not printed text), and stay CJK/ANSI-correct.
A ``definitions`` value may be a plain string or a sequence of ``(text, style)``
segments, so one value can carry a styled run — e.g. a coloured severity glyph
ahead of plain text. Colour is auto-stripped by ``print_block`` for a non-TTY /
``NO_COLOR`` and the ``─`` rule degrades to ``-`` for a non-Unicode target; a
caller that wants plain labels (a machine-ish surface) passes ``label_style``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from painted import Block, Style

    # A listing value: a plain string, or styled segments (a ``None`` style
    # falls back to the row's value style) so a value can carry a coloured run.
    ValueCell = str | Sequence[tuple[str, "Style | None"]]


def _oneline(s: object) -> str:
    """Flatten a cell to a single visual line so newlines can't break alignment.

    A key or value carrying ``\\n`` / ``\\r`` would otherwise split one logical
    row into several visual lines (the tail flush at column 0), destroying the
    column alignment and the one-row-per-pair contract. Embedded breaks collapse
    to a space — a listing cell is intrinsically single-line.
    """
    return str(s).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _value_segments(value: ValueCell, default_style: Style) -> list[tuple[str, Style]]:
    """Normalise a listing value to ``(text, style)`` segments.

    A plain ``str`` becomes one segment in the row's value style. A *non-empty*
    sequence whose every element is a ``(text, style)`` 2-tuple (style a painted
    ``Style`` or ``None``) is taken verbatim as segments — a ``None`` style falls
    back to that default — so one value can carry a styled run (e.g. a coloured
    severity glyph ahead of plain text). Anything else is stringified, keeping the
    helper *total* over any value (the pre-segment contract) so a stray tuple of
    strings degrades to its ``repr`` rather than mis-splitting into per-character
    segments. Each segment's text is flattened to one visual line so a stray
    newline can't break the row.
    """
    from painted import Style

    if not isinstance(value, str):
        try:
            items = list(value)
        except TypeError:
            items = None
        if items and all(
            isinstance(x, tuple)
            and len(x) == 2
            and isinstance(x[0], str)
            and (x[1] is None or isinstance(x[1], Style))
            for x in items
        ):
            return [(_oneline(t), s if s is not None else default_style) for t, s in items]
    return [(_oneline(value), default_style)]


def heading(text: str, *, as_ascii: bool = False) -> Block:
    """A section title — an accent title over a thin ``─`` underline rule.

    The accent title with a muted ``─`` rule beneath it (spanning the title's
    display width), echoing the gutter table's header rule so a section header
    and a table read as one family. Border-free and single-column, so it wraps
    and degrades cleanly at any width. ``as_ascii`` degrades the rule ``─`` →
    ``-`` for a non-Unicode target (``print_heading`` decides it from the stream).
    """
    from painted import current_palette, join_vertical

    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles

    title = _oneline(text)
    title_line = row_line([(title, domain_styles().label)])
    width = title_line.width
    rule = ("-" if as_ascii else "─") * width
    rule_line = row_line([(rule, current_palette().muted)])
    return join_vertical(title_line.to_block(width), rule_line.to_block(width))


def print_heading(text: str) -> None:
    """Render and print an underlined accent section title to stdout."""
    from painted import print_block

    from siftd.output.common import prefers_ascii

    print_block(heading(text, as_ascii=prefers_ascii()))


def definitions(
    pairs: Mapping[str, ValueCell] | Iterable[tuple[str, ValueCell]],
    *,
    indent: int = 2,
    gutter: int = 2,
    label_style: Style | None = None,
    value_style: Style | None = None,
) -> Block:
    """Render ``label → value`` pairs as an aligned, headerless listing ``Block``.

    Labels are padded to the widest label's *display* width (wcwidth-correct, not
    ``len()``) and separated from values by ``gutter`` spaces, the whole block
    indented by ``indent``. The left column takes the accent ``label`` role by
    default (the house style — it rhymes with section headings); ``value_style``
    defaults to plain. Pass ``label_style`` to opt out (e.g. a machine-ish surface
    that wants plain keys). A value may be a plain string or ``(text, style)``
    segments (see ``_value_segments``) so it can carry a styled run. An empty
    input renders an empty (zero-row) block — an empty section contributes nothing.
    """
    from collections.abc import Mapping

    from painted import Block, Style, join_vertical
    from painted.core._text_width import display_width

    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles

    items = list(pairs.items()) if isinstance(pairs, Mapping) else list(pairs)
    if not items:
        return Block.empty(0, 0)

    plain = Style()
    lbl_style = label_style if label_style is not None else domain_styles().label
    val_style = value_style if value_style is not None else plain
    pad = " " * indent
    sep = " " * gutter
    keys = [_oneline(k) for k, _ in items]
    label_w = max(display_width(k) for k in keys)

    rows: list[Block] = []
    for key_s, (_, value) in zip(keys, items):
        # Pad to display width, not len() — the alignment padding is unstyled so
        # trailing styled whitespace never bleeds colour past the value.
        align = " " * (label_w - display_width(key_s))
        line = row_line([
            (pad, plain),
            (key_s, lbl_style),
            (align + sep, plain),
            *_value_segments(value, val_style),
        ])
        rows.append(line.to_block(line.width))
    return join_vertical(*rows)


def print_definitions(
    pairs: Mapping[str, ValueCell] | Iterable[tuple[str, ValueCell]],
    *,
    indent: int = 2,
    gutter: int = 2,
    label_style: Style | None = None,
    value_style: Style | None = None,
) -> None:
    """Render and print an aligned key:value listing to stdout.

    Forwards ``label_style`` / ``value_style`` so a caller can override the
    accent-label default without dropping to ``definitions`` + ``print_block``.
    """
    from painted import print_block

    print_block(
        definitions(
            pairs,
            indent=indent,
            gutter=gutter,
            label_style=label_style,
            value_style=value_style,
        )
    )
