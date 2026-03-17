from datetime import timedelta, timezone

from siftd.output.common import fmt_timestamp


def test_fmt_timestamp_converts_aware_timestamps_to_local_timezone():
    eastern = timezone(timedelta(hours=-5))

    assert fmt_timestamp("2024-01-15T10:00:00Z", local_tz=eastern) == "2024-01-15 05:00"
    assert fmt_timestamp("2024-01-15T10:00:00Z", time_only=True, local_tz=eastern) == "05:00"


def test_fmt_timestamp_leaves_naive_timestamps_unchanged():
    eastern = timezone(timedelta(hours=-5))

    assert fmt_timestamp("2024-01-15T10:00:00", local_tz=eastern) == "2024-01-15 10:00"


def test_fmt_timestamp_preserves_short_date_strings():
    assert fmt_timestamp("2024-01-15") == "2024-01-15"
    assert fmt_timestamp("2024-01-15", time_only=True) == ""
