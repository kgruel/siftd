"""Tests for the live-render policy (output/live.py).

Covers the siftd POLICY layer — the degrade gate, the throttle, and cursor
safety — over painted's ``InPlaceRenderer``, plus the thin row builders. The
renderer mechanism itself is painted's; here we pin that siftd drives it only on
a Unicode TTY, never drops a forced frame, and always restores the cursor.
"""

from __future__ import annotations

import io

import pytest

from siftd.output.live import LiveRegion, bar_row, spinner_glyph, text_row


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _FakeRenderer:
    """Stand-in for painted's InPlaceRenderer recording the calls it gets."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.renders: list = []
        self.finalized: object = "<unset>"
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def render(self, block) -> None:
        self.renders.append(block)

    def finalize(self, block=None) -> None:
        self.finalized = block


@pytest.fixture
def spy(monkeypatch):
    """Force the region active and swap in a recording renderer."""
    made: list[_FakeRenderer] = []
    monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
    monkeypatch.setattr(
        "painted.InPlaceRenderer", lambda stream: made.append(_FakeRenderer(stream)) or made[-1]
    )
    return made


# --- the degrade gate ------------------------------------------------------


def test_inactive_when_not_a_tty():
    region = LiveRegion(stream=io.StringIO())  # StringIO.isatty() is False
    assert region.active is False


def test_inactive_when_disabled(monkeypatch):
    monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
    region = LiveRegion(stream=_FakeTTY(), enabled=False)
    assert region.active is False


def test_inactive_without_unicode(monkeypatch):
    monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: False)
    region = LiveRegion(stream=_FakeTTY())
    assert region.active is False


def test_inactive_region_is_all_noops():
    stream = io.StringIO()
    region = LiveRegion(stream=stream)
    with region:
        region.update(text_row([("hi", None)]), force=True)
        region.finalize(text_row([("done", None)]))
    assert stream.getvalue() == ""  # nothing painted, no cursor codes


def test_active_on_unicode_tty(spy):
    region = LiveRegion(stream=_FakeTTY())
    assert region.active is True


# --- the throttle ----------------------------------------------------------


def test_throttle_collapses_rapid_updates(spy):
    region = LiveRegion(stream=_FakeTTY(), min_interval=3600.0)  # never elapses in a test
    block = text_row([("x", None)])
    with region:
        region.update(block)  # first update always paints
        region.update(block)  # within the interval → dropped
        region.update(block)  # still within → dropped
    assert len(spy[0].renders) == 1


def test_force_bypasses_the_throttle(spy):
    region = LiveRegion(stream=_FakeTTY(), min_interval=3600.0)
    block = text_row([("x", None)])
    with region:
        region.update(block)  # paints (first)
        region.update(block, force=True)  # paints (forced)
        region.update(block, force=True)  # paints (forced)
    assert len(spy[0].renders) == 3


def test_finalize_always_deposits(spy):
    region = LiveRegion(stream=_FakeTTY(), min_interval=3600.0)
    final = text_row([("final", None)])
    with region:
        region.update(text_row([("x", None)]))
        region.finalize(final)  # bypasses the throttle
    assert spy[0].finalized is final


# --- cursor safety ---------------------------------------------------------


def test_cursor_restored_on_exception(spy):
    region = LiveRegion(stream=_FakeTTY())
    with pytest.raises(RuntimeError):
        with region:
            region.update(text_row([("x", None)]), force=True)
            raise RuntimeError("boom")
    assert spy[0].exited is True  # __exit__ ran → cursor shown


# --- log quiescing (terminal-tear prevention) ------------------------------


def test_log_quiesce_buffers_during_region_and_replays_after(spy):
    import logging

    log = logging.getLogger("siftd")
    saved = (log.handlers[:], log.level, log.propagate)
    seen: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    try:
        log.handlers = [_Cap()]
        log.setLevel(logging.WARNING)
        log.propagate = False
        with LiveRegion(stream=_FakeTTY()):
            log.warning("mid-frame")
            assert seen == []  # held off the live frame, not emitted yet
        assert "mid-frame" in seen  # replayed once the region closed
        assert isinstance(log.handlers[0], _Cap)  # original handler restored
    finally:
        log.handlers, log.level, log.propagate = saved


# --- the row builders ------------------------------------------------------


def test_bar_row_is_one_line_with_fill_and_glyph():
    block = bar_row(
        "claude", 0.5, label_width=8, bar_width=10,
        segments=[("12/24  ", None), ("new ", None), ("3", None)], glyph="OK",
    )
    assert block.height == 1
    text = _render_plain(block)
    assert "claude" in text and "12/24" in text and "OK" in text


def test_bar_row_clamps_fraction_over_one():
    # value > 1 must not produce a negative empty span / crash
    block = bar_row("x", 5.0, label_width=2, bar_width=6)
    assert block.height == 1


def test_spinner_glyph_uses_ambient_iconset():
    from painted import ASCII_ICONS, use_icons

    assert spinner_glyph() in ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    with use_icons(ASCII_ICONS):
        assert spinner_glyph() in ("-", "\\", "|", "/")


def test_bar_row_degrades_to_ascii_under_ascii_icons():
    from painted import ASCII_ICONS, use_icons

    with use_icons(ASCII_ICONS):
        block = bar_row("x", 1.0, label_width=2, bar_width=6)
    text = _render_plain(block)
    assert "█" not in text and "░" not in text  # no Unicode bar glyphs


# --- helpers ---------------------------------------------------------------


def _render_plain(block) -> str:
    """The block's text content — a non-TTY sink makes print_block emit plain."""
    from painted import print_block

    sink = io.StringIO()  # not a TTY → print_block auto-strips ANSI
    print_block(block, sink)
    return sink.getvalue()
