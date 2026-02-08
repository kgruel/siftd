"""Tests for relative date parsing in CLI."""

from datetime import date
from unittest.mock import patch

import pytest

from siftd.cli_common import parse_date


class TestParseDate:
    """Unit tests for parse_date function."""

    @pytest.fixture
    def fixed_today(self):
        """Patch date.today to return 2024-06-15 for all tests."""
        with patch("siftd.cli_common.date") as mock_date:
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
        """Whitespace-only string raises ArgumentTypeError."""
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            parse_date("   ")

    def test_iso_format_passthrough(self):
        """ISO format dates pass through unchanged."""
        assert parse_date("2024-01-15") == "2024-01-15"
        assert parse_date("2023-12-31") == "2023-12-31"

    def test_iso_format_whitespace_stripped(self):
        """ISO dates work regardless of surrounding whitespace."""
        assert parse_date("  2024-01-15  ") == "2024-01-15"

    def test_invalid_format_raises_error(self):
        """Invalid formats raise ArgumentTypeError."""
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="invalid date format"):
            parse_date("not-a-date")
        with pytest.raises(argparse.ArgumentTypeError, match="invalid date format"):
            parse_date("2024/01/15")
        with pytest.raises(argparse.ArgumentTypeError, match="invalid date format"):
            parse_date("Jan 15, 2024")

    def test_partial_iso_raises_error(self):
        """Partial ISO formats raise ArgumentTypeError."""
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="invalid date format"):
            parse_date("2024-01")
        with pytest.raises(argparse.ArgumentTypeError, match="invalid date format"):
            parse_date("2024")

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
