"""Canonical CLI table rendering — one width-budgeted painted table.

Every tabular CLI surface (``query``/``peek`` lists, ``report`` results,
``adapters``, ``doctor --list``) renders through this module so they share one
layout discipline:

- columns size to their content (``AUTO``), wcwidth-correct;
- exactly one free-text column flexes to absorb the leftover budget (``Fill``),
  with a ``min_width`` floor so it never collapses;
- quantities right-align, labels left-align;
- the table is budgeted to the terminal via painted's ``Overflow.FIT`` — the
  ``Fill`` column shrinks (ellipsized) to absorb overflow, and the table
  overflows rather than clipping when it can't, so deep workspace paths
  ellipsize and no column is ever silently dropped.

Separators are spaced gutters, not box rules — *box a fixed set, never a feed*.
On a non-TTY the width budget drops to ``None`` (natural sizing) so piped /
machine output is never truncated.

This is the general form of the hand-rolled, ``len()``-based table algorithms it
replaced: one painted ``table(overflow=FIT)`` call — the responsive width
resolution, the shrink-to-fit policy, and the ellipsis all owned by painted.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from siftd.output.common import prefers_ascii, term_width

if TYPE_CHECKING:
    from painted import Align, Block, Style


# A spaced-gutter separator (two columns of padding) plus a thin per-column
# rule, instead of painted's default │/┼ box grid. ``crossing`` is a single
# char — painted repeats it ``sep_width`` times under the header rule, so it
# must stay one display column wide to line up with ``vertical``. The rule
# degrades ``─`` → ``-`` when ``as_ascii`` (a pipe or a non-UTF-8 TTY) — the only
# Unicode glyph this BORDER draws. (The table's own ``…`` truncation marker is a
# painted-owned glyph ``as_ascii`` can't reach; ``print_table`` sidesteps it by
# not budgeting an incapable stream, so no ``…`` is ever drawn there.)
def _gutter_borders(as_ascii: bool = False):
    from painted import BorderChars

    return BorderChars("", "", "", "", "-" if as_ascii else "─", "  ", " ")


@dataclass(frozen=True)
class Col:
    """A table column: a header, a per-row cell accessor, and a width policy.

    Composes painted's ``Column`` (the width/align/min_width track-sizing) with
    the data accessor (``cell``) and per-cell ``style`` that painted's ``Column``
    has no notion of. ``fill=True`` marks the single free-text column that flexes
    to the leftover budget; ``ellipsis_left`` keeps the *tail* when that cell is
    truncated, so a dash-encoded workspace leaf (``…Code/siftd-7``) survives.
    """

    header: str
    cell: Callable[[Any], str]
    style: Style | None = None
    align: Align | None = None  # None → START
    fill: bool = False
    min_width: int | None = None
    ellipsis_left: bool = False


def render_table(
    cols: list[Col], items: list, *, width: int | None, as_ascii: bool = False
) -> Block:
    """Render ``items`` as a width-honest painted table from ``Col`` specs.

    ``width`` is the terminal budget, or ``None`` for natural sizing (the piped /
    non-TTY escape — never truncates). The layout policy lives in painted's
    ``Overflow.FIT``: a ``Fill`` column sizes to its content and shrinks
    (ellipsized on its side) to absorb overflow, never stretching into slack;
    and when the non-fill columns alone exceed the budget the table overflows at
    natural width rather than clipping — so no column or value is silently
    dropped. ``ellipsis_left`` keeps the tail (a workspace leaf survives);
    otherwise the head is kept (prose). ``as_ascii`` degrades the header rule to
    ``-`` for a non-Unicode target (``print_table`` decides it at the stream).
    """
    from painted import Align, Line, Span, Style
    from painted.views import AUTO, Column, EllipsisSide, Fill, Overflow, TableState, table

    style_for = lambda c: c.style if c.style is not None else Style()  # noqa: E731
    align_for = lambda c: c.align if c.align is not None else Align.START  # noqa: E731

    columns = [
        Column(
            header=Line(spans=(Span(c.header, Style()),)),
            width=Fill() if c.fill else AUTO,
            align=align_for(c),
            min_width=c.min_width,
            ellipsis=True,
            ellipsis_side=EllipsisSide.START if c.ellipsis_left else EllipsisSide.END,
        )
        for c in cols
    ]

    rows: list[list[Line]] = [
        [_styled_line(c.cell(item), style_for(c)) for c in cols] for item in items
    ]

    state = TableState().with_count(len(rows)).with_visible(len(rows))
    return table(
        state,
        columns,
        rows,
        visible_height=len(rows),
        width=width,
        overflow=Overflow.FIT,
        borders=_gutter_borders(as_ascii),
        selected_style=Style(),
    )


def render_string_table(
    headers: list[str], rows: list[list[str]], *, width: int | None, as_ascii: bool = False
) -> Block:
    """Render pre-stringified columns as a table, inferring the width policy.

    For callers that only have strings (``report`` SQL results, ``adapters``,
    ``doctor --list``): numeric columns right-align and carry the amber ``metric``
    role (consistent with the ``Col``-spec ``render_table`` path, where a count is
    gilded), and the widest *non-numeric* column is the flex (``Fill``) candidate
    so free text — never a quantity — absorbs any overflow. An all-numeric table
    gets no flex column (every column sizes to content). The same ``_is_numeric_col``
    inference drives both the right-align and the metric hue; text cells render
    verbatim (no styling guessed onto them).
    """
    from painted import Align
    from painted.core._text_width import display_width

    from siftd.output.theme import domain_styles

    ds = domain_styles()
    ncols = len(headers)
    col_cells = [[(row[j] if j < len(row) else "") for row in rows] for j in range(ncols)]
    numeric = [_is_numeric_col(col_cells[j]) for j in range(ncols)]

    def natural(j: int) -> int:
        return max([display_width(headers[j]), *(display_width(c) for c in col_cells[j])])

    # Flex only a free-text column; a numeric column must never become Fill (it
    # would float its quantity to the far edge). No text column → no Fill.
    text_cols = [j for j in range(ncols) if not numeric[j]]
    fill_idx = max(text_cols, key=natural) if text_cols else None

    cols = [
        Col(
            header=headers[j],
            cell=_index_getter(j),
            align=Align.END if numeric[j] else Align.START,
            fill=(j == fill_idx),
            min_width=10 if j == fill_idx else None,
            style=ds.metric if numeric[j] else None,
        )
        for j in range(ncols)
    ]
    return render_table(cols, rows, width=width, as_ascii=as_ascii)


def table_budget() -> tuple[int | None, bool]:
    """The ``(width, as_ascii)`` a painted table should render at for stdout.

    The single point of control for the table layout decision, shared by
    ``print_table`` and the block-returning ``render_table`` callers (the
    ``query``/``peek`` lists and the ``ingest`` summary). A capable TTY keeps the
    terminal width budget — painted may ellipsize an over-budget column with the
    painted-owned ``…`` marker. An incapable stream (the ``prefers_ascii`` case:
    a pipe, or a non-UTF-8 ``LANG=C`` TTY) drops the budget to natural sizing, so
    nothing truncates — no ``…`` to garble, or to *crash* a strict-ASCII stream
    mid-table — and the header rule degrades to ``-`` via ``as_ascii``.
    """
    ascii_mode = prefers_ascii()
    width = term_width() if (sys.stdout.isatty() and not ascii_mode) else None
    return width, ascii_mode


def print_table(columns: list[str], rows: list[list[str]]) -> None:
    """Render and print a string table, budgeted to the terminal on a TTY.

    The painted-backed replacement for the old ``len()``-based string table:
    same call shape, but wcwidth-correct, width-budgeted (via ``table_budget``),
    and themed. Non-TTY / non-Unicode output keeps natural widths (no truncation,
    so pipes stay machine-readable and no ``…`` crashes a strict-ASCII stream)
    and the header rule degrades to ``-``.
    """
    from painted import print_block

    from siftd.output.common import should_use_ansi

    width, ascii_mode = table_budget()
    block = render_string_table(columns, rows, width=width, as_ascii=ascii_mode)
    print_block(block, use_ansi=should_use_ansi())


# --- helpers ---------------------------------------------------------------

# A formatted quantity: an ungrouped digit run or proper thousands-grouped
# triples (so "1,234" matches but list-ish "1,2,3" / "12," do not), with an
# optional sign, decimal, k/M/B/% suffix, or leading "$".
_NUMERIC_RE = re.compile(r"^[$+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[kKmMbB%]?$")


def _is_numeric_col(cells: list[str]) -> bool:
    """True when every non-empty cell looks like a quantity (→ right-align)."""
    seen = [c.strip() for c in cells if c.strip()]
    return bool(seen) and all(_NUMERIC_RE.match(c) for c in seen)


def _index_getter(j: int) -> Callable[[list[str]], str]:
    return lambda row: row[j] if j < len(row) else ""


def _styled_line(text: str, style: Style):
    # A single-segment row — the shared row atom (empty text → an empty Line).
    from siftd.output.row import row_line

    return row_line([(text, style)])
