"""Tests for output formatters and registry."""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from painted import Fidelity

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
    yield conn, conv_id
    conn.close()


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


@pytest.fixture
def enriched_results(formatter_db):
    """Sample search results pre-enriched with metadata (as cli_search now does)."""
    _, conv_id = formatter_db
    return [
        {
            "chunk_id": "chunk1",
            "conversation_id": conv_id,
            "score": 0.85,
            "chunk_type": "prompt",
            "text": "How do I implement caching?",
            "source_ids": ["src1"],
            "_workspace": "project",
            "_started_at": "2024-01-15",
        },
        {
            "chunk_id": "chunk2",
            "conversation_id": conv_id,
            "score": 0.72,
            "chunk_type": "response",
            "text": "You can use Redis or in-memory caching...",
            "source_ids": ["src1"],
            "_workspace": "project",
            "_started_at": "2024-01-15",
        },
    ]


class TestJsonRenderSearch:
    """Tests for json_fmt.render_search."""

    def test_formats_chunk_results(self, enriched_results):
        from siftd.output import json_fmt

        result = json_fmt.render_search(
            enriched_results, Fidelity(), query="caching", mode="chunks"
        )

        assert isinstance(result, dict)
        assert result["query"] == "caching"
        assert result["mode"] == "chunks"
        assert result["result_count"] == 2
        assert len(result["results"]) == 2

    def test_includes_chunk_fields(self, enriched_results):
        from siftd.output import json_fmt

        result = json_fmt.render_search(
            enriched_results, Fidelity(), query="caching", mode="chunks"
        )

        chunk = result["results"][0]
        assert "chunk_id" in chunk
        assert "conversation_id" in chunk
        assert "score" in chunk
        assert "chunk_type" in chunk
        assert "text" in chunk
        assert "conversation" in chunk

    def test_formats_conversation_mode(self, enriched_results):
        from siftd.output import json_fmt
        from siftd.cli_search import _aggregate_conversations

        conv_results = _aggregate_conversations(enriched_results, limit=10)
        result = json_fmt.render_search(
            conv_results, Fidelity(), query="caching", mode="conversations"
        )

        assert result["mode"] == "conversations"
        assert len(result["results"]) == 1  # Both chunks same conversation

        conv = result["results"][0]
        assert "conversation_id" in conv
        assert "max_score" in conv
        assert "mean_score" in conv
        assert "chunk_count" in conv

    def test_includes_timestamp(self, enriched_results):
        from siftd.output import json_fmt

        result = json_fmt.render_search(
            enriched_results, Fidelity(), query="test", mode="chunks"
        )

        assert "timestamp" in result
        assert result["timestamp"].endswith("Z")

    def test_thread_mode(self, enriched_results):
        from siftd.output import json_fmt

        result = json_fmt.render_search(
            enriched_results, Fidelity(),
            query="caching", mode="thread",
            tier1=enriched_results[:1], tier2=enriched_results[1:],
        )

        assert result["mode"] == "thread"
        assert "tier1" in result
        assert "tier2" in result

    def test_empty_results(self):
        from siftd.output import json_fmt

        result = json_fmt.render_search(
            [], Fidelity(), query="nothing", mode="chunks"
        )

        assert result["query"] == "nothing"
        assert result["result_count"] == 0
        assert result["results"] == []


class TestTerminalRenderSearch:
    """Tests for terminal_fmt.render_search."""

    def test_chunks_mode_output(self, enriched_results):
        from siftd.output import terminal_fmt

        output = terminal_fmt.render_search(
            enriched_results, Fidelity(), query="caching", mode="chunks"
        )

        assert isinstance(output, str)
        assert "Results for: caching" in output
        # Score
        assert "0.850" in output
        # Chunk type
        assert "PROMPT" in output
        assert "RESPONSE" in output
        # Workspace
        assert "project" in output

    def test_verbose_fidelity_shows_full_text(self, enriched_results):
        """With high depth fidelity, full text is shown."""
        from siftd.output import terminal_fmt

        fidelity = Fidelity(depth=3, chars=0)  # --full equivalent
        output = terminal_fmt.render_search(
            enriched_results, fidelity, query="caching", mode="chunks"
        )

        assert "How do I implement caching?" in output
        assert "You can use Redis or in-memory caching..." in output

    def test_default_truncation(self):
        """Default fidelity truncates to 200 chars."""
        from siftd.output import terminal_fmt

        long_text = "x" * 500
        results = [{
            "conversation_id": "abc123",
            "score": 0.8,
            "chunk_type": "prompt",
            "text": long_text,
            "_workspace": "test",
            "_started_at": "2024-01-15",
        }]

        output = terminal_fmt.render_search(
            results, Fidelity(), query="test", mode="chunks"
        )

        # Should be truncated (200 chars + "...")
        assert "..." in output
        assert long_text not in output

    def test_conversations_mode(self, enriched_results):
        from siftd.output import terminal_fmt
        from siftd.cli_search import _aggregate_conversations

        conv_results = _aggregate_conversations(enriched_results, limit=10)
        output = terminal_fmt.render_search(
            conv_results, Fidelity(), query="caching", mode="conversations"
        )

        assert "Conversations for: caching" in output
        assert "max=" in output
        assert "mean=" in output
        assert "[2 chunks]" in output

    def test_thread_mode(self, enriched_results):
        from siftd.output import terminal_fmt
        from siftd.cli_search import _compute_thread_tiers

        tier1, tier2 = _compute_thread_tiers(enriched_results)
        output = terminal_fmt.render_search(
            enriched_results, Fidelity(),
            query="caching", mode="thread",
            tier1=tier1, tier2=tier2,
        )

        assert "Results for: caching" in output
        # Should have tier structure
        assert "project" in output

    def test_thread_mode_two_tiers(self):
        """Thread mode with clearly separated tiers."""
        from siftd.output import terminal_fmt

        results = [
            {"conversation_id": "conv-high", "score": 0.95, "chunk_type": "prompt",
             "text": "High relevance", "_workspace": "high", "_started_at": "2024-01-15"},
            {"conversation_id": "conv-low", "score": 0.45, "chunk_type": "prompt",
             "text": "Low relevance", "_workspace": "low", "_started_at": "2024-01-16"},
        ]
        from siftd.cli_search import _compute_thread_tiers

        tier1, tier2 = _compute_thread_tiers(results)
        output = terminal_fmt.render_search(
            results, Fidelity(),
            query="caching", mode="thread",
            tier1=tier1, tier2=tier2,
        )

        assert "More results:" in output

    def test_single_result_in_thread_tier2(self, enriched_results):
        """Single result at mean score goes to tier2."""
        from siftd.output import terminal_fmt
        from siftd.cli_search import _compute_thread_tiers

        single = enriched_results[:1]
        tier1, tier2 = _compute_thread_tiers(single)
        output = terminal_fmt.render_search(
            single, Fidelity(),
            query="caching", mode="thread",
            tier1=tier1, tier2=tier2,
        )

        # Single result at mean goes to tier2
        assert "More results:" in output

    def test_empty_results(self):
        from siftd.output import terminal_fmt

        output = terminal_fmt.render_search(
            [], Fidelity(), query="nothing", mode="chunks"
        )

        assert "Results for: nothing" in output

    def test_multiline_text_full_fidelity(self):
        """Full fidelity preserves multiline text."""
        from siftd.output import terminal_fmt

        multiline_text = "Line one\nLine two\nLine three"
        results = [{
            "conversation_id": "abc123",
            "score": 0.8,
            "chunk_type": "prompt",
            "text": multiline_text,
            "_workspace": "project",
            "_started_at": "2024-01-15",
        }]

        fidelity = Fidelity(depth=3, chars=0)
        output = terminal_fmt.render_search(
            results, fidelity, query="test", mode="chunks"
        )

        assert "Line one" in output
        assert "Line two" in output
        assert "Line three" in output

    def test_missing_workspace(self):
        """Results with no workspace don't crash."""
        from siftd.output import terminal_fmt

        results = [{
            "conversation_id": "abc123",
            "score": 0.8,
            "chunk_type": "prompt",
            "text": "No workspace",
            "_workspace": "",
            "_started_at": "2024-01-15",
        }]

        output = terminal_fmt.render_search(
            results, Fidelity(), query="test", mode="chunks"
        )

        assert "No workspace" in output

    def test_exchanges_displayed(self):
        """Pre-enriched exchanges are shown in full mode."""
        from siftd.output import terminal_fmt

        results = [{
            "conversation_id": "abc123",
            "score": 0.8,
            "chunk_type": "prompt",
            "text": "original chunk",
            "_workspace": "project",
            "_started_at": "2024-01-15",
            "_exchanges": [
                ("pid1", "What is caching?", "Caching stores data."),
            ],
        }]

        output = terminal_fmt.render_search(
            results, Fidelity(depth=3, chars=0),
            query="test", mode="chunks",
        )

        assert "> What is caching?" in output
        assert "Caching stores data." in output

    def test_context_displayed(self):
        """Pre-enriched context exchanges are shown with markers."""
        from siftd.output import terminal_fmt

        results = [{
            "conversation_id": "abc123",
            "score": 0.8,
            "chunk_type": "prompt",
            "text": "original chunk",
            "_workspace": "project",
            "_started_at": "2024-01-15",
            "_context": [
                ("pid0", "before prompt", "before response", False),
                ("pid1", "match prompt", "match response", True),
                ("pid2", "after prompt", "after response", False),
            ],
        }]

        output = terminal_fmt.render_search(
            results, Fidelity(), query="test", mode="chunks",
        )

        assert ">>>" in output
        assert "match prompt" in output


class TestMarkdownRenderSearch:
    """Tests for markdown_fmt.render_search."""

    def test_chunks_mode(self, enriched_results):
        from siftd.output import markdown_fmt

        output = markdown_fmt.render_search(
            enriched_results, Fidelity(), query="caching", mode="chunks"
        )

        assert isinstance(output, str)
        assert "## Results for: caching" in output
        assert "####" in output

    def test_conversations_mode_table(self, enriched_results):
        from siftd.output import markdown_fmt
        from siftd.cli_search import _aggregate_conversations

        conv_results = _aggregate_conversations(enriched_results, limit=10)
        output = markdown_fmt.render_search(
            conv_results, Fidelity(), query="caching", mode="conversations"
        )

        assert "## Conversations for: caching" in output
        # Should have markdown table
        assert "| ID |" in output
        assert "| --- |" in output

    def test_thread_mode(self):
        from siftd.output import markdown_fmt
        from siftd.cli_search import _compute_thread_tiers

        results = [
            {"conversation_id": "conv-high", "score": 0.95, "chunk_type": "prompt",
             "text": "High relevance", "_workspace": "high", "_started_at": "2024-01-15"},
            {"conversation_id": "conv-low", "score": 0.45, "chunk_type": "prompt",
             "text": "Low relevance", "_workspace": "low", "_started_at": "2024-01-16"},
        ]
        tier1, tier2 = _compute_thread_tiers(results)
        output = markdown_fmt.render_search(
            results, Fidelity(),
            query="caching", mode="thread",
            tier1=tier1, tier2=tier2,
        )

        assert "## Results for: caching" in output
        # tier1 has separator
        assert "---" in output
        # tier2 has more results section
        assert "### More results" in output


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


class TestSelectFormat:
    """Tests for the unified select_format system used by search."""

    def test_json_mode(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(json_mode=True, is_tty=False)
        assert fmt.name == "json"

    def test_tty_gets_terminal(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(json_mode=False, is_tty=True)
        assert fmt.name == "terminal"

    def test_non_tty_gets_markdown(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(json_mode=False, is_tty=False)
        assert fmt.name == "markdown"

    def test_explicit_name(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(name="json", is_tty=True)
        assert fmt.name == "json"

    def test_unknown_name_raises(self):
        from siftd.output.format_registry import select_format

        with pytest.raises(ValueError, match="Unknown format"):
            select_format(name="nonexistent")

    def test_all_formats_have_render_search(self):
        """All built-in formats implement render_search."""
        from siftd.output.format_registry import select_format

        for name in ("terminal", "markdown", "json"):
            fmt = select_format(name=name)
            assert hasattr(fmt, "render_search"), f"{name} missing render_search"


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
        # Create a valid drop-in formatter (new interface)
        formatter_code = '''
FORMATTER_INTERFACE_VERSION = 1
name = "custom"
media_type = "text/plain"

def render_detail(turns, fidelity, **context):
    return "custom output"
'''
        (tmp_path / "custom.py").write_text(formatter_code)

        plugins = load_dropin_formatters(tmp_path)

        assert len(plugins) == 1
        assert plugins[0].name == "custom"

    def test_skip_invalid_dropin(self, tmp_path, capsys):
        # Create an invalid drop-in (missing required attrs)
        formatter_code = '''
def render_detail(turns, fidelity, **context):
    return None
'''
        (tmp_path / "invalid.py").write_text(formatter_code)

        plugins = load_dropin_formatters(tmp_path)

        assert len(plugins) == 0
        captured = capsys.readouterr()
        assert "missing" in captured.err

    def test_skip_underscore_files(self, tmp_path):
        # Files starting with _ should be skipped
        (tmp_path / "_helper.py").write_text("name = 'helper'")

        plugins = load_dropin_formatters(tmp_path)

        assert len(plugins) == 0

    def test_dropin_overrides_builtin(self, tmp_path):
        # Create a drop-in that overrides 'json' (new interface)
        formatter_code = '''
FORMATTER_INTERFACE_VERSION = 1
name = "json"
media_type = "application/json"

def render_detail(turns, fidelity, **context):
    return "overridden"

def create_formatter():
    """Legacy compat for search formatter registry."""
    class OverrideFormatter:
        def format(self, ctx):
            print("Override!")
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
        module.FORMATTER_INTERFACE_VERSION = 1
        module.name = "test"
        module.media_type = "text/plain"
        module.render_detail = lambda turns, fidelity, **ctx: ""

        error = _validate_formatter(module, "test")

        assert error is None

    def test_missing_name(self):
        module = MagicMock(spec=[])  # No attributes

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "name" in error

    def test_wrong_name_type(self):
        module = MagicMock()
        module.FORMATTER_INTERFACE_VERSION = 1
        module.name = 123  # Should be str
        module.media_type = "text/plain"
        module.render_detail = lambda turns, fidelity, **ctx: ""

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "str" in error and "int" in error  # type mismatch

    def test_missing_render_detail(self):
        module = MagicMock()
        module.FORMATTER_INTERFACE_VERSION = 1
        module.name = "test"
        module.media_type = "text/plain"
        del module.render_detail  # Remove the callable

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "render_detail" in error

    def test_wrong_interface_version(self):
        module = MagicMock()
        module.FORMATTER_INTERFACE_VERSION = 99
        module.name = "test"
        module.media_type = "text/plain"
        module.render_detail = lambda turns, fidelity, **ctx: ""

        error = _validate_formatter(module, "test")

        assert error is not None
        assert "incompatible" in error


class TestSearchHelpers:
    """Tests for cli_search helper functions used by the new formatter flow."""

    def test_aggregate_conversations(self, enriched_results):
        from siftd.cli_search import _aggregate_conversations

        conv_results = _aggregate_conversations(enriched_results, limit=10)

        assert len(conv_results) == 1
        r = conv_results[0]
        assert r["max_score"] == 0.85
        assert r["chunk_count"] == 2
        assert r["_workspace"] == "project"

    def test_aggregate_conversations_respects_limit(self):
        from siftd.cli_search import _aggregate_conversations

        results = [
            {"conversation_id": f"conv{i}", "score": 0.9 - i * 0.1,
             "chunk_type": "prompt", "text": f"Result {i}",
             "_workspace": "", "_started_at": ""}
            for i in range(5)
        ]

        conv_results = _aggregate_conversations(results, limit=2)
        assert len(conv_results) == 2

    def test_compute_thread_tiers(self):
        from siftd.cli_search import _compute_thread_tiers

        results = [
            {"conversation_id": "high", "score": 0.95, "chunk_type": "prompt",
             "text": "high", "_workspace": "ws", "_started_at": "2024-01-15"},
            {"conversation_id": "low", "score": 0.45, "chunk_type": "prompt",
             "text": "low", "_workspace": "ws", "_started_at": "2024-01-16"},
        ]

        tier1, tier2 = _compute_thread_tiers(results)

        assert len(tier1) == 1
        assert tier1[0]["conversation_id"] == "high"
        assert len(tier2) == 1
        assert tier2[0]["conversation_id"] == "low"

    def test_compute_thread_tiers_single(self):
        """Single result at mean goes to tier2."""
        from siftd.cli_search import _compute_thread_tiers

        results = [
            {"conversation_id": "only", "score": 0.8, "chunk_type": "prompt",
             "text": "only result", "_workspace": "ws", "_started_at": "2024-01-15"},
        ]

        tier1, tier2 = _compute_thread_tiers(results)

        assert len(tier1) == 0
        assert len(tier2) == 1

    def test_fetch_search_metadata(self, mock_conn, sample_results):
        from siftd.cli_search import _fetch_search_metadata

        _fetch_search_metadata(mock_conn, sample_results)

        for r in sample_results:
            assert "_workspace" in r
            assert "_started_at" in r
            assert r["_workspace"] == "project"
            assert r["_started_at"] == "2024-01-15"


class TestSelectFormatterExtended:
    """Extended tests for formatter selection logic (old system, backward compat)."""

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
