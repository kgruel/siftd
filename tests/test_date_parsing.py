"""Tests for relative date parsing in CLI."""

from datetime import date
from unittest.mock import patch

import pytest

from siftd.cli import parse_date


class TestParseDate:
    """Unit tests for parse_date function."""

    @pytest.fixture
    def fixed_today(self):
        """Patch date.today to return 2024-06-15 for all tests."""
        with patch("siftd.cli.date") as mock_date:
            mock_date.today.return_value = date(2024, 6, 15)
            yield

    # Non-relative tests (no mock needed)

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

    def test_iso_format_whitespace_stripped(self):
        """ISO dates work regardless of surrounding whitespace."""
        assert parse_date("  2024-01-15  ") == "2024-01-15"

    def test_invalid_format_passthrough(self):
        """Invalid formats pass through unchanged for downstream handling."""
        assert parse_date("not-a-date") == "not-a-date"
        assert parse_date("2024/01/15") == "2024/01/15"
        assert parse_date("Jan 15, 2024") == "jan 15, 2024"  # lowercased

    def test_partial_iso_passthrough(self):
        """Partial ISO formats pass through unchanged."""
        assert parse_date("2024-01") == "2024-01"
        assert parse_date("2024") == "2024"

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
