"""Tests for temporal weighting in search.py."""

from datetime import datetime, timedelta, timezone

import pytest

from siftd.search import apply_temporal_weight


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(conv_id: str, score: float) -> dict:
    """Build a minimal result dict."""
    return {"conversation_id": conv_id, "score": score}


def _iso_timestamp(days_ago: float) -> str:
    """Generate an ISO timestamp for N days ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------

class TestApplyTemporalWeightBasics:
    """Core apply_temporal_weight behavior."""

    def test_empty_input(self):
        """Empty results list returns empty."""
        result = apply_temporal_weight([], {})
        assert result == []

    def test_no_boost_when_disabled(self):
        """max_boost <= 1.0 returns results unchanged."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": _iso_timestamp(0)}

        out = apply_temporal_weight(results, timestamps, max_boost=1.0)
        assert out[0]["score"] == 0.8

        out = apply_temporal_weight(results, timestamps, max_boost=0.9)
        assert out[0]["score"] == 0.8

    def test_does_not_modify_original(self):
        """Original results list is not modified."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": _iso_timestamp(0)}

        apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert results[0]["score"] == 0.8  # Original unchanged

    def test_preserves_other_fields(self):
        """Non-score fields are preserved in output."""
        results = [{"conversation_id": "c1", "score": 0.8, "text": "hello", "extra": 123}]
        timestamps = {"c1": _iso_timestamp(0)}

        out = apply_temporal_weight(results, timestamps)
        assert out[0]["text"] == "hello"
        assert out[0]["extra"] == 123
        assert out[0]["conversation_id"] == "c1"


# ---------------------------------------------------------------------------
# Boost behavior
# ---------------------------------------------------------------------------

class TestTemporalBoost:
    """Verify boost values are correct."""

    def test_today_gets_max_boost(self):
        """Results from today get the maximum boost."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": _iso_timestamp(0)}  # today

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        # Should be close to 0.8 * 1.15 = 0.92
        assert 0.91 < out[0]["score"] < 0.93

    def test_half_life_decay(self):
        """At half_life days, boost should be ~half of max extra."""
        results = [_result("c1", 1.0)]  # Use 1.0 for easy math
        timestamps = {"c1": _iso_timestamp(30)}  # 30 days ago

        # At half_life=30, boost = 1 + (1.15-1) * 0.5 = 1.075
        out = apply_temporal_weight(results, timestamps, half_life_days=30.0, max_boost=1.15)
        # Should be close to 1.0 * 1.075 = 1.075
        assert 1.07 < out[0]["score"] < 1.08

    def test_old_results_approach_no_boost(self):
        """Very old results should have boost approaching 1.0 (no penalty)."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": _iso_timestamp(365)}  # 1 year ago

        out = apply_temporal_weight(results, timestamps, half_life_days=30.0, max_boost=1.15)
        # After many half-lives, boost should be very close to 1.0
        # 365 days / 30 half_life = ~12 half-lives, so boost = 1 + 0.15 * 2^-12 ≈ 1.00004
        assert 0.79 < out[0]["score"] < 0.81  # Very close to original 0.8

    def test_never_penalizes_below_original(self):
        """Old results are never scored below their original score."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": _iso_timestamp(1000)}  # Very old

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        # Score should be >= original (no penalty, just reduced boost)
        assert out[0]["score"] >= 0.8


# ---------------------------------------------------------------------------
# Missing/invalid timestamps
# ---------------------------------------------------------------------------

class TestMissingTimestamps:
    """Handle missing or invalid timestamps gracefully."""

    def test_missing_timestamp_keeps_original_score(self):
        """Results with missing timestamps keep their original score."""
        results = [_result("c1", 0.8), _result("c2", 0.7)]
        timestamps = {"c1": _iso_timestamp(0)}  # c2 is missing

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert out[0]["score"] > 0.8  # c1 gets boost
        assert out[1]["score"] == 0.7  # c2 unchanged

    def test_empty_timestamp_string(self):
        """Empty timestamp string keeps original score."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": ""}

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert out[0]["score"] == 0.8

    def test_invalid_timestamp_format(self):
        """Invalid timestamp format keeps original score."""
        results = [_result("c1", 0.8)]
        timestamps = {"c1": "not-a-date"}

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert out[0]["score"] == 0.8


# ---------------------------------------------------------------------------
# Relative ranking
# ---------------------------------------------------------------------------

class TestRelativeRanking:
    """Verify temporal weighting affects relative ordering."""

    def test_recent_beats_old_when_close(self):
        """Recent result should rank higher than old result when scores are close."""
        results = [
            _result("c_old", 0.82),    # old, slightly higher raw score
            _result("c_new", 0.80),    # new, slightly lower raw score
        ]
        timestamps = {
            "c_old": _iso_timestamp(60),   # 60 days ago
            "c_new": _iso_timestamp(1),    # 1 day ago
        }

        out = apply_temporal_weight(results, timestamps, half_life_days=30.0, max_boost=1.15)

        # After weighting, new should beat old
        out_sorted = sorted(out, key=lambda x: x["score"], reverse=True)
        assert out_sorted[0]["conversation_id"] == "c_new"

    def test_significantly_better_old_still_wins(self):
        """Old result with significantly higher score should still win."""
        results = [
            _result("c_old", 0.95),    # old but much better relevance
            _result("c_new", 0.70),    # new but much lower relevance
        ]
        timestamps = {
            "c_old": _iso_timestamp(90),   # 90 days ago
            "c_new": _iso_timestamp(0),    # today
        }

        out = apply_temporal_weight(results, timestamps, half_life_days=30.0, max_boost=1.15)

        # Even with max boost, 0.70 * 1.15 = 0.805 < 0.95 * ~1.0
        out_sorted = sorted(out, key=lambda x: x["score"], reverse=True)
        assert out_sorted[0]["conversation_id"] == "c_old"


# ---------------------------------------------------------------------------
# Timestamp format handling
# ---------------------------------------------------------------------------

class TestTimestampFormats:
    """Handle various timestamp formats."""

    def test_iso_with_z_suffix(self):
        """Handle ISO format with Z suffix."""
        results = [_result("c1", 0.8)]
        now = datetime.now(timezone.utc)
        timestamps = {"c1": now.isoformat().replace("+00:00", "Z")}

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert out[0]["score"] > 0.8  # Got boost

    def test_iso_with_offset(self):
        """Handle ISO format with timezone offset."""
        results = [_result("c1", 0.8)]
        now = datetime.now(timezone.utc)
        timestamps = {"c1": now.isoformat()}  # Has +00:00

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert out[0]["score"] > 0.8  # Got boost

    def test_iso_without_timezone(self):
        """Handle ISO format without timezone (assumed UTC)."""
        results = [_result("c1", 0.8)]
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")  # No timezone
        timestamps = {"c1": ts}

        out = apply_temporal_weight(results, timestamps, max_boost=1.15)
        assert out[0]["score"] > 0.8  # Got boost
