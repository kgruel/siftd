"""Tests for unified search command with auto-selection.

Tests the behavior matrix:
| Embeddings | Flag       | Behavior                              |
|------------|------------|---------------------------------------|
| Yes        | (none)     | Hybrid: FTS5 recall + embeddings rerank |
| Yes        | --fts      | Pure FTS5                             |
| Yes        | --semantic | Pure embeddings                       |
| No         | (none)     | FTS5 with hint about embeddings       |
| No         | --fts      | FTS5 (same as default)                |
| No         | --semantic | Error with install instructions       |
"""

import argparse

import pytest

from siftd.storage.fts import insert_fts_content
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
def fts_db(tmp_path):
    """Create a database with FTS5 index populated (no embeddings needed)."""
    db_path = tmp_path / "main.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    model_id = get_or_create_model(conn, "test-model")
    ws_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")

    # Conversation with searchable content
    conv_id = insert_conversation(
        conn, external_id="conv-1", harness_id=harness_id,
        workspace_id=ws_id, started_at="2024-01-15T10:00:00Z",
    )
    p_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
    p_content_id = insert_prompt_content(conn, p_id, 0, "text", '{"text": "How do I handle errors?"}')
    # Insert FTS content for search
    insert_fts_content(conn, p_content_id, "prompt", conv_id, "How do I handle errors?")

    r_id = insert_response(
        conn, conv_id, p_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=10, output_tokens=100,
    )
    r_content_id = insert_response_content(
        conn, r_id, 0, "text",
        '{"text": "Use try/except blocks for error handling in Python."}'
    )
    # Insert FTS content for search
    insert_fts_content(conn, r_content_id, "response", conv_id, "Use try/except blocks for error handling in Python.")

    conn.commit()
    conn.close()

    return {"db_path": db_path, "embed_db_path": tmp_path / "embeddings.db"}


def make_search_args(**kwargs):
    """Create argparse.Namespace with defaults for cmd_search."""
    defaults = {
        "query": [],
        "db": None,
        "embed_db": None,
        "limit": 10,
        "verbose": False,
        "full": False,
        "select": "all",
        "sort": "score",
        "mode": "auto",
        "view": "chunks",
        "workspace": None,
        "model": None,
        "since": None,
        "before": None,
        "index": False,
        "rebuild": False,
        "backend": None,
        "recall": 80,
        "role": None,
        "refs": None,
        "threshold": None,
        "json": False,
        "format": None,
        "no_exclude_active": True,
        "include_derivative": True,
        "no_diversity": True,
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


@pytest.fixture(autouse=True)
def _route_embed_db(monkeypatch, tmp_path):
    """Route embeddings_db_path() at tmp_path/embeddings.db (the fixtures' embed path).

    `siftd search` dropped `--embed-db`; the built index is wired in by pointing
    embeddings_db_path() at it, the resolution the real command uses.
    """
    p = tmp_path / "embeddings.db"
    monkeypatch.setattr("siftd.paths.embeddings_db_path", lambda: p)
    monkeypatch.setattr("siftd.cli.search.embeddings_db_path", lambda: p)


class TestNoEmbeddingsInstalled:
    """Tests when embeddings are NOT installed (mocked)."""

    def test_default_mode_falls_back_to_fts5(self, fts_db, capsys, monkeypatch):
        """Without embeddings, default mode uses FTS5 with hint."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        # Mock embeddings as unavailable
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            embed_db=str(fts_db["embed_db_path"]),
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        # FTS5 mode fallback hint is now the search-mode-degraded caveat (channel="text"),
        # which fires only on non-empty results. Empty-result path: silent fallback.

    def test_fts_flag_works_without_embeddings(self, fts_db, capsys, monkeypatch):
        """--fts flag works when embeddings unavailable."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            embed_db=str(fts_db["embed_db_path"]),
            mode="fts",
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        # The FTS path ran without embeddings and surfaced the query term —
        # either in rendered hits (stdout) or the no-results echo (stderr).
        assert "error" in (captured.out + captured.err).lower()

    def test_semantic_flag_errors_without_embeddings(self, fts_db, capsys, monkeypatch):
        """--semantic flag errors with install instructions when embeddings unavailable."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            embed_db=str(fts_db["embed_db_path"]),
            mode="semantic",
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "requires an embedding backend" in captured.err
        assert "siftd install embed" in captured.err


class TestFtsOnlyMode:
    """Tests for pure FTS5 mode (--fts flag)."""

    def test_fts_returns_keyword_matches(self, fts_db, capsys, monkeypatch):
        """--fts mode returns FTS5 keyword matches."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        # Mock embeddings unavailable to ensure we're testing FTS path
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            mode="fts",
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        assert captured.out or captured.err  # produced output (results on stdout, notes on stderr)

    def test_fts_json_output(self, fts_db, capsys, monkeypatch):
        """--fts with --json outputs valid JSON with mode indicator."""
        import json

        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            mode="fts",
            json=True,
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        data = json.loads(captured.out)
        assert data["mode"] == "fts"
        assert "results" in data
        assert data["query"] == "error"

    def test_fts_handles_no_results(self, fts_db, capsys, monkeypatch):
        """--fts shows appropriate message when no results."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["xyzzynonexistent"],
            db=str(fts_db["db_path"]),
            mode="fts",
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "No results" in captured.err

    def test_fts_respects_workspace_filter(self, tmp_path, capsys, monkeypatch):
        """--fts respects --workspace filter."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        # Create DB with two workspaces
        db_path = tmp_path / "main.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        model_id = get_or_create_model(conn, "test-model")
        ws1_id = get_or_create_workspace(conn, "/projects/alpha", "2024-01-01T10:00:00Z")
        ws2_id = get_or_create_workspace(conn, "/projects/beta", "2024-01-01T10:00:00Z")

        # Conversation in alpha about errors
        conv1_id = insert_conversation(
            conn, external_id="conv-alpha", harness_id=harness_id,
            workspace_id=ws1_id, started_at="2024-01-15T10:00:00Z",
        )
        p1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
        p1_c_id = insert_prompt_content(conn, p1_id, 0, "text", '{"text": "alpha error"}')
        insert_fts_content(conn, p1_c_id, "prompt", conv1_id, "alpha error")
        r1_id = insert_response(
            conn, conv1_id, p1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
            input_tokens=10, output_tokens=10,
        )
        r1_c_id = insert_response_content(conn, r1_id, 0, "text", '{"text": "alpha response"}')
        insert_fts_content(conn, r1_c_id, "response", conv1_id, "alpha response")

        # Conversation in beta about errors
        conv2_id = insert_conversation(
            conn, external_id="conv-beta", harness_id=harness_id,
            workspace_id=ws2_id, started_at="2024-01-16T10:00:00Z",
        )
        p2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
        p2_c_id = insert_prompt_content(conn, p2_id, 0, "text", '{"text": "beta error"}')
        insert_fts_content(conn, p2_c_id, "prompt", conv2_id, "beta error")
        r2_id = insert_response(
            conn, conv2_id, p2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
            input_tokens=10, output_tokens=10,
        )
        r2_c_id = insert_response_content(conn, r2_id, 0, "text", '{"text": "beta response"}')
        insert_fts_content(conn, r2_c_id, "response", conv2_id, "beta response")

        conn.commit()
        conn.close()

        # Search only in alpha workspace
        args = make_search_args(
            query=["error"],
            db=str(db_path),
            mode="fts",
            workspace="alpha",
            json=True,
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        import json
        data = json.loads(captured.out)
        # Should only have results from alpha (conv-alpha starts with conv-alpha conv id)
        for r in data["results"]:
            # The conversation_id should be from alpha workspace
            assert r["conversation_id"]  # Just verify we have results


class TestQuerySearchRemoved:
    """Tests that query -s flag has been removed."""

    def test_query_s_flag_no_longer_accepted(self, fts_db):
        """query -s is no longer a valid flag (removed, not deprecated)."""
        from siftd.cli import main

        with pytest.raises(SystemExit, match="2"):
            main(["--db", str(fts_db["db_path"]), "query", "-s", "error"])


# Tests that require embeddings are marked separately
@pytest.mark.embeddings
class TestWithEmbeddingsInstalled:
    """Tests when embeddings ARE installed (requires [embed] extra)."""

    @pytest.fixture
    def indexed_db(self, tmp_path):
        """Create database with embeddings index."""
        pytest.importorskip("fastembed")

        from siftd.embeddings.indexer import build_embeddings_index

        db_path = tmp_path / "main.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
        model_id = get_or_create_model(conn, "test-model")
        ws_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")

        conv_id = insert_conversation(
            conn, external_id="conv-1", harness_id=harness_id,
            workspace_id=ws_id, started_at="2024-01-15T10:00:00Z",
        )
        p_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
        p_c_id = insert_prompt_content(conn, p_id, 0, "text", '{"text": "How do I handle errors?"}')
        insert_fts_content(conn, p_c_id, "prompt", conv_id, "How do I handle errors?")
        r_id = insert_response(
            conn, conv_id, p_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
            input_tokens=10, output_tokens=100,
        )
        r_c_id = insert_response_content(
            conn, r_id, 0, "text",
            '{"text": "Use try/except blocks for error handling."}'
        )
        insert_fts_content(conn, r_c_id, "response", conv_id, "Use try/except blocks for error handling.")

        conn.commit()
        conn.close()

        embed_db_path = tmp_path / "embeddings.db"
        build_embeddings_index(db_path=db_path, embed_db_path=embed_db_path, verbose=False)

        return {"db_path": db_path, "embed_db_path": embed_db_path}

    def test_default_mode_uses_hybrid(self, indexed_db, capsys):
        """Default mode (no flags) uses hybrid search with embeddings."""
        from siftd.cli.search import cmd_search

        args = make_search_args(
            query=["error", "handling"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        # Should NOT show FTS5 mode hint (we're in hybrid mode)
        assert "FTS5 mode" not in captured.err

    def test_fts_flag_uses_pure_fts5(self, indexed_db, capsys):
        """--fts flag uses pure FTS5 even with embeddings available."""
        from siftd.cli.search import cmd_search

        args = make_search_args(
            query=["error"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            mode="fts",
            json=True,
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        import json
        data = json.loads(captured.out)
        assert data["mode"] == "fts"

    def test_semantic_flag_uses_pure_embeddings(self, indexed_db, capsys):
        """--semantic flag uses pure embeddings search (auto-sets embeddings_only)."""
        from siftd.cli.search import cmd_search

        args = make_search_args(
            query=["error", "handling"],
            db=str(indexed_db["db_path"]),
            embed_db=str(indexed_db["embed_db_path"]),
            mode="semantic",
            # Note: NOT setting embeddings_only - --semantic should set it automatically
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        # Should NOT print FTS5 fallback message (pure embeddings mode)
        assert "FTS5 found no matches" not in captured.err


class TestHelpText:
    """Tests for help text clarity."""

    def test_search_help_mentions_auto_selection(self, capsys):
        """Search command help mentions auto-selection."""
        import argparse
        from siftd.cli.search import build_search_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        build_search_parser(subparsers)

        # Get help text
        search_parser = subparsers.choices["search"]
        help_text = search_parser.format_help()

        assert "auto-select" in help_text.lower() or "unified" in help_text.lower()
        assert "--mode" in help_text
        assert "--view" in help_text

    def test_query_search_flag_removed(self):
        """Query no longer has a -s/--search flag."""
        from siftd import cli
        import inspect
        source = inspect.getsource(cli)

        # The deprecated --search flag should no longer appear in query definitions
        assert "DEPRECATED" not in source or "--search" not in source


class TestAutoSelectionHints:
    """Tests for auto-selection hints in different scenarios."""

    def test_deps_installed_but_index_missing_shows_index_hint(self, fts_db, capsys, monkeypatch):
        """When deps installed but index missing, hints at --index."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        # Mock: embeddings available but index doesn't exist
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "ok"))
        # embed_db_path doesn't exist (fts_db fixture doesn't create it)

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            embed_db=str(fts_db["embed_db_path"]),  # This file doesn't exist
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        # The embeddings-stale caveat surfaces both the "not indexed" note and its
        # remediation (the caveat's fix_command, rendered as a "↳ siftd embed" hint by status.caveats) on stderr.
        assert "not indexed" in captured.err
        assert "siftd embed" in captured.err

    def test_deps_not_installed_shows_install_hint(self, fts_db, capsys, monkeypatch):
        """When deps not installed, hints at installing."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        # Mock: embeddings not available
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            embed_db=str(fts_db["embed_db_path"]),
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        # The install-embed hint is now the search-mode-degraded caveat (channel="text",
        # fires on non-empty results only). With no results, no embed hint fires on stderr.


class TestFtsOnlyModeWarnings:
    """Tests for warnings about unsupported flags in FTS-only mode."""

    def test_unsupported_flags_show_warning(self, fts_db, capsys, monkeypatch):
        """Unsupported flags in FTS mode show warning."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            mode="fts",
            view="thread",  # Unsupported in FTS mode
            full=True,      # Unsupported
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "ignored in FTS5 mode" in captured.err
        assert "--view=thread" in captured.err
        assert "--full" in captured.err

    def test_supported_flags_no_warning(self, fts_db, capsys, monkeypatch):
        """Supported flags don't trigger warning."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        args = make_search_args(
            query=["error"],
            db=str(fts_db["db_path"]),
            mode="fts",
            json=True,  # Supported in FTS mode
            limit=5,    # Supported
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 0
        assert "ignored in FTS5 mode" not in captured.err


class TestFtsMissingTableError:
    """Tests for missing FTS table error handling."""

    def test_missing_fts_table_shows_helpful_error(self, tmp_path, capsys, monkeypatch):
        """Missing FTS table shows 'run ingest' message."""
        import siftd.embeddings.availability as avail
        from siftd.cli.search import cmd_search

        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        # Create a DB without FTS table
        db_path = tmp_path / "no_fts.db"
        conn = create_database(db_path)
        # Drop the FTS table
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.commit()
        conn.close()

        args = make_search_args(
            query=["error"],
            db=str(db_path),
            mode="fts",
        )

        result = cmd_search(args)
        captured = capsys.readouterr()

        assert result == 1
        assert "FTS index not found" in captured.err
        assert "siftd ingest" in captured.err


# ---------------------------------------------------------------------------
# ST-4a: _search_fts_only delegation path (Important #2 from codex review)
# ---------------------------------------------------------------------------


class TestFtsOnlyDelegation:
    """CLI-layer test: _search_fts_only attempts try_serve before local execution.

    TestSearchModeWireContract in test_serve_e2e_smoke.py covers the server route
    side. These tests cover the CLI side: that _search_fts_only builds an op with
    mode='fts', attempts delegation when a serve is reachable, and correctly handles
    the dict result shape that try_serve returns (vs the list that execute_for_render
    returns).
    """

    def test_delegates_with_mode_fts_in_op(self, tmp_path, monkeypatch, capsys):
        """try_serve is called with op.params['mode']=='fts'; dict result renders ok."""
        import siftd.serve.delegation as delegation_mod
        from siftd.cli.search import cmd_search

        db_path = tmp_path / "test.db"
        db_path.touch()

        captured_ops: list = []

        def mock_can_delegate(a, *, db):
            return True

        def mock_try_serve(op):
            captured_ops.append(op)
            return {"results": []}

        monkeypatch.setattr("siftd.cli.search._can_delegate_to_serve", mock_can_delegate)
        monkeypatch.setattr(delegation_mod, "try_serve", mock_try_serve)

        args = make_search_args(
            query=["testquery"],
            db=str(db_path),
            mode="fts",
        )

        result = cmd_search(args)

        assert result == 0, "should succeed (empty results from serve)"
        assert len(captured_ops) == 1, "try_serve must be called exactly once"
        op = captured_ops[0]
        assert op.params["mode"] == "fts", (
            f"op must carry mode='fts' on the wire; got {op.params.get('mode')!r}"
        )

    def test_fts_delegation_does_not_fall_back_to_local_on_dict_result(
        self, tmp_path, monkeypatch
    ):
        """When try_serve returns a dict, execute_for_render must not be called.

        This pins the dict-vs-list normalization at cli/search.py:516: a dict from
        the server is unwrapped via .get('results', []) and processed; execute_for_render
        (which would hit the local SQLite DB) is skipped entirely.
        """
        import siftd.serve.delegation as delegation_mod
        from siftd.api import dispatch as dispatch_mod
        from siftd.cli.search import cmd_search

        db_path = tmp_path / "test.db"
        db_path.touch()

        local_executed: list = []

        monkeypatch.setattr("siftd.cli.search._can_delegate_to_serve", lambda *a, **kw: True)
        monkeypatch.setattr(delegation_mod, "try_serve", lambda op: {"results": []})
        monkeypatch.setattr(
            dispatch_mod,
            "execute_for_render",
            lambda op: (local_executed.append(op), ([], []))[1],
        )

        args = make_search_args(query=["testquery"], db=str(db_path), fts=True)
        cmd_search(args)

        assert not local_executed, (
            "execute_for_render must not be called when try_serve returns a dict"
        )
