"""Canonical CLI table rendering — one width-budgeted painted table.

Every tabular CLI surface (``query``/``peek`` lists, ``report`` results,
``adapters``, ``doctor --list``) renders through this module so they share one
layout discipline:

- columns size to their content (``AUTO``), wcwidth-correct;
- exactly one free-text column flexes to absorb the leftover budget (``Fill``),
  with a ``min_width`` floor so it never collapses;
- quantities right-align, labels left-align;
- the whole grid is budgeted to the terminal width, so deep workspace paths
  ellipsize instead of overflowing the line.

Separators are spaced gutters, not box rules — *box a fixed set, never a feed*.
On a non-TTY the width budget drops to ``None`` (natural sizing) so piped /
machine output is never truncated.

This is the general form of three hand-rolled, ``len()``-based width algorithms
(``painted_bridge._styled_table``, ``common.format_table``): one painted
``table()`` call, the responsive width resolution owned by painted.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from siftd.output.common import term_width

if TYPE_CHECKING:
    from painted import Align, Block, Style


# A spaced-gutter separator (two columns of padding) plus a thin per-column
# rule, instead of painted's default │/┼ box grid. ``crossing`` is a single
# char — painted repeats it ``sep_width`` times under the header rule, so it
# must stay one display column wide to line up with ``vertical``.
def _gutter_borders():
    from painted import BorderChars

    return BorderChars("", "", "", "", "─", "  ", " ")


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


def render_table(cols: list[Col], items: list, *, width: int | None) -> Block:
    """Render ``items`` as a painted table from ``Col`` specs.

    ``width`` is the terminal budget, or ``None`` for natural sizing (the piped /
    non-TTY escape). The budget is only *engaged* — letting the one ``fill``
    column flex — when the natural table is wider than the terminal AND the
    non-fill columns fit so the fill can absorb the overflow. When the content
    already fits, or when there are simply too many columns for the terminal,
    the table renders at natural width: compact, or soft-wrapping, but never
    clipping data. So painted's whole-block right-clip can't silently drop
    columns or values. The fill cell is pre-ellipsized to its resolved width
    (tail kept for paths via ``ellipsis_left``, head kept for prose).
    """
    from painted import Align, Line, Span, Style
    from painted.core._text_width import display_width
    from painted.views import AUTO, Column, Fill, TableState, table

    style_for = lambda c: c.style if c.style is not None else Style()  # noqa: E731
    align_for = lambda c: c.align if c.align is not None else Align.START  # noqa: E731

    columns = [
        Column(
            header=Line(spans=(Span(c.header, Style()),)),
            width=Fill() if c.fill else AUTO,
            align=align_for(c),
            min_width=c.min_width,
        )
        for c in cols
    ]

    # Cell text grid (one pass over the data).
    grid: list[list[str]] = [[c.cell(item) for c in cols] for item in items]

    fill_idx = next((j for j, c in enumerate(cols) if c.fill), None)
    sep_w = display_width(_gutter_borders().vertical)
    eff = _effective_width(columns, grid, width, fill_idx, cols, sep_w)

    # When the budget is engaged, pre-ellipsize the fill cell to its resolved
    # width via painted's own resolver (no duplicated budget math), since the
    # non-fill columns are guaranteed to fit and only the fill cell can exceed
    # its slot — painted would right-clip it with no marker.
    if eff is not None and fill_idx is not None:
        from painted.views.components._table import resolve_column_widths

        line_rows = [[Line.plain(cell) for cell in row] for row in grid]
        resolved = resolve_column_widths(columns, line_rows, available=eff, sep_width=sep_w)
        wj = resolved[fill_idx]
        left = cols[fill_idx].ellipsis_left
        for row in grid:
            row[fill_idx] = _ellipsize(row[fill_idx], wj, left=left)

    rows: list[list[Line]] = [
        [_styled_line(cell, style_for(c)) for cell, c in zip(row, cols)]
        for row in grid
    ]

    state = TableState().with_count(len(rows)).with_visible(len(rows))
    return table(
        state,
        columns,
        rows,
        visible_height=len(rows),
        width=eff,
        borders=_gutter_borders(),
        selected_style=Style(),
    )


def _effective_width(columns, grid, width, fill_idx, cols, sep_w):
    """The width to render at: the budget when the fill can absorb real overflow,
    else ``None`` (natural — compact if it fits, soft-wrapping if it can't).

    Engaging painted's budget makes the single fill column flex to consume the
    leftover space; doing that when the table already fits would *balloon* a
    narrow column to full width, and doing it when the bounded columns alone
    overflow would *clip* trailing columns. Both are avoided here.
    """
    if width is None:
        return None
    from painted import Line
    from painted.views.components._table import resolve_column_widths

    line_rows = [[Line.plain(cell) for cell in row] for row in grid]
    natural = resolve_column_widths(columns, line_rows, available=None, sep_width=sep_w)
    natural_total = sum(natural) + sep_w * (len(columns) - 1)
    if natural_total <= width:
        return None  # already fits — natural, no ballooning
    if fill_idx is None:
        return None  # nothing to absorb overflow — natural, soft-wrap (no clip)
    floor = cols[fill_idx].min_width or 1
    nonfill_total = natural_total - natural[fill_idx]
    if nonfill_total + floor <= width:
        return width  # the fill column can absorb the overflow
    return None  # bounded columns already overflow — natural, don't clip data


def render_string_table(
    headers: list[str], rows: list[list[str]], *, width: int | None
) -> Block:
    """Render pre-stringified columns as a table, inferring the width policy.

    For callers that only have strings (``report`` SQL results, ``adapters``,
    ``doctor --list``): numeric columns right-align, and the widest *non-numeric*
    column is the flex (``Fill``) candidate so free text — never a quantity —
    absorbs any overflow. An all-numeric table gets no flex column (every column
    sizes to content). This is a deterministic layout policy over a known
    tabular shape, not a guess at what the data *means* — cells render verbatim.
    """
    from painted import Align
    from painted.core._text_width import display_width

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
        )
        for j in range(ncols)
    ]
    return render_table(cols, rows, width=width)


def print_table(columns: list[str], rows: list[list[str]]) -> None:
    """Render and print a string table, budgeted to the terminal on a TTY.

    The painted-backed replacement for the old ``len()``-based string table:
    same call shape, but wcwidth-correct, width-budgeted, and themed. Non-TTY
    output keeps natural widths (no truncation) so pipes stay machine-readable.
    """
    from painted import print_block

    budget = term_width() if sys.stdout.isatty() else None
    block = render_string_table(columns, rows, width=budget)
    print_block(block)


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
    from painted import Line, Span

    return Line(spans=(Span(text, style),)) if text else Line(spans=())


def _ellipsize(text: str, max_width: int, *, left: bool, ellipsis: str = "…") -> str:
    """Truncate ``text`` to ``max_width`` display columns with an ellipsis.

    ``left=True`` keeps the *tail* (a path leaf — the inverse of painted's
    right-only ``truncate``); ``left=False`` keeps the *head* (prose). Always
    display-width aware so a CJK/emoji cell never overruns its slot.
    """
    from painted.core._text_width import display_width

    if display_width(text) <= max_width:
        return text
    ell_w = display_width(ellipsis)
    budget = max_width - ell_w if max_width > ell_w else max_width
    show_ellipsis = max_width > ell_w
    kept: list[str] = []
    used = 0
    for ch in reversed(text) if left else text:
        cw = display_width(ch) or 1
        if used + cw > budget:
            break
        kept.append(ch)
        used += cw
    if left:
        body = "".join(reversed(kept))
        return (ellipsis + body) if show_ellipsis else body
    body = "".join(kept)
    return (body + ellipsis) if show_ellipsis else body
