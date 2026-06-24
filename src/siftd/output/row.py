"""The row atom — a styled line of text segments.

The leaf primitive every other output atom composes: a sequence of
``(text, style)`` segments becomes one painted ``Line``. ``definitions`` rows,
table cells (``output.table``), the live step-log (``output.live.text_row``),
and the doctor/search line builders (``doctor.view._line``,
``painted_bridge._line``) were each their own near-identical copy of this; they
now delegate here so there is one definition of "a row of styled text".

A ``Line`` (not a ``Block``) is returned so callers can compose further — join
horizontally, measure ``.width``, ``.truncate`` to a budget — before settling on
a block. ``output.live.text_row`` is the block-returning convenience over this.

Semantics (the union of the four it replaces, behaviour-preserving): empty-text
segments are dropped, a ``None`` style means plain, and an optional ``indent``
prefixes a plain run of spaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from painted import Line, Style


def row_line(
    segments: Iterable[tuple[str, Style | None]], *, indent: str = ""
) -> Line:
    """Build a ``Line`` from ``(text, style)`` segments.

    Empty-text segments are dropped (so optional parts collapse cleanly); a
    ``None`` style renders plain; ``indent`` prepends that many leading spaces as
    a plain span. The single source the table/listing/live/doctor row builders
    delegate to.
    """
    from painted import Line, Span, Style

    plain = Style()
    spans: list[Span] = []
    if indent:
        spans.append(Span(indent, plain))
    spans.extend(Span(text, style or plain) for text, style in segments if text)
    return Line(spans=tuple(spans))
