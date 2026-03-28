"""Tests for stats cache (write at ingest, read in db stats)."""

import json
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest
from conftest import make_db

from siftd.api.stats import (
    DatabaseStats,
    HarnessCount,
    HarnessInfo,
    TableCounts,
    TagStats,
    TokenCoverage,
    TokenCoverageByHarness,
    ToolStats,
    WorkspaceStats,
    _dict_to_stats,
    dict_to_stats,
    _stats_to_dict,
    read_stats_cache,
    stats_cache_path,
    write_stats_cache,
)
from siftd.serialization.stats import serialize_stats


def _make_stats(db_path: Path) -> DatabaseStats:
    """Build a minimal DatabaseStats for testing."""
    return DatabaseStats(
        db_path=db_path,
        db_size_bytes=1024,
        counts=TableCounts(
            conversations=10,
            prompts=20,
            responses=20,
            tool_calls=5,
            harnesses=2,
            workspaces=3,
            tools=4,
            models=2,
            ingested_files=15,
        ),
        harnesses=[
            HarnessInfo(name="claude_code", source="~/.claude", log_format="jsonl"),
            HarnessInfo(name="aider", source="~/.aider", log_format=None),
        ],
        harness_counts=[
            HarnessCount(name="claude_code", conversation_count=7),
            HarnessCount(name="aider", conversation_count=3),
        ],
        top_workspaces=[
            WorkspaceStats(path="/home/user/project", conversation_count=5, last_activity="2024-06-01T10:00:00Z"),
        ],
        models=["claude-3-opus", "gpt-4"],
        top_tools=[
            ToolStats(name="Read", usage_count=100),
            ToolStats(name="Write", usage_count=50),
        ],
        top_tags=[
            TagStats(name="debug", count=3),
        ],
        token_coverage=TokenCoverage(
            responses=20,
            with_tokens=18,
            pct_with_tokens=90.0,
            by_harness=[
                TokenCoverageByHarness(name="claude_code", responses=14, with_tokens=14, pct_with_tokens=100.0),
                TokenCoverageByHarness(name="aider", responses=6, with_tokens=4, pct_with_tokens=66.67),
            ],
        ),
        activity_window=("2024-01-01T00:00:00Z", "2024-06-01T10:00:00Z"),
        last_ingest_at="2024-06-01T12:00:00Z",
    )


class TestStatsRoundTrip:
    """Serialize → deserialize preserves all fields."""

    def test_dict_roundtrip(self, tmp_path):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)

        d = _stats_to_dict(stats)
        restored = dict_to_stats(d)

        assert restored.counts == stats.counts
        assert restored.models == stats.models
        assert len(restored.harnesses) == len(stats.harnesses)
        assert restored.harnesses[0].name == stats.harnesses[0].name
        assert restored.harnesses[1].log_format is None
        assert restored.top_workspaces[0].conversation_count == 5
        assert restored.top_tools[0].usage_count == 100
        assert restored.top_tags[0].count == 3
        assert restored.token_coverage.pct_with_tokens == 90.0
        assert restored.token_coverage.by_harness[1].pct_with_tokens == 66.67
        assert restored.activity_window == ("2024-01-01T00:00:00Z", "2024-06-01T10:00:00Z")
        assert restored.last_ingest_at == "2024-06-01T12:00:00Z"
        assert restored.harness_counts[0].conversation_count == 7

    def test_private_alias_matches_public(self, tmp_path):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)

        d = _stats_to_dict(stats)
        assert _dict_to_stats(d) == dict_to_stats(d)

    def test_serialize_stats_contract_matches_dataclasses(self, tmp_path):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)

        payload = serialize_stats(stats)
        assert set(payload) == {f.name for f in dataclass_fields(DatabaseStats)}
        assert set(payload["counts"]) == {f.name for f in dataclass_fields(TableCounts)}
        assert set(payload["token_coverage"]) == {f.name for f in dataclass_fields(TokenCoverage)}
        assert set(payload["token_coverage"]["by_harness"][0]) == {
            f.name for f in dataclass_fields(TokenCoverageByHarness)
        }

    def test_write_read_roundtrip(self, tmp_path, monkeypatch):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)

        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
        monkeypatch.setattr("siftd.api.stats.default_db_path", lambda: db_file)

        write_stats_cache(stats)

        cache = stats_cache_path()
        assert cache.exists()

        restored = read_stats_cache(db_path=db_file)
        assert restored is not None
        assert restored.counts.conversations == 10
        assert restored.models == ["claude-3-opus", "gpt-4"]

    def test_write_read_with_none_activity_window(self, tmp_path, monkeypatch):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)
        # Override with None values
        stats = DatabaseStats(
            **{**stats.__dict__, "activity_window": (None, None), "last_ingest_at": None}
        )

        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
        monkeypatch.setattr("siftd.api.stats.default_db_path", lambda: db_file)

        write_stats_cache(stats)
        restored = read_stats_cache(db_path=db_file)
        assert restored is not None
        assert restored.activity_window == (None, None)
        assert restored.last_ingest_at is None


class TestCacheMiss:
    """Cases where read_stats_cache returns None."""

    def test_missing_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "no-such-dir")
        assert read_stats_cache() is None

    def test_corrupt_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path)
        cache = tmp_path / "stats.json"
        cache.write_text("not valid json {{{")
        assert read_stats_cache() is None

    def test_wrong_db_path_returns_none(self, tmp_path, monkeypatch):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)

        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
        write_stats_cache(stats)

        # Read with a different db_path
        other_db = tmp_path / "other.db"
        other_db.write_bytes(b"y" * 512)
        assert read_stats_cache(db_path=other_db) is None


class TestCacheMeta:
    """Cache file includes metadata for staleness detection."""

    def test_cache_has_meta(self, tmp_path, monkeypatch):
        db_file = tmp_path / "siftd.db"
        db_file.write_bytes(b"x" * 1024)
        stats = _make_stats(db_file)

        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
        write_stats_cache(stats)

        raw = json.loads((tmp_path / "cache" / "stats.json").read_text())
        assert "_meta" in raw
        assert "computed_at" in raw["_meta"]
        assert "db_mtime_ns" in raw["_meta"]
        assert isinstance(raw["_meta"]["db_mtime_ns"], int)


class TestIngestWritesCache:
    """Integration: cmd_ingest writes the stats cache."""

    @pytest.mark.slow
    def test_ingest_creates_cache(self, tmp_path, monkeypatch):
        from siftd.cli import main

        monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
        db = tmp_path / "test.db"

        # Create a DB with a conversation so ingest has something to count
        make_db(db, conversations=[{"external_id": "c1"}])

        # Run ingest (nothing new to ingest, but cache should still be written)
        monkeypatch.setattr("siftd.paths.db_path", lambda: db)
        main(["--db", str(db), "ingest", "--quiet"])

        cache = tmp_path / "cache" / "stats.json"
        assert cache.exists()

        restored = read_stats_cache(db_path=db)
        assert restored is not None
        assert restored.counts.conversations >= 1
