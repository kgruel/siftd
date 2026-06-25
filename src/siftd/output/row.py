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

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from painted import Line, Span, Style


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


def wrap_spans(spans: Sequence[Span], width: int) -> list[Line]:
    """Word-wrap styled spans into ``Line``s of ``<= width`` display columns.

    Preserves each span's style across wrap boundaries; a single token wider than
    ``width`` (an unbroken JSON blob, a long flag list) is hard-split so it never
    overflows. The wcwidth-correct word-wrap shared by the markdown body renderer,
    the search snippet expander, and the help body — the sibling of ``row_line``
    (which builds one line; this breaks a styled run across many). Returns at least
    one (possibly empty) line.
    """
    from painted import Line, Span
    from painted.core._text_width import display_width

    out_lines: list[list[Span]] = []
    cur: list[Span] = []
    cur_w = 0
    for sp in spans:
        for tok in re.findall(r"\S+|\s+", sp.text):
            tw = display_width(tok)
            if tok.isspace():
                if cur_w + tw <= width:
                    cur.append(Span(tok, sp.style))
                    cur_w += tw
                else:
                    out_lines.append(cur)
                    cur, cur_w = [], 0
                continue
            if cur and cur_w + tw > width:
                out_lines.append(cur)
                cur, cur_w = [], 0
            if tw > width:  # token longer than a whole line — hard-split it
                buf, bw = "", 0
                for ch in tok:
                    cw = display_width(ch)
                    if buf and bw + cw > width:
                        cur.append(Span(buf, sp.style))
                        out_lines.append(cur)
                        cur, cur_w = [], 0
                        buf, bw = "", 0
                    buf += ch
                    bw += cw
                if buf:
                    cur.append(Span(buf, sp.style))
                    cur_w += bw
            else:
                cur.append(Span(tok, sp.style))
                cur_w += tw
    if cur:
        out_lines.append(cur)
    return [Line(spans=tuple(line)) for line in out_lines] or [Line(spans=())]


def wrap_segments(
    segments: Iterable[tuple[str, Style | None]],
    width: int | None,
    first_prefix: Sequence[tuple[str, Style | None]] = (),
    cont_prefix: Sequence[tuple[str, Style | None]] = (),
) -> list[Line]:
    """Word-wrap ``(text, style)`` segments to ``width``, prefixing each line.

    ``first_prefix`` / ``cont_prefix`` are ``(text, style)`` segment lists prepended
    to the first and continuation lines respectively (a body indent, a list marker,
    or an aligned key column whose width the continuations pad to). The first
    prefix's display width is reserved out of ``width`` so the wrapped run never
    collides with it. Reuses ``wrap_spans`` so inline styling survives wrap
    boundaries. A ``None`` style renders plain. The aligned-continuation layout
    shared by the markdown list/quote renderer and the help body's two-column rows.
    """
    from painted import Line, Span, Style
    from painted.core._text_width import display_width

    plain = Style()
    spans = [Span(t, s or plain) for t, s in segments if t]
    pfx_w = sum(display_width(t) for t, _ in first_prefix)
    avail = max(1, (width or 80) - pfx_w)
    wrapped = wrap_spans(spans, avail) if spans else [Line(spans=())]
    out: list[Line] = []
    for i, ln in enumerate(wrapped):
        pfx = first_prefix if i == 0 else cont_prefix
        pspans = tuple(Span(t, s or plain) for t, s in pfx if t)
        out.append(Line(spans=pspans + ln.spans))
    return out
