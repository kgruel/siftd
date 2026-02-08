"""Shared CLI utilities."""

from __future__ import annotations

import argparse
import re
from datetime import date, timedelta
from pathlib import Path


def parse_date(value: str | None) -> str | None:
    """Parse date string to ISO format (YYYY-MM-DD).

    Supports:
    - ISO format: 2024-01-01 (passthrough)
    - Relative days: 7d, 3d (subtract N days from today)
    - Relative weeks: 1w, 2w (subtract N weeks from today)
    - Keywords: yesterday, today

    Raises argparse.ArgumentTypeError for unrecognized formats,
    so this can be used as type= on argparse arguments.
    """
    if not value:
        return None

    value = value.strip().lower()

    # Keywords
    if value == "today":
        return date.today().isoformat()
    if value == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()

    # Relative days: 7d, 3d
    if match := re.fullmatch(r"(\d+)d", value):
        days = int(match.group(1))
        return (date.today() - timedelta(days=days)).isoformat()

    # Relative weeks: 1w, 2w
    if match := re.fullmatch(r"(\d+)w", value):
        weeks = int(match.group(1))
        return (date.today() - timedelta(weeks=weeks)).isoformat()

    # ISO format passthrough (validate format)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value

    raise argparse.ArgumentTypeError(
        f"invalid date format: '{value}' (expected YYYY-MM-DD, Nd, Nw, today, or yesterday)"
    )


def resolve_db(args) -> Path:
    """Resolve database path from args."""
    from siftd.paths import db_path

    return Path(args.db) if args.db else db_path()


def _get_version() -> str:
    """Get package version from metadata."""
    try:
        from importlib.metadata import version

        return version("siftd")
    except Exception:
        return "unknown"
