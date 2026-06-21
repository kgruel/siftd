"""Live-render policy over painted's ``InPlaceRenderer``.

painted owns the ephemeral-live MECHANISM: ``InPlaceRenderer`` re-paints Block
frames in place in the scrollback (row-diffed, DEC-2026 synchronized atomic
writes) and ``finalize()`` deposits the final frame as static history. Per
painted's two-tier contract (``docs/LIVE_DELIVERY_DESIGN.md``) this is the tier
for "spinners, progress bars, short-lived status — anything where the final
state belonging to terminal history is the point, and nobody scrolls mid-run":
exactly a finite ``ingest`` / ``doctor --fix`` run, where the terminal is ours
alone for the duration and the final summary belongs in scrollback.

This module owns the siftd POLICY painted has no concept of:

  - **the degrade gate** — drive the renderer only on a Unicode-capable TTY
    (``isatty() and supports_unicode()``, the same gate doctor and status use).
    A pipe / ``--json`` / non-UTF-8 locale leaves ``active`` False and the
    caller renders its existing plain path; the live region never fires.
  - **throttling** — a fast event source (``ingest`` emits far faster than the
    eye can track; skips dominate) repaints at most every ``min_interval``
    seconds. Terminal moments (an adapter finishing, an error) pass
    ``force=True`` and always paint.
  - **cursor safety** — the cursor is restored even if the body raises.

Two consumers share this policy, each owning its own block shape: ``ingest``
(per-adapter progress bars) and ``doctor --fix`` (a spinner step-log). The
shared substance is the InPlaceRenderer-driving policy, not the block shapes —
so the row builders here (``bar_row`` / ``text_row``) are thin compositions of
painted primitives (``progress_bar`` / ``Line``) the consumers fill in. The
bars and glyphs inherit the ambient NORD palette + IconSet, so colour and
ASCII degradation come for free.
"""

from __future__ import annotations

import logging
import sys
from time import perf_counter
from typing import TYPE_CHECKING

from siftd.output.common import supports_unicode

if TYPE_CHECKING:
    from typing import TextIO

    from painted import Block, Style


class _BufferHandler(logging.Handler):
    """Captures records instead of emitting them, for later replay."""

    def __init__(self, buffer: list[logging.LogRecord]) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(record)


class _LogQuiesce:
    """Hold the ``siftd`` logger's output while a live region owns the terminal.

    A stray stderr write *during* an ``InPlaceRenderer`` frame scrolls the
    viewport and tears the relative-addressed repaint (the documented fragility
    of the ephemeral tier). While active we buffer the ``siftd`` logger's
    records — adapters and ingest log warnings mid-run — and replay them once the
    region has closed, so they land *below* the deposited final frame instead of
    shredding it. Records are never dropped; only deferred.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("siftd")
        self._buffer: list[logging.LogRecord] = []
        self._saved_handlers: list[logging.Handler] | None = None
        self._saved_propagate = True

    def __enter__(self) -> _LogQuiesce:
        self._saved_handlers = self._logger.handlers[:]
        self._saved_propagate = self._logger.propagate
        self._logger.handlers = [_BufferHandler(self._buffer)]
        self._logger.propagate = False  # don't let root handlers emit either
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._saved_handlers is not None:
            self._logger.handlers = self._saved_handlers
            self._logger.propagate = self._saved_propagate
            for record in self._buffer:
                for handler in self._saved_handlers:
                    if record.levelno >= handler.level:
                        handler.handle(record)
            self._buffer.clear()
            self._saved_handlers = None
        return False


class LiveRegion:
    """siftd's gate + throttle + lifecycle over painted's ``InPlaceRenderer``.

    Usage::

        live = LiveRegion(enabled=not quiet)
        if live.active:
            with live:
                for ev in events:
                    ...update state...
                    live.update(build_block())          # throttled
                live.finalize(build_block(done=True))    # deposit final frame
        else:
            ...caller's existing plain path...

    ``active`` folds the whole gate (enabled + Unicode TTY) into one question so
    the caller branches once. Entering the context is safe either way: when
    inactive, ``__enter__`` / ``update`` / ``finalize`` are no-ops.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        min_interval: float = 0.08,
        enabled: bool = True,
    ) -> None:
        # Resolve sys.stdout at call time, not at def time — a default-arg
        # binding would capture a stale (possibly closed/redirected) handle.
        self._stream = stream if stream is not None else sys.stdout
        self._min_interval = min_interval
        self._active = bool(enabled) and self._stream.isatty() and supports_unicode()
        self._renderer: object | None = None
        self._quiesce: _LogQuiesce | None = None
        self._last_paint: float | None = None  # None → the first update always paints

    @property
    def active(self) -> bool:
        """True iff this region is driving the live renderer (enabled + Unicode TTY)."""
        return self._active

    def __enter__(self) -> LiveRegion:
        if self._active:
            from painted import InPlaceRenderer

            self._quiesce = _LogQuiesce().__enter__()  # hold logs off the frame
            renderer = InPlaceRenderer(self._stream)
            renderer.__enter__()
            self._renderer = renderer
        return self

    def __exit__(self, *exc: object) -> bool:
        # Restore the cursor even on an exception that skipped finalize() …
        if self._renderer is not None:
            self._renderer.__exit__(*exc)  # type: ignore[attr-defined]
            self._renderer = None
        # … then replay buffered logs, now below the final (deposited) frame.
        if self._quiesce is not None:
            self._quiesce.__exit__(*exc)
            self._quiesce = None
        return False

    def update(self, block: Block, *, force: bool = False) -> None:
        """Repaint ``block`` in place, throttled to ``min_interval`` unless ``force``.

        ``force`` is for terminal moments (an item finishing, an error) that must
        not be dropped by the throttle; the steady event stream rides the
        interval so a thousand skips a second cost a handful of frames.
        """
        if self._renderer is None:
            return
        now = perf_counter()
        if (
            not force
            and self._last_paint is not None
            and (now - self._last_paint) < self._min_interval
        ):
            return
        self._last_paint = now
        self._renderer.render(block)  # type: ignore[attr-defined]

    def finalize(self, block: Block | None = None) -> None:
        """Lock ``block`` into scrollback as static history and restore the cursor.

        Always renders ``block`` (it bypasses the throttle), so the final frame
        is never the one the throttle happened to drop. A no-op when inactive.
        """
        if self._renderer is not None:
            self._renderer.finalize(block)  # type: ignore[attr-defined]
            self._renderer = None


# --- row builders: thin compositions of painted primitives -----------------


def _blank() -> Block:
    from painted import Block

    return Block.empty(0, 1)


def bar_row(
    label: str,
    fraction: float,
    *,
    label_width: int,
    bar_width: int,
    segments: list[tuple[str, Style | None]] | None = None,
    glyph: str = "",
    glyph_style: Style | None = None,
    label_style: Style | None = None,
    filled_char: str | None = None,
    empty_char: str | None = None,
) -> Block:
    """One labelled progress-bar row: ``label  [====----]  <segments>  <glyph>``.

    ``fraction`` is clamped to 0..1; the bar draws from the ambient palette
    (accent fill / muted empty). ``filled_char`` / ``empty_char`` override the
    IconSet glyphs (default ``█``/``░``) — pass e.g. ``━``/``─`` for a lighter,
    thinner rule. ``segments`` is a list of ``(text, style)`` so trailing stats
    can be individually themed; ``glyph`` is the trailing status mark.
    """
    from painted import Line, Span, Style, join_horizontal
    from painted.views import ProgressState, progress_bar

    plain = Style()
    label_line = Line(spans=(Span(f"{label:<{label_width}}  ", label_style or plain),))
    parts = [label_line.to_block(label_line.width)]
    parts.append(progress_bar(
        ProgressState().set(fraction), bar_width,
        filled_char=filled_char, empty_char=empty_char,
    ))

    trailing: list[Span] = [Span("  ", plain)]
    for text, style in segments or []:
        trailing.append(Span(text, style or plain))
    if glyph:
        trailing.append(Span("  ", plain))
        trailing.append(Span(glyph, glyph_style or plain))
    trail_line = Line(spans=tuple(trailing))
    parts.append(trail_line.to_block(trail_line.width))

    return join_horizontal(*parts)


def text_row(segments: list[tuple[str, Style | None]], *, indent: str = "") -> Block:
    """A single row of individually-styled segments → a one-line Block.

    The block-returning convenience over ``output.row.row_line`` (the shared row
    atom): the step-log of ``doctor --fix`` and assorted footers. Empty-text
    segments drop and a ``None`` style is plain (row_line's contract); an empty
    row collapses to a blank line.
    """
    from siftd.output.row import row_line

    line = row_line(segments, indent=indent)
    return line.to_block(line.width) if line.width > 0 else _blank()


def spinner_glyph(frame: int = 0) -> str:
    """The ambient IconSet's spinner frame (``⠋`` family; ASCII ``-\\|/`` degraded).

    ``doctor --fix`` runs blocking steps, so the indicator is static rather than
    animated — frame 0 is a fixed "in progress" mark, sourced from the IconSet so
    it degrades with everything else.
    """
    from painted import current_icons

    frames = current_icons().spinner
    return frames[frame % len(frames)]
