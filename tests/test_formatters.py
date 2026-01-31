"""Tests for output formatters and registry."""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from siftd.output import (
    ChunkListFormatter,
    ContextFormatter,
    ConversationFormatter,
    FormatterContext,
    FullExchangeFormatter,
    JsonFormatter,
    ThreadFormatter,
    VerboseFormatter,
    select_formatter,
)
from siftd.output.registry import (
    FormatterRegistry,
    _validate_formatter,
    load_dropin_formatters,
)


@pytest.fixture
def formatter_db(tmp_path):
    """Create a real database with full schema and sample data for formatters.

    Returns (conn, conv_id) so sample_results can reference the real ID.
    """
    from siftd.storage.sqlite import (
        create_database,
        get_or_create_harness,
        get_or_create_workspace,
        insert_conversation,
    )

    db_path = tmp_path / "formatter_test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-15T10:00:00Z")
    conv_id = insert_conversation(
        conn,
        external_id="conv123",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )
    conn.commit()
    return conn, conv_id


@pytest.fixture
def mock_conn(formatter_db):
    """Database connection for formatter tests."""
    conn, _ = formatter_db
    return conn


@pytest.fixture
def sample_results(formatter_db):
    """Sample search results with conversation_id matching the real DB."""
    _, conv_id = formatter_db
    return [
        {
            "chunk_id": "chunk1",
            "conversation_id": conv_id,
            "score": 0.85,
            "chunk_type": "prompt",
            "text": "How do I implement caching?",
            "source_ids": ["src1"],
        },
        {
            "chunk_id": "chunk2",
            "conversation_id": conv_id,
            "score": 0.72,
            "chunk_type": "response",
            "text": "You can use Redis or in-memory caching...",
            "source_ids": ["src1"],
        },
    ]


class TestJsonFormatter:
    def test_formats_chunk_results(self, mock_conn, sample_results, capsys):
        args = argparse.Namespace(conversations=False, limit=10)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = JsonFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["query"] == "caching"
        assert output["mode"] == "chunks"
        assert output["result_count"] == 2
        assert len(output["results"]) == 2

    def test_includes_chunk_fields(self, mock_conn, sample_results, capsys):
        args = argparse.Namespace(conversations=False, limit=10)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = JsonFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        chunk = output["results"][0]
        assert "chunk_id" in chunk
        assert "conversation_id" in chunk
        assert "score" in chunk
        assert "chunk_type" in chunk
        assert "text" in chunk
        assert "conversation" in chunk

    def test_formats_conversation_mode(self, mock_conn, sample_results, capsys):
        args = argparse.Namespace(conversations=True, limit=10)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = JsonFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["mode"] == "conversations"
        assert len(output["results"]) == 1  # Both chunks same conversation

        conv = output["results"][0]
        assert "conversation_id" in conv
        assert "max_score" in conv
        assert "mean_score" in conv
        assert "chunk_count" in conv
        assert "best_chunk" in conv

    def test_includes_timestamp(self, mock_conn, sample_results, capsys):
        args = argparse.Namespace(conversations=False, limit=10)
        ctx = FormatterContext(
            query="test",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = JsonFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert "timestamp" in output
        assert output["timestamp"].endswith("Z")


class TestSelectFormatter:
    def test_default_is_chunk_list(self):
        args = argparse.Namespace()
        formatter = select_formatter(args)
        assert isinstance(formatter, ChunkListFormatter)

    def test_verbose_flag(self):
        args = argparse.Namespace(verbose=True)
        formatter = select_formatter(args)
        assert isinstance(formatter, VerboseFormatter)

    def test_json_flag(self):
        args = argparse.Namespace(json=True)
        formatter = select_formatter(args)
        assert isinstance(formatter, JsonFormatter)

    def test_json_flag_priority(self):
        # --json should work even with --verbose
        args = argparse.Namespace(json=True, verbose=True)
        formatter = select_formatter(args)
        assert isinstance(formatter, JsonFormatter)

    def test_format_argument(self):
        args = argparse.Namespace(format="json")
        formatter = select_formatter(args)
        assert isinstance(formatter, JsonFormatter)

    def test_unknown_format_raises_error(self):
        args = argparse.Namespace(format="nonexistent")
        with pytest.raises(ValueError) as exc_info:
            select_formatter(args)
        assert "Unknown format 'nonexistent'" in str(exc_info.value)
        assert "Available:" in str(exc_info.value)
        assert "json" in str(exc_info.value)


class TestFormatterRegistry:
    def test_builtin_formatters_available(self):
        registry = FormatterRegistry(dropin_path=Path("/nonexistent"))

        names = registry.list_names()

        assert "default" in names
        assert "verbose" in names
        assert "json" in names
        assert "thread" in names
        assert "conversations" in names

    def test_get_builtin_formatter(self):
        registry = FormatterRegistry(dropin_path=Path("/nonexistent"))

        formatter = registry.get("json")

        assert formatter is not None
        assert isinstance(formatter, JsonFormatter)

    def test_get_unknown_returns_none(self):
        registry = FormatterRegistry(dropin_path=Path("/nonexistent"))

        formatter = registry.get("nonexistent_formatter")

        assert formatter is None


class TestDropinFormatters:
    def test_load_valid_dropin(self, tmp_path):
        # Create a valid drop-in formatter
        formatter_code = '''
NAME = "custom"

class CustomFormatter:
    def format(self, ctx):
        print("Custom output")

def create_formatter():
    return CustomFormatter()
'''
        (tmp_path / "custom.py").write_text(formatter_code)

        plugins = load_dropin_formatters(tmp_path)

        assert len(plugins) == 1
        assert plugins[0].name == "custom"

    def test_skip_invalid_dropin(self, tmp_path, capsys):
        # Create an invalid drop-in (missing NAME)
        formatter_code = '''
def create_formatter():
    return None
'''
        (tmp_path / "invalid.py").write_text(formatter_code)

        plugins = load_dropin_formatters(tmp_path)

        assert len(plugins) == 0
        captured = capsys.readouterr()
        assert "missing" in captured.err and "NAME" in captured.err

    def test_skip_underscore_files(self, tmp_path):
        # Files starting with _ should be skipped
        (tmp_path / "_helper.py").write_text("NAME = 'helper'")

        plugins = load_dropin_formatters(tmp_path)

        assert len(plugins) == 0

    def test_dropin_overrides_builtin(self, tmp_path):
        # Create a drop-in that overrides 'json'
        formatter_code = '''
NAME = "json"

class OverrideFormatter:
    def format(self, ctx):
        print("Override!")

def create_formatter():
    return OverrideFormatter()
'''
        (tmp_path / "json_override.py").write_text(formatter_code)

        registry = FormatterRegistry(dropin_path=tmp_path)
        formatter = registry.get("json")

        # Should get the drop-in, not the built-in
        assert formatter is not None
        assert type(formatter).__name__ == "OverrideFormatter"


class TestValidateFormatter:
    def test_valid_module(self):
        module = MagicMock()
        module.NAME = "test"
        module.create_formatter = lambda: None

        error = _validate_formatter(module, "test")

        assert error is None

    def test_missing_name(self):
        module = MagicMock(spec=[])  # No attributes

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "NAME" in error

    def test_wrong_name_type(self):
        module = MagicMock()
        module.NAME = 123  # Should be str

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "str" in error and "int" in error  # type mismatch

    def test_missing_create_formatter(self):
        module = MagicMock()
        module.NAME = "test"
        del module.create_formatter  # Remove the callable

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "create_formatter" in error


class TestVerboseFormatter:
    """Tests for VerboseFormatter output structure."""

    def test_outputs_full_chunk_text(self, mock_conn, sample_results, capsys):
        """VerboseFormatter shows full chunk text without truncation."""
        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = VerboseFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Full text should appear, not truncated
        assert "How do I implement caching?" in captured.out
        assert "You can use Redis or in-memory caching..." in captured.out

    def test_includes_score_and_metadata(self, mock_conn, sample_results, capsys):
        """VerboseFormatter includes score, chunk type, and workspace."""
        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = VerboseFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Should show scores
        assert "0.85" in captured.out or "0.850" in captured.out
        # Should show chunk type (PROMPT/RESPONSE)
        assert "PROMPT" in captured.out
        assert "RESPONSE" in captured.out
        # Should show workspace name
        assert "project" in captured.out

    def test_shows_query_header(self, mock_conn, sample_results, capsys):
        """VerboseFormatter shows query header."""
        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = VerboseFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        assert "Results for: caching" in captured.out

    def test_chrono_mode_sorts_by_time(self, mock_conn, capsys):
        """VerboseFormatter with chrono=True sorts by timestamp."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
        )

        # Create a second conversation with earlier timestamp
        harness_id = get_or_create_harness(mock_conn, "test2", source="test", log_format="jsonl")
        workspace_id = get_or_create_workspace(mock_conn, "/other/project", "2024-01-10T10:00:00Z")
        conv2_id = insert_conversation(
            mock_conn,
            external_id="conv456",
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at="2024-01-10T10:00:00Z",  # Earlier than conv1
        )
        mock_conn.commit()

        # Get the first conv_id from the database
        conv1_id = mock_conn.execute(
            "SELECT id FROM conversations WHERE external_id = 'conv123'"
        ).fetchone()[0]

        results = [
            {"chunk_id": "c1", "conversation_id": conv1_id, "score": 0.9,
             "chunk_type": "prompt", "text": "Later conversation", "source_ids": []},
            {"chunk_id": "c2", "conversation_id": conv2_id, "score": 0.8,
             "chunk_type": "prompt", "text": "Earlier conversation", "source_ids": []},
        ]

        args = argparse.Namespace(chrono=True)
        ctx = FormatterContext(query="test", results=results, conn=mock_conn, args=args)

        formatter = VerboseFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Earlier conversation should appear first in chrono mode
        earlier_pos = captured.out.find("Earlier conversation")
        later_pos = captured.out.find("Later conversation")
        assert earlier_pos < later_pos


class TestThreadFormatter:
    """Tests for ThreadFormatter two-tier output."""

    def test_outputs_thread_format(self, mock_conn, sample_results, capsys):
        """ThreadFormatter produces output with tier structure."""
        args = argparse.Namespace()
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = ThreadFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        assert "Results for: caching" in captured.out
        # Should show project name (workspace)
        assert "project" in captured.out

    def test_tier1_expanded_tier2_compact(self, mock_conn, capsys):
        """ThreadFormatter shows tier1 expanded, tier2 as shortlist."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
        )

        # Create multiple conversations with different scores
        harness_id = get_or_create_harness(mock_conn, "test2", source="test", log_format="jsonl")

        # High-score conversation (tier 1)
        ws1_id = get_or_create_workspace(mock_conn, "/high/project", "2024-01-15T10:00:00Z")
        high_conv_id = insert_conversation(
            mock_conn, external_id="conv-high", harness_id=harness_id,
            workspace_id=ws1_id, started_at="2024-01-15T10:00:00Z",
        )

        # Low-score conversation (tier 2)
        ws2_id = get_or_create_workspace(mock_conn, "/low/project", "2024-01-16T10:00:00Z")
        low_conv_id = insert_conversation(
            mock_conn, external_id="conv-low", harness_id=harness_id,
            workspace_id=ws2_id, started_at="2024-01-16T10:00:00Z",
        )
        mock_conn.commit()

        results = [
            {"chunk_id": "c1", "conversation_id": high_conv_id, "score": 0.95,
             "chunk_type": "prompt", "text": "High relevance result about caching", "source_ids": []},
            {"chunk_id": "c2", "conversation_id": low_conv_id, "score": 0.45,
             "chunk_type": "prompt", "text": "Low relevance result", "source_ids": []},
        ]

        args = argparse.Namespace()
        ctx = FormatterContext(query="caching", results=results, conn=mock_conn, args=args)

        formatter = ThreadFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Tier 2 header should appear
        assert "More results:" in captured.out

    def test_single_result_in_tier2(self, mock_conn, sample_results, capsys):
        """ThreadFormatter with single result at mean score goes to tier2."""
        # Use only first result - its score equals the mean (since it's the only one)
        # so it goes to tier2 (tier1 requires score > mean)
        args = argparse.Namespace()
        ctx = FormatterContext(
            query="caching",
            results=sample_results[:1],
            conn=mock_conn,
            args=args,
        )

        formatter = ThreadFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Single result at mean goes to tier2
        assert "More results:" in captured.out


class TestConversationFormatter:
    """Tests for ConversationFormatter aggregated output."""

    def test_aggregates_by_conversation(self, mock_conn, sample_results, capsys):
        """ConversationFormatter groups chunks by conversation."""
        args = argparse.Namespace(limit=10)
        ctx = FormatterContext(
            query="caching",
            results=sample_results,
            conn=mock_conn,
            args=args,
        )

        formatter = ConversationFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Should show "Conversations for:" header
        assert "Conversations for: caching" in captured.out
        # Should show max/mean scores
        assert "max=" in captured.out
        assert "mean=" in captured.out
        # Should show chunk count
        assert "[2 chunks]" in captured.out  # Both sample results are from same conv

    def test_respects_limit(self, mock_conn, capsys):
        """ConversationFormatter respects limit argument."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
        )

        harness_id = get_or_create_harness(mock_conn, "test2", source="test", log_format="jsonl")
        results = []

        # Create 5 conversations
        for i in range(5):
            ws_id = get_or_create_workspace(mock_conn, f"/project{i}", "2024-01-15T10:00:00Z")
            conv_id = insert_conversation(
                mock_conn, external_id=f"conv{i}", harness_id=harness_id,
                workspace_id=ws_id, started_at=f"2024-01-1{i}T10:00:00Z",
            )
            results.append({
                "chunk_id": f"c{i}", "conversation_id": conv_id, "score": 0.9 - i * 0.1,
                "chunk_type": "prompt", "text": f"Result {i}", "source_ids": []
            })
        mock_conn.commit()

        args = argparse.Namespace(limit=2)
        ctx = FormatterContext(query="test", results=results, conn=mock_conn, args=args)

        formatter = ConversationFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Should only show 2 conversations (limited)
        assert captured.out.count("max=") == 2


class TestFormatterEdgeCases:
    """Edge case tests for formatters."""

    def test_empty_results(self, mock_conn, capsys):
        """Formatters handle empty results gracefully."""
        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(query="nothing", results=[], conn=mock_conn, args=args)

        formatter = ChunkListFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        assert "Results for: nothing" in captured.out

    def test_single_result(self, mock_conn, sample_results, capsys):
        """Formatters handle single result."""
        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(
            query="caching",
            results=[sample_results[0]],
            conn=mock_conn,
            args=args,
        )

        formatter = VerboseFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        assert "How do I implement caching?" in captured.out

    def test_missing_workspace(self, mock_conn, capsys):
        """Formatters handle results with no workspace."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            insert_conversation,
        )

        harness_id = get_or_create_harness(mock_conn, "test_no_ws", source="test", log_format="jsonl")
        # Insert conversation with NULL workspace
        conv_id = insert_conversation(
            mock_conn, external_id="conv-no-ws", harness_id=harness_id,
            workspace_id=None, started_at="2024-01-15T10:00:00Z",
        )
        mock_conn.commit()

        results = [
            {"chunk_id": "c1", "conversation_id": conv_id, "score": 0.8,
             "chunk_type": "prompt", "text": "No workspace", "source_ids": []}
        ]

        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(query="test", results=results, conn=mock_conn, args=args)

        formatter = ChunkListFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Should not crash, and show the result
        assert "No workspace" in captured.out

    def test_multiline_text(self, mock_conn, capsys):
        """VerboseFormatter preserves multiline text."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
        )

        harness_id = get_or_create_harness(mock_conn, "test_ml", source="test", log_format="jsonl")
        ws_id = get_or_create_workspace(mock_conn, "/multi/project", "2024-01-15T10:00:00Z")
        conv_id = insert_conversation(
            mock_conn, external_id="conv-ml", harness_id=harness_id,
            workspace_id=ws_id, started_at="2024-01-15T10:00:00Z",
        )
        mock_conn.commit()

        multiline_text = "Line one\nLine two\nLine three"
        results = [
            {"chunk_id": "c1", "conversation_id": conv_id, "score": 0.8,
             "chunk_type": "prompt", "text": multiline_text, "source_ids": []}
        ]

        args = argparse.Namespace(chrono=False)
        ctx = FormatterContext(query="test", results=results, conn=mock_conn, args=args)

        formatter = VerboseFormatter()
        formatter.format(ctx)

        captured = capsys.readouterr()
        # Each line should be preserved
        assert "Line one" in captured.out
        assert "Line two" in captured.out
        assert "Line three" in captured.out


class TestSelectFormatterExtended:
    """Extended tests for formatter selection logic."""

    def test_thread_flag(self):
        """--thread selects ThreadFormatter."""
        args = argparse.Namespace(thread=True)
        formatter = select_formatter(args)
        assert isinstance(formatter, ThreadFormatter)

    def test_conversations_flag(self):
        """--conversations selects ConversationFormatter."""
        args = argparse.Namespace(conversations=True)
        formatter = select_formatter(args)
        assert isinstance(formatter, ConversationFormatter)

    def test_context_flag(self):
        """--context N selects ContextFormatter."""
        args = argparse.Namespace(context=2)
        formatter = select_formatter(args)
        assert isinstance(formatter, ContextFormatter)
        assert formatter.n == 2

    def test_full_flag(self):
        """--full selects FullExchangeFormatter."""
        args = argparse.Namespace(full=True)
        formatter = select_formatter(args)
        assert isinstance(formatter, FullExchangeFormatter)
