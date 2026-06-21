"""Report-structure atoms — a section heading and an aligned key:value listing.

These are the two shapes a status report is made of: titled sections, each
section a key:value listing or a list of free-form lines. The deferred
``StatusReport`` will compose them; today they style the CLI's report surfaces
(db dry-runs, ``install``, ``config tag-prefixes``) so they read as one designed
system rather than plain text dropped above a styled table.

The visual language is **accent-led**, echoing ``output.table``: the structural
elements — section titles and the left/key column — take the NORD accent (the
``label`` domain role, bold), and the data — values, listing right-hand sides —
renders plain. ``output.table`` already paints its header accent and its rows
plain; headings and listings now rhyme with it.

Both atoms compose painted's already-public ``Line``/``Span``/``join_vertical``
+ ``display_width`` (the ``output.live`` precedent — no painted change), return a
``Block`` (not printed text), and stay CJK/ANSI-correct. Colour is auto-stripped
by ``print_block`` for a non-TTY / ``NO_COLOR``; a caller that wants a different
treatment (e.g. plain labels for a machine-ish surface) passes ``label_style``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from painted import Block, Style


def _oneline(s: object) -> str:
    """Flatten a cell to a single visual line so newlines can't break alignment.

    A key or value carrying ``\\n`` / ``\\r`` would otherwise split one logical
    row into several visual lines (the tail flush at column 0), destroying the
    column alignment and the one-row-per-pair contract. Embedded breaks collapse
    to a space — a listing cell is intrinsically single-line.
    """
    return str(s).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def heading(text: str) -> Block:
    """A section title — accent (bold), flush-left, matching the table header.

    The structural top of a report section. Sits above a ``definitions`` listing
    or a ``table`` and shares their accent so the section reads as one unit.
    """
    from painted import Line, Span

    from siftd.output.theme import domain_styles

    line = Line(spans=(Span(_oneline(text), domain_styles().label),))
    return line.to_block(line.width)


def print_heading(text: str) -> None:
    """Render and print an accent section title to stdout."""
    from painted import print_block

    print_block(heading(text))


def definitions(
    pairs: Mapping[str, str] | Iterable[tuple[str, str]],
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
    default (the house style — it rhymes with the table header and section
    headings); ``value_style`` defaults to plain. Pass ``label_style`` to opt out
    (e.g. a machine-ish surface that wants plain keys). An empty input renders an
    empty (zero-row) block — an empty section contributes nothing.
    """
    from collections.abc import Mapping

    from painted import Block, Line, Span, Style, join_vertical
    from painted.core._text_width import display_width

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
        val_s = _oneline(value)
        # Pad to display width, not len() — the alignment padding is unstyled so
        # trailing styled whitespace never bleeds colour past the value.
        align = " " * (label_w - display_width(key_s))
        line = Line(
            spans=(
                Span(pad, plain),
                Span(key_s, lbl_style),
                Span(align + sep, plain),
                Span(val_s, val_style),
            )
        )
        rows.append(line.to_block(line.width))
    return join_vertical(*rows)


def print_definitions(
    pairs: Mapping[str, str] | Iterable[tuple[str, str]],
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
