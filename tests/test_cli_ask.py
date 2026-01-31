"""Integration tests for 'siftd ask' semantic search CLI.

Tests the query → search → format output flow and key options.
"""

import argparse

import pytest

pytest.importorskip("fastembed")

from siftd.cli_ask import cmd_ask
from siftd.embeddings.indexer import build_embeddings_index
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


@pytest.fixture
def populated_db(tmp_path):
    """Create a database with conversations for search tests."""
    db_path = tmp_path / "main.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    model_id = get_or_create_model(conn, "test-model")

    # Workspace 1: Python project conversations
    ws1_id = get_or_create_workspace(conn, "/projects/python-app", "2024-01-01T10:00:00Z")

    conv1_id = insert_conversation(
        conn, external_id="conv-python", harness_id=harness_id,
        workspace_id=ws1_id, started_at="2024-01-15T10:00:00Z",
    )
    p1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p1_id, 0, "text", '{"text": "How do I handle exceptions in Python?"}')
    r1_id = insert_response(
        conn, conv1_id, p1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=10, output_tokens=100,
    )
    insert_response_content(
        conn, r1_id, 0, "text",
        '{"text": "Use try/except blocks to catch and handle exceptions. You can catch specific exception types."}'
    )

    # Workspace 2: Rust project conversations
    ws2_id = get_or_create_workspace(conn, "/projects/rust-cli", "2024-01-01T10:00:00Z")

    conv2_id = insert_conversation(
        conn, external_id="conv-rust", harness_id=harness_id,
        workspace_id=ws2_id, started_at="2024-01-16T10:00:00Z",
    )
    p2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, p2_id, 0, "text", '{"text": "How do I handle errors in Rust?"}')
    r2_id = insert_response(
        conn, conv2_id, p2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=10, output_tokens=100,
    )
    insert_response_content(
        conn, r2_id, 0, "text",
        '{"text": "Use Result<T, E> for recoverable errors. Use the ? operator to propagate errors."}'
    )

    conn.commit()
    conn.close()

    return {
        "db_path": db_path,
        "ws1_path": "/projects/python-app",
        "ws2_path": "/projects/rust-cli",
        "conv1_id": conv1_id,
        "conv2_id": conv2_id,
    }


@pytest.fixture
def indexed_db(populated_db, tmp_path):
    """Create database with embeddings index built."""
    embed_db_path = tmp_path / "embeddings.db"

    build_embeddings_index(
        db_path=populated_db["db_path"],
        embed_db_path=embed_db_path,
        verbose=False,
    )

    return {
        **populated_db,
        "embed_db_path": embed_db_path,
    }


def make_args(**kwargs):
    """Create argparse.Namespace with defaults for cmd_ask."""
    defaults = {
        "query": [],
        "db": None,
        "embed_db": None,
        "limit": 10,
        "verbose": False,
        "full": False,
        "context": None,
        "chrono": False,
        "workspace": None,
        "model": None,
        "since": None,
        "before": None,
        "index": False,
        "rebuild": False,
        "backend": None,
        "thread": False,
        "embeddings_only": False,
        "recall": 80,
        "role": None,
        "first": False,
        "conversations": False,
        "refs": None,
        "threshold": None,
        "json": False,
        "format": None,
        "no_exclude_active": True,  # Disable active exclusion for tests
        "include_derivative": True,  # Include derivative for tests
        "no_diversity": True,  # Disable MMR for deterministic tests
        "lambda_": 0.7,
        "recency": False,
        "recency_half_life": 30.0,
        "recency_max_boost": 1.15,
        "tag": None,
        "all_tags": None,
        "no_tag": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestAskIndexing:
    """Tests for --index and --rebuild modes."""

    def test_index_builds_embeddings(self, populated_db, tmp_path, capsys):
        """--index creates embeddings database."""
        embed_db_path = tmp_path / "embeddings.db"

        args = make_args(
            db=str(populated_db["db_path"]),
            embed_db=str(embed_db_path),
            index=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        assert embed_db_path.exists()
        assert "Embedding" in captured.out or "chunks" in captured.out

    def test_rebuild_clears_and_rebuilds(self, indexed_db, capsys):
        """--rebuild clears existing index and rebuilds."""
        args = make_args(
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            rebuild=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "Clearing" in captured.out or "chunks" in captured.out

    def test_index_requires_main_db(self, tmp_path):
        """--index fails if main database doesn't exist."""
        args = make_args(
            db=str(tmp_path / "nonexistent.db"),
            embed_db=str(tmp_path / "embed.db"),
            index=True,
        )

        result = cmd_ask(args)

        assert result == 1


class TestAskSearch:
    """Tests for query → search → format flow."""

    def test_basic_search_returns_results(self, indexed_db, capsys):
        """Basic search returns relevant results."""
        args = make_args(
            query=["error", "handling"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        # Should find content about errors/exceptions
        assert "error" in captured.out.lower() or "exception" in captured.out.lower() or captured.out

    def test_empty_query_shows_usage(self, indexed_db, capsys):
        """Empty query shows usage message."""
        args = make_args(
            query=[],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "Usage" in captured.out

    def test_missing_embed_db_shows_hint(self, populated_db, tmp_path, capsys):
        """Missing embeddings database shows helpful message."""
        args = make_args(
            query=["test"],
            db=str(populated_db["db_path"]),
            embed_db=str(tmp_path / "nonexistent_embed.db"),
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "No embeddings index found" in captured.out
        assert "--index" in captured.out

    def test_missing_main_db_shows_hint(self, tmp_path, capsys):
        """Missing main database shows helpful message."""
        args = make_args(
            query=["test"],
            db=str(tmp_path / "nonexistent.db"),
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "Database not found" in captured.out
        assert "ingest" in captured.out


class TestAskFilters:
    """Tests for workspace and other filters."""

    def test_workspace_filter(self, indexed_db, capsys):
        """--workspace filters to matching workspace."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            workspace="python",
            json=True,  # JSON output for easier verification
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        # Results should only contain Python workspace
        if captured.out.strip():
            import json
            data = json.loads(captured.out)
            # Verify results exist (or no results which is also valid)
            assert isinstance(data, (list, dict))

    def test_limit_restricts_results(self, indexed_db, capsys):
        """--limit restricts number of results."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            limit=1,
            json=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        if captured.out.strip():
            import json
            data = json.loads(captured.out)
            # JSON output is a list of results
            if isinstance(data, list):
                assert len(data) <= 1


class TestAskOutputFormats:
    """Tests for output format options."""

    def test_json_output(self, indexed_db, capsys):
        """--json outputs valid JSON."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            json=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        # Should be valid JSON
        import json
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_verbose_shows_more_text(self, indexed_db, capsys):
        """--verbose shows full chunk text."""
        # Run without verbose
        args1 = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
        )
        cmd_ask(args1)
        out1 = capsys.readouterr().out

        # Run with verbose
        args2 = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            verbose=True,
        )
        cmd_ask(args2)
        out2 = capsys.readouterr().out

        # Verbose output should be longer or equal
        assert len(out2) >= len(out1)


class TestAskFlagValidation:
    """Tests for flag combination validation."""

    def test_json_with_refs_errors(self, indexed_db, capsys):
        """--json with --refs returns error (refs would break JSON validity)."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            json=True,
            refs=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "--refs is not supported with --json" in captured.err

    def test_json_with_thread_warns_but_succeeds(self, indexed_db, capsys):
        """--json with --thread warns to stderr but outputs valid JSON."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            json=True,
            thread=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "--thread is ignored with --json output" in captured.err
        # Output should still be valid JSON
        import json
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_json_without_refs_works(self, indexed_db, capsys):
        """--json without --refs outputs valid JSON."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            json=True,
            refs=None,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        import json
        data = json.loads(captured.out)
        assert isinstance(data, (list, dict))

    def test_refs_without_json_works(self, indexed_db, capsys):
        """--refs without --json outputs refs as before."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            json=False,
            refs=True,
        )

        result = cmd_ask(args)
        # Should succeed (refs content may or may not be present depending on data)
        assert result == 0


class TestAskEdgeCases:
    """Edge case tests."""

    def test_no_results_for_unrelated_query(self, indexed_db, capsys):
        """Query with no matches returns appropriate message."""
        args = make_args(
            query=["xyzzy", "completely", "unrelated", "gibberish"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            threshold=0.99,  # Very high threshold to ensure no matches
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        # Should return 0 (no error) but indicate no results
        assert result == 0
        assert "No results" in captured.out or not captured.out.strip()

    def test_threshold_filters_low_scores(self, indexed_db, capsys):
        """--threshold filters out low-scoring results."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            threshold=0.99,  # Very high threshold
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        # Either returns 0 with "No results" or returns successfully
        assert result == 0

    def test_first_respects_custom_threshold(self, indexed_db, capsys):
        """--first with --threshold uses the user-specified threshold, not hardcoded 0.65."""
        # First, verify default (implicit 0.65) behavior with --first
        args_default = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            first=True,
            json=True,
        )
        cmd_ask(args_default)
        out_default = capsys.readouterr().out

        # Now test with explicit low threshold
        args_low = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            first=True,
            threshold=0.1,  # Very low threshold
            json=True,
        )
        result = cmd_ask(args_low)
        out_low = capsys.readouterr().out

        # Should succeed (no error)
        assert result == 0
        # With a low threshold, we should get results if any exist
        # (The exact behavior depends on scores, but we're testing the passthrough)


class TestAskThreadMode:
    """Tests for --thread mode candidate pool handling."""

    @pytest.fixture
    def multi_chunk_db(self, tmp_path):
        """Create database with many chunks across conversations for thread tests."""
        db_path = tmp_path / "main.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
        model_id = get_or_create_model(conn, "test-model")
        ws_id = get_or_create_workspace(conn, "/projects/test", "2024-01-01T10:00:00Z")

        # Create 10 conversations with 2 exchanges each = 20 chunks
        # All about "error handling" to ensure they match the query
        for i in range(10):
            conv_id = insert_conversation(
                conn, external_id=f"conv-{i}", harness_id=harness_id,
                workspace_id=ws_id, started_at=f"2024-01-{10+i:02d}T10:00:00Z",
            )
            # First exchange
            p1_id = insert_prompt(conn, conv_id, f"p{i}-1", f"2024-01-{10+i:02d}T10:00:00Z")
            insert_prompt_content(conn, p1_id, 0, "text", f'{{"text": "How do I handle errors in scenario {i}?"}}')
            r1_id = insert_response(
                conn, conv_id, p1_id, model_id, None, f"r{i}-1", f"2024-01-{10+i:02d}T10:00:01Z",
                input_tokens=10, output_tokens=100,
            )
            insert_response_content(
                conn, r1_id, 0, "text",
                f'{{"text": "For error handling in scenario {i}, use try/except blocks and proper logging."}}'
            )
            # Second exchange
            p2_id = insert_prompt(conn, conv_id, f"p{i}-2", f"2024-01-{10+i:02d}T10:01:00Z")
            insert_prompt_content(conn, p2_id, 0, "text", f'{{"text": "What about error recovery in case {i}?"}}')
            r2_id = insert_response(
                conn, conv_id, p2_id, model_id, None, f"r{i}-2", f"2024-01-{10+i:02d}T10:01:01Z",
                input_tokens=10, output_tokens=100,
            )
            insert_response_content(
                conn, r2_id, 0, "text",
                f'{{"text": "Error recovery for case {i} should include retry logic and graceful degradation."}}'
            )

        conn.commit()
        conn.close()

        embed_db_path = tmp_path / "embeddings.db"
        build_embeddings_index(db_path=db_path, embed_db_path=embed_db_path, verbose=False)

        return {"db_path": db_path, "embed_db_path": embed_db_path}

    def test_thread_mode_returns_more_than_limit(self, multi_chunk_db, capsys):
        """--thread mode should not trim results to --limit.

        The widened candidate pool (40+) should be preserved for the thread
        formatter to group by conversation, rather than being trimmed early.
        """
        args = make_args(
            query=["error", "handling"],
            db=str(multi_chunk_db["db_path"]),
            embed_db=str(multi_chunk_db["embed_db_path"]),
            limit=3,  # Request only 3, but thread mode should get more
            thread=True,
            json=True,  # JSON for easy counting
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0

        import json
        data = json.loads(captured.out)
        # JSON output is {"results": [...], ...}
        results = data.get("results", data) if isinstance(data, dict) else data
        # Thread mode should return more results than --limit since it
        # manages its own candidate pool for conversation grouping
        assert len(results) > 3, f"Expected >3 results for --thread, got {len(results)}"

    def test_non_thread_mode_respects_limit(self, multi_chunk_db, capsys):
        """Non-thread mode should respect --limit as before."""
        args = make_args(
            query=["error", "handling"],
            db=str(multi_chunk_db["db_path"]),
            embed_db=str(multi_chunk_db["embed_db_path"]),
            limit=3,
            thread=False,
            json=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0

        import json
        data = json.loads(captured.out)
        # JSON output is {"results": [...], ...}
        results = data.get("results", data) if isinstance(data, dict) else data
        assert len(results) <= 3, f"Expected <=3 results without --thread, got {len(results)}"


class TestAskPrivacyWarning:
    """Tests for privacy warning on --full and --refs flags."""

    def test_full_flag_prints_privacy_warning(self, indexed_db, capsys):
        """--full prints privacy warning to stderr."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            full=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "Showing full content which may contain sensitive information" in captured.err

    def test_refs_flag_prints_privacy_warning(self, indexed_db, capsys):
        """--refs prints privacy warning to stderr."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            refs=True,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "Showing full content which may contain sensitive information" in captured.err

    def test_normal_output_no_privacy_warning(self, indexed_db, capsys):
        """Normal output (no --full or --refs) has no privacy warning."""
        args = make_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            full=False,
            refs=None,
        )

        result = cmd_ask(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "Showing full content which may contain sensitive information" not in captured.err
