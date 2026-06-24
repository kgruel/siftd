"""The progress-event contract — one typed stream every action command emits.

The general form abstracted straight from ``IngestEvent``
(``ingestion/orchestration.py``): the same notion — "I am making progress
through work" — is expressed three incompatible ways across the API today
(ingest's rich event stream, migrate's ``Callable[[str], None]`` line callback,
push/pull's silence). ``ProgressEvent`` is the dissolution: the typed stream
that every long-running, terminal-owning command emits, and that one
``LiveRegion``-driven consumer in ``output/`` reads.

``IngestEvent`` carries the two hard-won fields this generalizes — an optional
total, and per-unit domain counts — so they lead here:

  - ``total: int | None`` models work whose size is unknown or *growing*. Push's
    bisection (a window splits into sub-windows on HTTP 413) makes a fixed
    denominator a lie; ``None`` signals the renderer's indeterminate (sweep)
    branch instead of a false percentage.
  - ``tally`` carries the command-meaningful running counts a bar surfaces —
    the dc.html mock's ``kept 37`` is a tally cell. It is a free
    ``Mapping[str, int]``: the renderer only formats cells, nobody branches on
    keys.

Lives in ``domain`` (the lowest layer) so both the ``api`` producers that emit
and the ``output`` renderer that consumes can import it without a cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# NOTE(progress-fold): ``ingest``'s live bars now render through the generic
# ``ProgressConsumer`` — ``cli/data.py:_IngestTextRenderer._progress_event`` maps
# each ``IngestEvent`` onto a ``ProgressEvent`` at the boundary, so there is one
# bar renderer rather than two. What stays distinct is the *event type*: the API
# still emits ``IngestEvent``, whose richer fields (workspace_path / model /
# summary) feed the plain streaming path and the final per-adapter content table
# — neither of which is progress. Folding those into a ``ProgressEvent`` subclass
# — one progress type end to end — is the remaining convergence; the renderer is
# already shared.


@dataclass(frozen=True)
class ProgressEvent:
    """One unit of progress through looping work, for the live renderer.

    Producers expose ``on_progress: ProgressSink | None`` mirroring
    ``run_ingest(on_event=…)`` and emit one of these per work unit (and one
    ``terminal=True`` event at each group's start / error / completion).
    """

    # Identity --------------------------------------------------------------
    group: str
    """The bar/step a unit belongs to.

    ingest → adapter name; push → ``"windows"``; migrate → step label
    (``"merge workspaces"``). The renderer keys block state by this.
    """

    # Magnitude -------------------------------------------------------------
    index: int | None = None
    """Work units done within the group (the bar's numerator)."""

    total: int | None = None
    """Group size, or ``None`` when unknown/growing.

    ``None`` ⇒ the renderer draws the indeterminate **sweep**, not a
    percentage — push's bisection grows this mid-flight.
    """

    # Domain tally (the dc.html "kept" lesson) ------------------------------
    tally: Mapping[str, int] = field(default_factory=dict)
    """Command-meaningful running counts the bar can surface.

    ingest ``{new, updated, skipped}``; push ``{conversations, bytes}``; the
    mock's ``kept 37`` is one of these cells. A free mapping — the renderer
    formats every cell, nobody branches on keys.
    """

    # Lifecycle -------------------------------------------------------------
    status: str = "progress"
    """``progress`` | ``done`` | ``error`` | ``skipped``."""

    terminal: bool = False
    """Force a paint past the throttle — a group's start, an error, completion.

    The moments not to drop to the throttle (see ``LiveRegion.update``).
    """

    message: str | None = None
    """Free text for the plain / verbose sink (the non-TTY path)."""


# The sink shape every producer accepts. A type alias so producers and the
# consumer share one name (mirrors how ``run_ingest`` types ``on_event``).
ProgressSink = Callable[[ProgressEvent], None]
