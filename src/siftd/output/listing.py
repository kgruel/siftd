"""Aligned key:value listing — the report's key:value atom.

A headerless, borderless list of ``label → value`` pairs, labels padded to a
common display width. This is the smaller sibling of ``output.table`` (a
*bordered, width-budgeted data table* with a header, a ``Fill`` column and
overflow handling): a listing has no header, no width budget and no flex column
— just wcwidth-correct label alignment.

It is *one* of the shapes the deferred ``StatusReport`` will compose: a status
report is a sequence of titled sections, and a section is either a key:value
listing (this) or a list of free-form lines (a sibling atom, not yet built). So
this covers the key:value half — not a whole report on its own.

Labels render **plain by default**, matching what every current report-ish
surface already does — ``cmd_status`` prints its fields plain, ``output.table``
left-aligns text columns plain — and matching the plain ``print()`` these call
sites replaced. The left column is sometimes a field label and sometimes data
(a tag name, a component/harness key, an argument the user types), so the atom
takes no styling opinion; a caller that genuinely wants field emphasis passes
``label_style`` explicitly. Output rides the ambient NORD theme regardless, and
colour is auto-stripped by ``print_block`` for a non-TTY / ``NO_COLOR``.

Composes painted's already-public ``Line``/``Span``/``join_vertical`` +
``display_width`` (the ``output.live`` precedent — no painted change), so the
listing returns a ``Block`` (not printed text) and stays CJK/ANSI-correct.
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
    indented by ``indent``. ``label_style`` / ``value_style`` both default to
    plain; pass ``label_style`` (e.g. the ``label`` domain role) only for a
    surface whose left column is a genuine field label that wants emphasis. An
    empty input renders an empty (zero-row) block — an empty section contributes
    nothing.
    """
    from collections.abc import Mapping

    from painted import Block, Line, Span, Style, join_vertical
    from painted.core._text_width import display_width

    items = list(pairs.items()) if isinstance(pairs, Mapping) else list(pairs)
    if not items:
        return Block.empty(0, 0)

    plain = Style()
    lbl_style = label_style if label_style is not None else plain
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

    Forwards ``label_style`` / ``value_style`` so a caller can emphasise a
    genuine field-label column without dropping to ``definitions`` + ``print_block``.
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
