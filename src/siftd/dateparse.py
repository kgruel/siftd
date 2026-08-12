"""Shared date and timestamp handling.

Two directions, both grounded in the fact that `conversations.started_at` is
compared as a SQL *string*: `parse_date` renders a read-side bound (CLI
filters, inline query fields, sync cursors), and `to_utc` / `local_to_utc`
resolve a stored value to the instant it names.

Every timestamp helper lives here rather than beside its one caller. The habit
of writing the converter where it was needed produced five of them — in
`ingestion`, `output` (twice), `search`, and `peek` — plus two more in `api`
and `cli` that #32's own survey missed, each spelling the `Z` handling its own
way (`.replace("Z", "+00:00")`, `.rstrip("Z")`, a `"+" not in s` heuristic),
and they had already drifted into disagreeing about whether a lowercase `z` is
a timestamp at all.

**What a naive value means is the only thing that ever differed between them,
and it is the thing none of them said out loud.** It has exactly two answers
in siftd, and which one is right is a property of where the value came from,
never of the value itself:

- **UTC**, on the read side — `to_utc`. Every column siftd compares or renders
  (`started_at`, `ended_at`, `timestamp`, `ingested_at`, `last_activity`)
  stores UTC, so a naive value from one is a UTC instant that lost its
  designator, not a wall clock.
- **Host-local**, on the write side — `local_to_utc`. An adapter log with no
  offset was written by a program reading the local clock, and the ingesting
  host's zone is the only zone that can be inferred (#31).

Reading one as the other shifts the instant by the size of the host's offset,
which is why they stay two names over one core rather than merging: at a call
site `to_utc(row.started_at)` and `local_to_utc(header_time)` state their
assumption, where a shared name with a defaulted flag would bury it.
"""

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


def to_utc(value: str) -> datetime:
    """Resolve a stored timestamp to the aware UTC instant it names.

    The read-side converter: for values coming back out of siftd's own
    columns, all of which store UTC. A naive value from one is therefore a UTC
    instant whose designator was never written, not a wall clock — contrast
    `local_to_utc`, which is the same operation for the write side, where
    naive means the host's local zone.

    Returns an aware datetime rather than a string because every caller wants
    arithmetic against `datetime.now(UTC)` or a `.timestamp()` epoch; the two
    callers that want a string render it themselves.

    Raises ValueError for anything `_parse_iso` cannot read. Callers that were
    already tolerant of unparseable stored values keep catching it — a column
    can hold third-party log content that no adapter validated.
    """
    return _resolve(value, naive_tz=UTC)


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
    return _resolve(value, naive_tz=tz).isoformat()


def _resolve(value: str, *, naive_tz: tzinfo | None) -> datetime:
    """Parse to UTC, resolving a naive value against `naive_tz`.

    `naive_tz=None` defers to the host's local zone, which is what a naive
    datetime already means to `astimezone` — so the two public spellings
    differ only in the argument they pass here.
    """
    parsed = _parse_iso(value)
    if parsed.tzinfo is None and naive_tz is not None:
        parsed = parsed.replace(tzinfo=naive_tz)
    # A still-naive value resolves against the host zone inside `astimezone`.
    return parsed.astimezone(UTC)


def _parse_iso(value: str) -> datetime:
    """`datetime.fromisoformat` with the two affordances every caller wants.

    `fromisoformat` takes any separator character but only an *uppercase* UTC
    designator, so a lowercase `z` is the one spelling it needs help with. And
    its own error names neither the value nor the format, so every entry point
    in this module would otherwise have to re-wrap it — which is how the two
    would drift into disagreeing about what a bad timestamp looks like.
    """
    candidate = f"{value[:-1]}Z" if value.endswith("z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError as e:
        raise ValueError(f"invalid timestamp: '{value}' is not a valid ISO 8601 timestamp") from e


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
    return to_utc(value).replace(tzinfo=None).isoformat(timespec="seconds")
