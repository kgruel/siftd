"""Shared date parsing utilities for CLI filters and inline query fields."""

from __future__ import annotations

import re
from datetime import date, timedelta


def parse_date(value: str | None) -> str | None:
    """Parse date string to ISO format (YYYY-MM-DD).

    Supports:
    - ISO format: 2024-01-01 (passthrough)
    - Relative days: 7d, 3d (subtract N days from today)
    - Relative weeks: 1w, 2w (subtract N weeks from today)
    - Keywords: yesterday, today

    Raises ValueError for unrecognized formats.
    """
    if not value:
        return None

    value = value.strip().lower()

    if value == "today":
        return date.today().isoformat()
    if value == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()

    if match := re.fullmatch(r"(\d+)d", value):
        days = int(match.group(1))
        return (date.today() - timedelta(days=days)).isoformat()

    if match := re.fullmatch(r"(\d+)w", value):
        weeks = int(match.group(1))
        return (date.today() - timedelta(weeks=weeks)).isoformat()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        # Validate the calendar date — a well-shaped but impossible date like
        # 2024-13-45 would otherwise pass through and, as a lexical string
        # comparison against ISO timestamps, silently match nothing/everything.
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as e:
            raise ValueError(f"invalid date: '{value}' is not a real calendar date") from e

    raise ValueError(
        f"invalid date format: '{value}' (expected YYYY-MM-DD, Nd, Nw, today, or yesterday)"
    )
