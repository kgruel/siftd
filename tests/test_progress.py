"""Tests for the ProgressEvent contract (domain/progress.py).

The contract is data only — these pin the field defaults and immutability that
producers and the renderer both rely on.
"""

from __future__ import annotations

import dataclasses

import pytest

from siftd.domain.progress import ProgressEvent


def test_minimal_event_defaults():
    ev = ProgressEvent(group="windows")
    assert ev.group == "windows"
    assert ev.index is None
    assert ev.total is None
    assert ev.tally == {}
    assert ev.status == "progress"
    assert ev.terminal is False
    assert ev.message is None


def test_event_is_frozen():
    ev = ProgressEvent(group="g")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.index = 3  # type: ignore[misc]


def test_tally_is_a_free_mapping():
    # Any string→int mapping; the renderer formats every cell, nobody branches.
    ev = ProgressEvent(group="push", tally={"conversations": 12, "bytes": 4096})
    assert ev.tally["conversations"] == 12
    assert ev.tally["bytes"] == 4096


def test_tally_default_is_not_shared():
    # field(default_factory=dict) — each event gets its own dict, no aliasing.
    a = ProgressEvent(group="a")
    b = ProgressEvent(group="b")
    assert a.tally is not b.tally


def test_total_none_models_growing_work():
    # None total is the contract's whole reason over a bare (done, total) float:
    # push's bisection grows the denominator mid-flight.
    ev = ProgressEvent(group="windows", index=3, total=None)
    assert ev.total is None and ev.index == 3
