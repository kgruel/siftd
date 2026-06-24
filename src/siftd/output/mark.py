"""The brand mark — the ``sift▪d`` wordmark.

The one identity atom. The wordmark the ``--version`` lockup and the ``--help``
masthead lead with: "sift" and "d" pop by WEIGHT (the structure role — bold over
the cream body substrate, the theme's "structure pops by weight, not colour"
law), and the grain between them is the single gold speck — the metric ("sifting
for gold") tone spent once on identity. The mark literally enacts the brand:
sift → ▪ (the gold) → the thing surfaced.

Composition, not chrome: it builds ``(text, style)`` segments for the shared
``output.row`` atom out of roles the theme already defines —
``palette.text.merge(palette.accent)`` is bold cream (substrate + weight, no new
role) and ``domain_styles().metric_strong`` is the gold the metric thread already
reserves. No new colour, no new role, no painted change.

The grain is siftd-identity, so painted's ``IconSet`` has no slot for it and this
owns its degradation directly: on a non-Unicode / piped stdout (``prefers_ascii``)
the gold speck can render neither its glyph nor its hue, so the grain segment is
empty — ``row_line`` drops it — and the mark collapses to the plain program name
``siftd``, the honest, machine-clean fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from painted import Line, Style

# U+25AA BLACK SMALL SQUARE — the gold speck between t and d. A nugget, not a dot
# (the lane legend's separator is the middot ·); the smallest filled square reads
# as a speck at cell size.
GRAIN = "▪"


def wordmark_segments(*, as_ascii: bool | None = None) -> list[tuple[str, Style | None]]:
    """The ``sift▪d`` wordmark as ``(text, style)`` segments for ``row_line``.

    The letters take bold cream — ``palette.text`` (the body substrate) merged
    with ``palette.accent`` (the structure weight) — so the mark composes two
    roles the theme already owns rather than naming a new one. The grain takes
    the gold ``metric_strong`` tone (the unified-gold decision: identity and the
    "gold you sift for" are one thread). On ``as_ascii`` the grain segment is
    empty and the mark reads as ``siftd``. ``as_ascii`` defaults to the live
    stdout capability (``prefers_ascii``); pass it to force the form (tests, or a
    known stream).
    """
    from painted import current_palette

    from siftd.output.common import prefers_ascii
    from siftd.output.theme import domain_styles

    ascii_form = prefers_ascii() if as_ascii is None else as_ascii
    palette = current_palette()
    letters = palette.text.merge(palette.accent)  # bold cream — substrate + weight
    grain = domain_styles().metric_strong          # the gold speck
    return [
        ("sift", letters),
        ("" if ascii_form else GRAIN, grain),
        ("d", letters),
    ]


def wordmark(*, as_ascii: bool | None = None) -> Line:
    """The ``sift▪d`` wordmark as a composable ``Line`` — the standalone lockup."""
    from siftd.output.row import row_line

    return row_line(wordmark_segments(as_ascii=as_ascii))
