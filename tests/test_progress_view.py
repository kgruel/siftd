"""Tests for the generic ProgressEvent consumer (output/progress_view.py).

Covers both block shapes, the indeterminate sweep, tally cells, and the
degrade gate — the consumer is the single ``LiveRegion``-driven reader every
action command will share. The renderer mechanism itself is painted's; here we
pin that the consumer folds the event stream into the right block and respects
the gate.
"""

from __future__ import annotations

import io

import pytest

from siftd.domain.progress import ProgressEvent
from siftd.output.live import LiveRegion
from siftd.output.progress_view import ProgressConsumer


@pytest.fixture(autouse=True)
def _themed():
    """The consumer reaches into domain_styles(); run under the siftd theme."""
    from painted import use_theme

    from siftd.output.theme import siftd_theme

    with use_theme(siftd_theme):
        yield


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeRenderer:
    """Stand-in for painted's InPlaceRenderer recording the calls it gets."""

    def __init__(self, stream) -> None:
        self.renders: list = []
        self.finalized: object = "<unset>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def render(self, block) -> None:
        self.renders.append(block)

    def finalize(self, block=None) -> None:
        self.finalized = block


@pytest.fixture
def spy(monkeypatch):
    """Force live regions active and swap in a recording renderer."""
    made: list[_FakeRenderer] = []
    monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
    monkeypatch.setattr(
        "painted.InPlaceRenderer",
        lambda stream: made.append(_FakeRenderer(stream)) or made[-1],
    )
    return made


def _render(block) -> str:
    from painted import print_block

    sink = io.StringIO()  # not a TTY → print_block auto-strips ANSI
    print_block(block, sink)
    return sink.getvalue()


# --- shape: bars -----------------------------------------------------------


def test_bars_determinate_renders_fraction_and_tally():
    c = ProgressConsumer(shape="bars")
    c.feed(ProgressEvent(group="claude", index=12, total=24, tally={"new": 3, "skip": 2}))
    text = _render(c._block())
    assert "claude" in text
    assert "12/24" in text
    assert "new 3" in text and "skip 2" in text


def test_bars_done_status_shows_ok_glyph():
    c = ProgressConsumer(shape="bars")
    c.feed(ProgressEvent(group="aider", index=8, total=8, status="done"))
    assert "✓" in _render(c._block())


def test_bars_error_status_shows_error_glyph():
    c = ProgressConsumer(shape="bars")
    c.feed(ProgressEvent(group="x", index=1, total=4, status="error"))
    assert "✗" in _render(c._block())


def test_bars_groups_render_in_first_seen_order():
    c = ProgressConsumer(shape="bars")
    c.feed(ProgressEvent(group="zzz", index=1, total=2))
    c.feed(ProgressEvent(group="aaa", index=1, total=2))
    text = _render(c._block())
    assert text.index("zzz") < text.index("aaa")


# --- shape: bars, indeterminate (the sweep) --------------------------------


def test_sweep_when_total_is_none_shows_no_percentage():
    c = ProgressConsumer(shape="bars", bar_width=20, label_width=8)
    c.feed(ProgressEvent(group="windows", total=None, tally={"conversations": 30}))
    text = _render(c._block())
    assert "windows" in text
    assert "conversations 30" in text
    assert "%" not in text  # indeterminate ⇒ no fraction/percentage readout
    assert "/" not in text  # nor a "done/total" count


def test_none_total_flips_a_determinate_bar_to_the_sweep():
    # The contract subtlety: total is the group's *current* size each event, so a
    # later total=None must override a known total (push's bisection grows the
    # work mid-flight) — not be ignored as "no update".
    c = ProgressConsumer(shape="bars", bar_width=20, label_width=8)
    c.feed(ProgressEvent(group="windows", index=1, total=3))
    assert "1/3" in _render(c._block())  # determinate
    c.feed(ProgressEvent(group="windows", index=1, total=None))
    text = _render(c._block())
    assert "/3" not in text and "%" not in text  # flipped to the indeterminate sweep


def test_sweep_window_advances_with_the_event_stream():
    # Each event ticks the frame; the lit window's position differs between
    # frames (no timer thread — the stream drives the animation).
    c = ProgressConsumer(shape="bars", bar_width=20, label_width=8)
    c.feed(ProgressEvent(group="windows", total=None))
    first = _render(c._block())
    for _ in range(6):
        c.feed(ProgressEvent(group="windows", total=None))
    later = _render(c._block())
    assert first != later


# --- shape: steps ----------------------------------------------------------


def test_steps_pending_shows_spinner_resolved_shows_glyph():
    c = ProgressConsumer(shape="steps")
    c.feed(ProgressEvent(group="merge", status="progress", message="merge"))
    pending = _render(c._block())
    assert "merge..." in pending  # in-flight spinner line
    c.feed(ProgressEvent(group="merge", status="done", message="merge: 2 merged"))
    done = _render(c._block())
    assert "✓" in done and "merge: 2 merged" in done


def test_steps_error_and_skipped_glyphs():
    c = ProgressConsumer(shape="steps")
    c.feed(ProgressEvent(group="a", status="error", message="a: boom"))
    c.feed(ProgressEvent(group="b", status="skipped", message="b: nothing to do"))
    text = _render(c._block())
    assert "✗" in text and "a: boom" in text
    assert "b: nothing to do" in text


def test_steps_falls_back_to_group_label_without_message():
    c = ProgressConsumer(shape="steps")
    c.feed(ProgressEvent(group="step one", status="progress"))
    assert "step one..." in _render(c._block())


# --- the degrade gate & lifecycle ------------------------------------------


def test_inactive_when_not_a_tty():
    c = ProgressConsumer(live=LiveRegion(stream=io.StringIO()))
    assert c.active is False


def test_inactive_consumer_feed_is_a_noop():
    stream = io.StringIO()
    c = ProgressConsumer(live=LiveRegion(stream=stream))
    with c:
        c.feed(ProgressEvent(group="g", index=1, total=2, terminal=True))
    assert stream.getvalue() == ""  # nothing painted


def test_active_consumer_paints_on_feed(spy):
    c = ProgressConsumer(shape="steps", live=LiveRegion(stream=_FakeTTY(), min_interval=0.0))
    with c:
        c.feed(ProgressEvent(group="g", status="progress", message="g", terminal=True))
    assert len(spy[0].renders) >= 1


def test_terminal_event_forces_a_paint_past_throttle(spy):
    # A long throttle would drop steady events; terminal=True must still paint.
    c = ProgressConsumer(shape="bars", live=LiveRegion(stream=_FakeTTY(), min_interval=3600.0))
    with c:
        c.feed(ProgressEvent(group="g", index=1, total=4))            # first paints
        c.feed(ProgressEvent(group="g", index=2, total=4))            # throttled
        c.feed(ProgressEvent(group="g", index=3, total=4, terminal=True))  # forced
    assert len(spy[0].renders) == 2  # first + forced (the middle one dropped)


def test_finalize_deposits_final_frame_on_clean_exit(spy):
    c = ProgressConsumer(shape="steps", live=LiveRegion(stream=_FakeTTY()))
    with c:
        c.feed(ProgressEvent(group="g", status="done", message="g: ok", terminal=True))
    assert spy[0].finalized is not None and spy[0].finalized != "<unset>"


def test_no_finalize_on_exception(spy):
    c = ProgressConsumer(shape="steps", live=LiveRegion(stream=_FakeTTY()))
    with pytest.raises(RuntimeError):
        with c:
            c.feed(ProgressEvent(group="g", status="progress", message="g", terminal=True))
            raise RuntimeError("boom")
    # No clean final frame to deposit on an exception path.
    assert spy[0].finalized == "<unset>"


def test_unknown_shape_rejected():
    with pytest.raises(ValueError, match="unknown progress shape"):
        ProgressConsumer(shape="pie")
