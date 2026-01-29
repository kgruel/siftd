"""Tests for output formatters and registry."""

import argparse
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from siftd.output import (
    ChunkListFormatter,
    FormatterContext,
    JsonFormatter,
    VerboseFormatter,
    get_formatter,
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
        get_or_create_workspace,
        get_or_create_harness,
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

        formatters = load_dropin_formatters(tmp_path)

        assert "custom" in formatters

    def test_skip_invalid_dropin(self, tmp_path, capsys):
        # Create an invalid drop-in (missing NAME)
        formatter_code = '''
def create_formatter():
    return None
'''
        (tmp_path / "invalid.py").write_text(formatter_code)

        formatters = load_dropin_formatters(tmp_path)

        assert len(formatters) == 0
        captured = capsys.readouterr()
        assert "missing required attribute 'NAME'" in captured.err

    def test_skip_underscore_files(self, tmp_path):
        # Files starting with _ should be skipped
        (tmp_path / "_helper.py").write_text("NAME = 'helper'")

        formatters = load_dropin_formatters(tmp_path)

        assert len(formatters) == 0

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
        assert "must be str" in error

    def test_missing_create_formatter(self):
        module = MagicMock()
        module.NAME = "test"
        del module.create_formatter  # Remove the callable

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "create_formatter" in error
