"""Shared date parsing utilities for CLI filters and inline query fields."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, tzinfo

# The vocabulary `parse_date` accepts, phrased for `--help`. Every `--since`
# and `--before` option interpolates this, so the flags and the parser cannot
# drift into disagreeing about what is legal.
DATE_VOCABULARY = "YYYY-MM-DD, ISO timestamp, 7d, 1w, yesterday, today"


def parse_date(value: str | None) -> str | None:
    """Parse date string to a lower bound comparable with stored timestamps.

    Supports:
    - ISO date: 2024-01-01 (passthrough — matches the whole day)
    - ISO timestamp: 2024-01-01T09:30:00Z, 2024-01-01T09:30:00.123456+00:00
      (normalized to UTC; matches from that instant)
    - Relative days: 7d, 3d (subtract N days from today)
    - Relative weeks: 1w, 2w (subtract N weeks from today)
    - Keywords: yesterday, today

    Raises ValueError for unrecognized formats.
    """
    if not value:
        return None

    # Keywords and relative forms are case-insensitive. The ISO branches read
    # `value` rather than `keyword` only so their errors quote what the user
    # actually typed — both re-render their result through `isoformat()`.
    value = value.strip()
    keyword = value.lower()

    if keyword == "today":
        return date.today().isoformat()
    if keyword == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()

    if match := re.fullmatch(r"(\d+)d", keyword):
        days = int(match.group(1))
        return (date.today() - timedelta(days=days)).isoformat()

    if match := re.fullmatch(r"(\d+)w", keyword):
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

    # Loose on purpose: `datetime.fromisoformat` owns the real validation, and
    # this only picks the branch.
    if re.match(r"\d{4}-\d{2}-\d{2}.\d{2}:\d{2}", value):
        return _normalize_timestamp(value)

    raise ValueError(
        f"invalid date format: '{value}' "
        f"(expected YYYY-MM-DD, an ISO 8601 timestamp, Nd, Nw, today, or yesterday)"
    )


def local_to_utc(value: str, *, tz: tzinfo | None = None) -> str:
    """Render a wall-clock timestamp as an aware UTC ISO 8601 string.

    For adapters whose logs record local time with no offset. `started_at` is
    a UTC column compared as a SQL *string* against UTC-anchored bounds, so a
    naive local value sorts by the size of the host's offset rather than by
    the instant it names — far enough below a `--since` cursor that delta sync
    skips it, silently and without self-healing.

    `tz` is the zone the value was written in; None means the host's current
    local zone, which is the only zone an adapter can infer. A value that
    already carries an offset is converted, never reinterpreted.

    Ambiguous local times — the repeated hour at a DST fall-back — resolve to
    the earlier, pre-transition instant (`fold=0`, Python's default). The log
    carries no information that could disambiguate them.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz) if tz is not None else parsed.astimezone()
    return parsed.astimezone(UTC).isoformat()


def _normalize_timestamp(value: str) -> str:
    """Normalize an ISO 8601 timestamp to a naive-UTC, second-precision bound.

    Sync persists its pull/push cursors as `datetime.now(UTC).isoformat()` and
    hands them back to `--since`, so this is the form the round trip depends on.

    Filters compare the result against `conversations.started_at` as SQL
    strings, and adapters spell that column inconsistently: `...00.123Z`,
    `...00.123456`, `...00.123456+00:00`, `...00Z`, and — whenever
    `epoch_ms_to_iso` lands on a whole second — `...00+00:00`. Truncating to
    seconds makes the bound a strict *prefix* of every one of those spellings,
    so a row in the bound's own second always sorts above it whatever suffix
    it carries. That is the same mechanism the bare-date form relies on, one
    level finer, and unlike a microsecond rendering it never depends on how
    `.`, `+`, `Z`, and the digits happen to order in ASCII — where `+` sorts
    *below* `.`, which silently dropped rows stored as `...00+00:00`.

    The residue is over-inclusion within the bound's own second: `--since`
    re-pulls at most a second of rows through an idempotent merge, and
    `--before` gives up sub-second precision it has never been asked for.
    """
    # `fromisoformat` takes any separator character but only an uppercase UTC
    # designator, so a lowercase `z` is the one spelling it needs help with.
    candidate = f"{value[:-1]}Z" if value.endswith("z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as e:
        raise ValueError(f"invalid timestamp: '{value}' is not a valid ISO 8601 timestamp") from e

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")
