"""Regression: a delta/resume push must window with a since-floor.

Companion to test_push_windowing.py. Windowing used to be gated to full
(`--all`) pushes; a delta/resume push sent one un-windowed slice. These cover
the `since_floor` parameter that bounds the first window of a delta at the
cursor (rather than None, which would reach back before the cursor).
"""

from __future__ import annotations

from siftd.api.sync import _derive_date_windows


def _make_convs(*timestamps: str):
    class _C:
        pass

    out = []
    for ts in timestamps:
        c = _C()
        c.started_at = ts
        out.append(c)
    return out


def test_since_floor_bounds_first_window():
    # Delta push: first window starts at the cursor (floor), not None.
    convs = _make_convs("2026-02-01", "2026-02-02", "2026-02-03", "2026-02-04")
    floor = "2026-01-15T00:00:00.000Z"
    windows = _derive_date_windows(
        convs, target_bytes=2, bytes_per_conv=1, since_floor=floor)
    assert windows[0] == (floor, "2026-02-03")
    assert windows[1] == ("2026-02-03", None)


def test_since_floor_single_window():
    # Fits in one window: the floor still bounds the lower edge.
    convs = _make_convs("2026-02-01", "2026-02-02")
    floor = "2026-01-15T00:00:00.000Z"
    windows = _derive_date_windows(
        convs, target_bytes=1000, bytes_per_conv=1, since_floor=floor)
    assert windows == [(floor, None)]


def test_full_push_unchanged_without_floor():
    # --all path (since_floor defaults to None): identical to legacy behaviour.
    convs = _make_convs("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04")
    windows = _derive_date_windows(convs, target_bytes=2, bytes_per_conv=1)
    assert windows[0] == (None, "2026-01-03")
    assert windows[1] == ("2026-01-03", None)
