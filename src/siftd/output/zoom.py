"""Internal semantic zoom levels for human-readable output."""

from enum import IntEnum


class NarrativeZoom(IntEnum):
    """Semantic detail level requested by the user."""

    MINIMAL = 0
    SUMMARY = 1
    DETAILED = 2
    FULL = 3


def detail_zoom(*, full: bool = False, thinking: bool = False, tools: bool = False) -> NarrativeZoom:
    """Map current CLI detail flags onto siftd's internal zoom model."""
    if full:
        return NarrativeZoom.FULL
    if thinking or tools:
        return NarrativeZoom.DETAILED
    return NarrativeZoom.SUMMARY


def query_detail_zoom(*, full: bool = False, thinking: bool = False, tools: str | None = None) -> NarrativeZoom:
    """Backward-compatible query detail zoom helper."""
    return detail_zoom(full=full, thinking=thinking, tools=tools is not None)


def peek_detail_zoom(*, full: bool = False, thinking: bool = False, tools: bool = False) -> NarrativeZoom:
    """Peek detail zoom helper aligned with query detail semantics."""
    return detail_zoom(full=full, thinking=thinking, tools=tools)
