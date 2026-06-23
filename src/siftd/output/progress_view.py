"""The generic ``ProgressEvent`` consumer — one renderer for every action bar.

The companion to ``domain.progress``: where that defines the contract every
long-running command emits, this reads the stream and paints it. It is the
single ``LiveRegion``-driven consumer the dissolution promised — new commands
stop inventing a progress shape and a bespoke plain branch; they emit
``ProgressEvent``\\s and hand ``feed`` to their producer.

A consumer is a switch over the two proven block **shapes** (no new painted
primitives — it composes ``output.live``'s row builders):

  - ``"bars"`` — stacked, one ``bar_row`` per ``group`` (ingest's shape). A
    group with a known ``total`` draws a determinate fraction; ``total is None``
    draws the indeterminate **sweep** (``sweep_row``), its frame advancing with
    the event stream.
  - ``"steps"`` — a ``text_row`` step-log (doctor-fix's shape): each resolved
    group is a ``✓``/``✗`` line; the in-flight group shows a spinner.

Usage mirrors ``LiveRegion`` (the CLI branches once on ``active``)::

    consumer = ProgressConsumer(shape="steps")
    with consumer:
        if consumer.active:
            run_work(on_progress=consumer.feed)   # live bars
        else:
            run_work(on_progress=plain_print)      # caller's plain path

``tally`` cells render as the amber ``metric`` thread (the dc.html ``kept 37``);
the degrade gate, throttle, ASCII degradation and NO_COLOR stripping all come
from ``LiveRegion`` / the ambient theme, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from siftd.domain.progress import ProgressEvent, ProgressSink
from siftd.output.live import LiveRegion, bar_row, spinner_glyph, sweep_row, text_row

if TYPE_CHECKING:
    from painted import Block, Style

# Re-exported so a CLI consumer (which may import ``output`` but not ``domain``
# per the layering) gets the consumer and the event it feeds from one module.
__all__ = ["ProgressConsumer", "ProgressEvent", "ProgressSink"]


@dataclass
class _GroupState:
    """Folded state for one ``group`` (a bar / a step)."""

    label: str
    index: int | None = None
    total: int | None = None
    tally: dict[str, int] = field(default_factory=dict)
    status: str = "progress"
    message: str | None = None
    frame: int = 0  # advances per event while indeterminate (drives the sweep)


class ProgressConsumer:
    """Folds a ``ProgressEvent`` stream into a re-painting live block.

    Holds per-``group`` state in first-seen order and repaints through a
    ``LiveRegion`` on each ``feed``. Terminal events (a group's start / error /
    completion) force a paint past the throttle.
    """

    def __init__(
        self,
        *,
        shape: str = "bars",
        live: LiveRegion | None = None,
        label_width: int | None = None,
        bar_width: int | None = None,
    ) -> None:
        if shape not in ("bars", "steps"):
            raise ValueError(f"unknown progress shape: {shape!r}")
        self.shape = shape
        self._live = live if live is not None else LiveRegion()
        self._label_width = label_width
        self._bar_width = bar_width
        self._groups: dict[str, _GroupState] = {}

    @property
    def active(self) -> bool:
        """True iff the live region is driving the renderer (enabled + Unicode TTY)."""
        return self._live.active

    def __enter__(self) -> ProgressConsumer:
        self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> bool:
        # Deposit the final frame before the region restores the cursor, so the
        # resolved bars/steps land in scrollback (skipped on an exception path,
        # where there is no clean final frame to deposit).
        if self._live.active and exc == (None, None, None):
            self._live.finalize(self._block())
        self._live.__exit__(*exc)
        return False

    def feed(self, event: ProgressEvent) -> None:
        """Fold one event into group state and repaint (throttled unless terminal)."""
        g = self._groups.get(event.group)
        if g is None:
            g = _GroupState(label=event.group)
            self._groups[event.group] = g
        if event.index is not None:
            g.index = event.index
        # ``total`` is the group's *current* size, reflected exactly each event —
        # including None, which flips a previously-determinate bar to the sweep
        # (push's bisection grows the work mid-flight). A producer that knows the
        # total re-sends it every event (push does); one that never knows it
        # (migrate steps) simply never carries it.
        g.total = event.total
        if event.tally:
            g.tally = dict(event.tally)
        g.status = event.status
        if event.message is not None:
            g.message = event.message
        # Indeterminate groups animate the sweep off the event stream itself;
        # one tick per event keeps the window moving without a timer thread.
        if g.total is None:
            g.frame += 1
        if self._live.active:
            self._live.update(self._block(), force=event.terminal)

    # --- block assembly ----------------------------------------------------

    def _block(self) -> Block:
        from painted import Block

        items = list(self._groups.values())
        if not items:
            return Block.empty(0, 0)
        if self.shape == "steps":
            return self._steps_block(items)
        return self._bars_block(items)

    def _steps_block(self, items: list[_GroupState]) -> Block:
        from painted import current_icons, current_palette, join_vertical

        pal = current_palette()
        ic = current_icons()
        rows = []
        for g in items:
            text = g.message or g.label
            if g.status == "progress":
                rows.append(text_row([(f"  {spinner_glyph()} ", pal.accent), (f"{text}...", pal.muted)]))
            elif g.status == "error":
                rows.append(text_row([(f"  {ic.error} ", pal.error), (text, None)]))
            elif g.status == "skipped":
                rows.append(text_row([(f"  {ic.rank_tail} ", pal.muted), (text, pal.muted)]))
            else:  # done
                rows.append(text_row([(f"  {ic.ok} ", pal.success), (text, None)]))
        return join_vertical(*rows)

    def _bars_block(self, items: list[_GroupState]) -> Block:
        from painted import current_icons, current_palette, join_vertical

        from siftd.output.common import term_width
        from siftd.output.theme import domain_styles

        pal = current_palette()
        ic = current_icons()
        ds = domain_styles()

        label_width = self._label_width or max(12, max(len(g.label) for g in items))
        bar_width = self._bar_width or max(10, min(28, term_width() - label_width - 44))

        rows = []
        for g in items:
            # Trailing tally cells: amber metric thread with a gold ▪ marker
            # (the dc.html "kept ▪ 37"). A free mapping — every cell is formatted.
            segments: list[tuple[str, Style | None]] = []
            for i, (key, value) in enumerate(g.tally.items()):
                prefix = "  " if i else ""
                segments.append((f"{prefix}{key} ", None))
                segments.append((str(value), ds.metric))

            if g.status == "error":
                glyph, gstyle = ic.error, pal.error
            elif g.status in ("done", "skipped"):
                glyph, gstyle = ic.ok, pal.success
            else:
                glyph, gstyle = spinner_glyph(), pal.accent

            if g.total is None:
                rows.append(sweep_row(
                    g.label, g.frame, label_width=label_width, bar_width=bar_width,
                    segments=segments, glyph=glyph, glyph_style=gstyle,
                    label_style=pal.muted, fill_style=ds.metric,
                    filled_char="━", empty_char="─",  # match the determinate thin rule
                ))
            else:
                done = g.index or 0
                frac = (done / g.total) if g.total else 0.0
                count = (f"{done}/{g.total}", pal.muted)
                rows.append(bar_row(
                    g.label, frac, label_width=label_width, bar_width=bar_width,
                    segments=[count, ("  ", None), *segments],
                    glyph=glyph, glyph_style=gstyle, label_style=pal.muted,
                    filled_char="━", empty_char="─",  # a thin rule, not a full block
                ))
        return join_vertical(*rows)
