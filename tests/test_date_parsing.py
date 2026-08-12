"""Tests for date and timestamp parsing in CLI filters."""

from datetime import UTC, date
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from conftest import pinned_tz

from siftd.dateparse import local_to_utc, parse_date


class TestParseDate:
    """Unit tests for parse_date function."""

    @pytest.fixture
    def fixed_today(self):
        """Patch date.today to return 2024-06-15 for all tests."""
        with patch("siftd.dateparse.date") as mock_date:
            mock_date.today.return_value = date(2024, 6, 15)
            yield

    # Non-relative tests (no mock needed)

    def test_none_returns_none(self):
        """None input returns None."""
        assert parse_date(None) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert parse_date("") is None

    def test_whitespace_raises_error(self):
        """Whitespace-only string raises ValueError."""
        with pytest.raises(ValueError):
            parse_date("   ")

    def test_iso_format_passthrough(self):
        """ISO format dates pass through unchanged."""
        assert parse_date("2024-01-15") == "2024-01-15"
        assert parse_date("2023-12-31") == "2023-12-31"

    def test_iso_format_whitespace_stripped(self):
        """ISO dates work regardless of surrounding whitespace."""
        assert parse_date("  2024-01-15  ") == "2024-01-15"

    def test_invalid_format_raises_error(self):
        """Invalid formats raise ValueError."""
        with pytest.raises(ValueError, match="invalid date format"):
            parse_date("not-a-date")
        with pytest.raises(ValueError, match="invalid date format"):
            parse_date("2024/01/15")
        with pytest.raises(ValueError, match="invalid date format"):
            parse_date("Jan 15, 2024")

    def test_partial_iso_raises_error(self):
        """Partial ISO formats raise ValueError."""
        with pytest.raises(ValueError, match="invalid date format"):
            parse_date("2024-01")
        with pytest.raises(ValueError, match="invalid date format"):
            parse_date("2024")

    def test_well_shaped_but_impossible_date_raises(self):
        """I09: a shaped-but-impossible calendar date must error, not pass through.

        Otherwise it flows into filters as a lexical comparison and silently
        matches nothing/everything.
        """
        for bad in ("2024-13-45", "2024-02-30", "2024-00-00", "2024-01-32"):
            with pytest.raises(ValueError, match="not a real calendar date"):
                parse_date(bad)

    # Relative date tests (need fixed_today fixture)

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("today", "2024-06-15"),
            ("TODAY", "2024-06-15"),
            ("Today", "2024-06-15"),
            ("yesterday", "2024-06-14"),
            ("YESTERDAY", "2024-06-14"),
            ("1d", "2024-06-14"),
            ("7d", "2024-06-08"),
            ("7D", "2024-06-08"),
            ("30d", "2024-05-16"),
            ("1w", "2024-06-08"),
            ("2w", "2024-06-01"),
            ("2W", "2024-06-01"),
            ("4w", "2024-05-18"),
        ],
    )
    def test_relative_date_keywords(self, fixed_today, input_val, expected):
        """Relative date keywords resolve correctly."""
        assert parse_date(input_val) == expected


class TestParseTimestamp:
    """ISO 8601 timestamps — the form sync persists as a pull/push cursor (#21)."""

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            # The exact shape `update_last_pull` stores.
            ("2024-01-15T09:30:12.780749+00:00", "2024-01-15T09:30:12"),
            ("2024-01-15T09:30:12.780749Z", "2024-01-15T09:30:12"),
            # RFC 3339 allows either case for the UTC designator.
            ("2024-01-15T09:30:12.780749z", "2024-01-15T09:30:12"),
            ("2024-01-15T09:30:12Z", "2024-01-15T09:30:12"),
            ("2024-01-15T09:30:12", "2024-01-15T09:30:12"),
            ("2024-01-15T09:30", "2024-01-15T09:30:00"),
            ("2024-01-15 09:30:12", "2024-01-15T09:30:12"),
            # A non-UTC offset is converted, not dropped.
            ("2024-01-15T09:30:12-05:00", "2024-01-15T14:30:12"),
        ],
    )
    def test_normalized_to_naive_utc(self, input_val, expected):
        """Timestamps normalize to naive UTC, truncated to whole seconds.

        Second precision is what makes the bound a prefix of every stored
        spelling of that second — see `_normalize_timestamp`.
        """
        assert parse_date(input_val) == expected

    def test_output_is_itself_accepted(self):
        """The normalized form round-trips: a bound can be re-parsed unchanged.

        Sync stores what it parsed, so a cursor may pass through `parse_date`
        more than once over its life.
        """
        once = parse_date("2024-01-15T09:30:12.780749+00:00")
        assert once is not None
        assert parse_date(once) == once

    @pytest.mark.parametrize(
        "bad",
        [
            "2024-13-45T01:00:00Z",
            "2024-01-15T25:00:00",
            "2024-01-15T09:99",
            "2024-01-15T09:30:12+99:00",
        ],
    )
    def test_impossible_timestamp_raises(self, bad):
        """A shaped-but-impossible timestamp errors rather than passing through."""
        with pytest.raises(ValueError, match="invalid timestamp"):
            parse_date(bad)


# Every spelling of `conversations.started_at` siftd can write. Each formats
# one second: adapters differ on fractional precision and on whether they
# write a UTC designator at all.
_STORED_SHAPES = (
    "{sec}.780Z",  # claude_code — milliseconds, Z
    "{sec}.780749",  # naive microseconds
    "{sec}.780749+00:00",  # explicit offset
    "{sec}Z",  # second precision, Z
    # `epoch_ms_to_iso` whenever the millisecond component is zero — the shape
    # that a microsecond-precision bound silently excluded, because `+` sorts
    # below the `.` it lined up against.
    "{sec}+00:00",
    # No designator at all — reachable from any adapter that passes its log's
    # own timestamp through (claude_code, codex_cli, gemini_cli) when the log
    # spells it this way. Meets a second-precision bound by equality rather
    # than by being a longer string, the tightest case the prefix property has.
    # aider used to land here too, until it stopped writing local wall time
    # into a UTC column; see TestLocalToUtc.
    "{sec}",
)


def _bound(value: str) -> str:
    """`parse_date` of a non-empty value, narrowed for string comparison."""
    parsed = parse_date(value)
    assert parsed is not None
    return parsed


_CURSOR_BOUND = _bound("2024-01-15T09:30:12.780749+00:00")
_DAY_BOUND = _bound("2024-01-15")


class TestTimestampLexicalOrdering:
    """`--since` reaches SQL as a plain string compared to `started_at`.

    So the parsed bound has to sort correctly against every spelling above,
    without the filter layer knowing which adapter wrote the row.
    """

    @pytest.mark.parametrize("shape", _STORED_SHAPES)
    def test_no_row_at_or_after_the_bound_sorts_below_it(self, shape):
        """The invariant that matters: a delta pull never silently skips a row."""
        assert shape.format(sec="2024-01-15T09:30:13") >= _CURSOR_BOUND
        assert shape.format(sec="2024-01-16T00:00:00") >= _CURSOR_BOUND

    @pytest.mark.parametrize("shape", _STORED_SHAPES)
    def test_rows_before_the_bound_are_excluded(self, shape):
        """Earlier rows still sort below — the bound is not vacuous."""
        assert shape.format(sec="2024-01-15T09:30:11") < _CURSOR_BOUND
        assert shape.format(sec="2024-01-14T23:59:59") < _CURSOR_BOUND

    @pytest.mark.parametrize("shape", _STORED_SHAPES)
    def test_the_bound_is_a_prefix_of_its_own_second(self, shape):
        """The property the whole scheme rests on.

        A row anywhere in the bound's own second sorts above it whatever
        suffix its adapter wrote, so `--since` over-includes by under a second
        rather than depending on how `.`, `+`, `Z`, and digits order in ASCII.
        """
        assert shape.format(sec="2024-01-15T09:30:12") >= _CURSOR_BOUND

    @pytest.mark.parametrize("shape", _STORED_SHAPES)
    def test_date_only_bound_covers_the_whole_day(self, shape):
        """The bare-date form keeps working against every shape."""
        assert shape.format(sec="2024-01-15T00:00:00") >= _DAY_BOUND
        assert shape.format(sec="2024-01-14T23:59:59") < _DAY_BOUND


class TestLocalToUtc:
    """`local_to_utc` resolves a naive wall-clock time to a UTC instant.

    Adapters whose logs carry no offset (aider) must do this at parse time:
    `started_at` is compared as a SQL string against UTC-anchored bounds, so a
    naive local value sorts by the host's offset rather than by its instant.
    """

    CHICAGO = ZoneInfo("America/Chicago")  # UTC-5 in July, UTC-6 in January

    def test_naive_value_is_read_in_the_given_zone(self):
        assert (
            local_to_utc("2025-07-15T14:32:01", tz=self.CHICAGO)
            == "2025-07-15T19:32:01+00:00"
        )

    def test_offset_follows_the_zone_not_the_calendar(self):
        """CST vs CDT — a fixed offset would get one of these wrong."""
        assert (
            local_to_utc("2025-01-15T14:32:01", tz=self.CHICAGO)
            == "2025-01-15T20:32:01+00:00"
        )

    def test_space_separator_is_accepted(self):
        """aider's header spells the separator as a space, not a `T`."""
        assert (
            local_to_utc("2025-07-15 14:32:01", tz=self.CHICAGO)
            == "2025-07-15T19:32:01+00:00"
        )

    def test_aware_value_is_converted_not_reinterpreted(self):
        """`tz` names the zone a *naive* value was written in, nothing more."""
        assert (
            local_to_utc("2025-07-15T14:32:01+02:00", tz=self.CHICAGO)
            == "2025-07-15T12:32:01+00:00"
        )

    def test_utc_host_is_a_passthrough(self):
        """The zone in which the old behavior and the new agree."""
        assert local_to_utc("2025-07-15T14:32:01", tz=UTC) == "2025-07-15T14:32:01+00:00"

    def test_ambiguous_fall_back_hour_takes_the_earlier_instant(self):
        """01:30 happens twice on 2025-11-02 in Chicago; `fold=0` picks CDT."""
        assert (
            local_to_utc("2025-11-02T01:30:00", tz=self.CHICAGO)
            == "2025-11-02T06:30:00+00:00"
        )

    def test_host_zone_is_the_default(self):
        with pinned_tz("America/Chicago"):
            assert local_to_utc("2025-07-15T14:32:01") == "2025-07-15T19:32:01+00:00"

    def test_converted_value_is_not_skipped_by_a_cursor_at_its_own_instant(self):
        """The defect, stated as the property that failed.

        A cursor written the moment a session started must not sort above that
        session's row. Before the conversion, the naive local spelling lost by
        the size of the host's offset — five hours of conversations dropped
        from every delta pull, silently and without self-healing.
        """
        stored = local_to_utc("2025-07-15T14:32:01", tz=self.CHICAGO)
        cursor = _bound("2025-07-15T19:32:01.780749+00:00")
        assert stored >= cursor
        assert "2025-07-15T14:32:01" < cursor  # what the adapter used to write
