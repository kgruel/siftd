"""The grain gutter — a per-line left-margin mark encoding a narrative line's kind.

One orthogonal dimension down the transcript's left edge: who/what produced each
line — the user, the assistant, its reasoning, or a tool call — with tool calls
refined by outcome (pass/fail). A reader scans the rail to navigate a long
transcript: failures (``✗``) and user turns (a bright ``▪``) jump out without
reading the body, and a big block of reasoning (``·``) reads as skippable.

painted supplies the mechanism (``apply_gutter`` builds the height-matched rail,
the glyph on every line); this module authors the one thing painted can't — the
narrative-taxonomy → glyph+style map, since the kinds are siftd's own domain.

Glyphs degrade to ASCII (``▪→*``, ``·→.``, ``✓→+``, ``✗→x``) via the ``ascii``
flag threaded in the payload, so a pipe or a non-UTF-8 ``LANG=C`` stream stays
clean — the same capability gate the rest of the output layer uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from painted import Block, Style

# Tool outcomes that read as a failure (✗, the reserved red); anything else that
# ran is treated as a pass (✓) — no news from a completed call is good news.
_ERROR_STATUSES = frozenset({"error", "failed", "failure"})

# 2 columns: the glyph plus its trailing space. The narrative renders its content
# at width - GUTTER_COLS so the rail-prefixed line lands at exactly the width.
GUTTER_COLS = 2


def gutter_event_kind(kind: str, payload: dict) -> tuple[str, Style]:
    """Map a narrative line's kind to its gutter ``(glyph, style)``.

    The siftd-authored ``GutterFn``: ``user`` is bright cream (the human turn),
    ``assistant`` recedes to the secondary weight, ``thinking`` is a distinct
    lighter ``·`` (skippable reasoning), and a ``tool`` line takes its outcome —
    ``✓`` teal pass / ``✗`` red fail — read from ``payload["status"]``. ``ascii``
    in the payload degrades each glyph to its plain form.
    """
    from painted import current_palette

    from siftd.output.theme import domain_styles

    ds = domain_styles()
    p = current_palette()
    a = bool(payload.get("ascii"))

    if kind == "user":
        return ("*" if a else "▪"), ds.assistant
    if kind == "thinking":
        return ("." if a else "·"), ds.label
    if kind == "tool":
        status = str(payload.get("status") or "").lower()
        if status in _ERROR_STATUSES:
            return ("x" if a else "✗"), p.error
        return ("+" if a else "✓"), p.success
    # assistant (text, fallbacks, headers) — the recessed default
    return ("*" if a else "▪"), ds.label


def apply_event_gutter(
    block: Block, kind: str, *, status: str | None = None, ascii_mode: bool = False
) -> Block:
    """Prepend the grain-gutter rail to ``block`` for a run of one ``kind``.

    Composes painted's ``apply_gutter`` with ``gutter_event_kind`` — the rail
    matches the block's height (the glyph on every line), so a multi-line block
    carries a continuous mark. ``status`` selects a tool line's ✓/✗; ``ascii``
    degrades the glyph.
    """
    from painted.views import apply_gutter

    return apply_gutter(
        block, kind, {"status": status, "ascii": ascii_mode}, gutter_event_kind
    )
