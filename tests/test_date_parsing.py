"""Tests for relative date parsing in CLI."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from siftd.cli import parse_date


class TestParseDate:
    """Unit tests for parse_date function."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert parse_date(None) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert parse_date("") is None

    def test_whitespace_returns_falsy(self):
        """Whitespace-only string returns falsy value."""
        assert not parse_date("   ")

    def test_iso_format_passthrough(self):
        """ISO format dates pass through unchanged."""
        assert parse_date("2024-01-15") == "2024-01-15"
        assert parse_date("2023-12-31") == "2023-12-31"

    def test_iso_format_case_insensitive(self):
        """ISO dates work regardless of surrounding whitespace."""
        assert parse_date("  2024-01-15  ") == "2024-01-15"

    @patch("siftd.cli.date")
    def test_today_keyword(self, mock_date):
        """'today' returns current date in ISO format."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("today") == "2024-06-15"

    @patch("siftd.cli.date")
    def test_today_case_insensitive(self, mock_date):
        """'TODAY' works case-insensitively."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("TODAY") == "2024-06-15"
        assert parse_date("Today") == "2024-06-15"

    @patch("siftd.cli.date")
    def test_yesterday_keyword(self, mock_date):
        """'yesterday' returns previous day in ISO format."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("yesterday") == "2024-06-14"

    @patch("siftd.cli.date")
    def test_yesterday_case_insensitive(self, mock_date):
        """'YESTERDAY' works case-insensitively."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("YESTERDAY") == "2024-06-14"

    @patch("siftd.cli.date")
    def test_relative_days(self, mock_date):
        """Relative day format (Nd) subtracts N days."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("1d") == "2024-06-14"
        assert parse_date("7d") == "2024-06-08"
        assert parse_date("30d") == "2024-05-16"

    @patch("siftd.cli.date")
    def test_relative_days_case_insensitive(self, mock_date):
        """Relative days work case-insensitively."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("7D") == "2024-06-08"

    @patch("siftd.cli.date")
    def test_relative_weeks(self, mock_date):
        """Relative week format (Nw) subtracts N weeks."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("1w") == "2024-06-08"
        assert parse_date("2w") == "2024-06-01"
        assert parse_date("4w") == "2024-05-18"

    @patch("siftd.cli.date")
    def test_relative_weeks_case_insensitive(self, mock_date):
        """Relative weeks work case-insensitively."""
        mock_date.today.return_value = date(2024, 6, 15)
        assert parse_date("2W") == "2024-06-01"

    def test_invalid_format_passthrough(self):
        """Invalid formats pass through unchanged for downstream handling."""
        assert parse_date("not-a-date") == "not-a-date"
        assert parse_date("2024/01/15") == "2024/01/15"
        assert parse_date("Jan 15, 2024") == "jan 15, 2024"  # lowercased

    def test_partial_iso_passthrough(self):
        """Partial ISO formats pass through unchanged."""
        assert parse_date("2024-01") == "2024-01"
        assert parse_date("2024") == "2024"
