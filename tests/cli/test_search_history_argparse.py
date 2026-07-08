"""Argparse-layer + behavior tests for siftd search --history.

All parsing tests run through the actual argparse entry point
(build_search_parser), not _search(_args(...)) shortcuts, per the
cli-argparse-test-gap memory. Behavior tests invoke siftd.cli.main so the
whole parse→dispatch path is exercised.
"""

import argparse

import pytest

from siftd.cli.search import build_search_parser


def _make_parser():
    """Build a standalone parser that includes the search subcommand."""
    parser = argparse.ArgumentParser(prog="siftd")
    subparsers = parser.add_subparsers(dest="command")
    build_search_parser(subparsers)
    return parser


# ---------------------------------------------------------------------------
# Flag wiring
# ---------------------------------------------------------------------------


class TestHistoryFlagParsing:
    def test_history_bare_uses_default_count(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "--history"])
        assert args.history == 20
        assert args.query == []

    def test_history_with_count(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "--history", "5"])
        assert args.history == 5

    def test_history_equals_form(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "--history=7"])
        assert args.history == 7

    def test_history_absent_by_default(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "some", "query"])
        assert args.history is None
        assert args.query == ["some", "query"]

    def test_history_non_int_exits_2(self):
        parser = _make_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "--history", "not-a-number", "--json"])
        assert exc.value.code == 2


class TestHistoryMutualExclusion:
    def test_query_then_history_exits_2(self, capsys):
        parser = _make_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "foo", "--history"])
        assert exc.value.code == 2
        assert "not allowed with argument" in capsys.readouterr().err

    def test_history_with_owner_filter_allowed(self):
        """--history composes with the owner filter (scoping, not a query)."""
        parser = _make_parser()
        args = parser.parse_args(["search", "--history", "--owner", "alice"])
        assert args.history == 20
        assert args.owner == "alice"


# ---------------------------------------------------------------------------
# Behavior through siftd.cli.main (full argparse dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def logged_db(tmp_path):
    """A DB with two captured searches (one carrying a linked open)."""
    from siftd.storage.search_log import (
        SearchEventFingerprint,
        record_open,
        record_search,
    )
    from siftd.storage.sqlite import create_database

    db_path = tmp_path / "siftd.db"
    conn = create_database(db_path)
    sid = record_search(
        conn, query="auth flow", issuer="agent",
        fingerprint=SearchEventFingerprint(), executed_mode="hybrid",
        result_ids=["c1", "c2"], result_count=2, commit=True,
    )
    record_open(
        conn, search_event_id=sid, conversation_id="c1", rank=1,
        surface="cli-heuristic", commit=True,
    )
    record_search(
        conn, query="rollup bug", issuer="cli",
        fingerprint=SearchEventFingerprint(), executed_mode="fts",
        result_ids=[], result_count=0, commit=True,
    )
    record_search(
        conn, query="tenant only", issuer="web",
        fingerprint=SearchEventFingerprint(), executed_mode="hybrid",
        result_ids=["c9"], result_count=1, owner="alice", commit=True,
    )
    conn.close()
    return db_path


class TestHistoryBehavior:
    def _run(self, argv):
        from siftd.cli import main

        return main(argv)

    def test_lists_recent_searches_with_columns(self, logged_db, capsys):
        rc = self._run(["--db", str(logged_db), "search", "--history"])
        assert rc == 0
        out = capsys.readouterr().out
        # query text, executed mode, issuer, result count, opened marker
        assert "auth flow" in out
        assert "rollup bug" in out
        assert "hybrid" in out and "fts" in out
        assert "agent" in out and "cli" in out
        assert "opened" in out
        # owner-scoped: alice's search never leaks into the local bucket
        assert "tenant only" not in out

    def test_owner_scoping_matches_capture(self, logged_db, capsys):
        rc = self._run(["--db", str(logged_db), "search", "--history", "--owner", "alice"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "tenant only" in out
        assert "auth flow" not in out

    def test_limit_applies(self, logged_db, capsys):
        rc = self._run(["--db", str(logged_db), "search", "--history", "1", "--json"])
        assert rc == 0
        import json

        body = json.loads(capsys.readouterr().out)
        assert len(body["history"]) == 1
        # most recent first
        assert body["history"][0]["query"] == "rollup bug"

    def test_json_row_shape(self, logged_db, capsys):
        rc = self._run(["--db", str(logged_db), "search", "--history", "--json"])
        assert rc == 0
        import json

        rows = json.loads(capsys.readouterr().out)["history"]
        opened_by_query = {r["query"]: r["opened"] for r in rows}
        assert opened_by_query["auth flow"] is True
        assert opened_by_query["rollup bug"] is False
        for r in rows:
            assert {"id", "query", "issued_at", "issuer", "executed_mode",
                    "result_count", "opened"} <= set(r)

    def test_empty_history_is_informational(self, tmp_path, capsys):
        from siftd.storage.sqlite import create_database

        db_path = tmp_path / "empty.db"
        create_database(db_path).close()
        rc = self._run(["--db", str(db_path), "search", "--history"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "No search history yet" in err

    def test_empty_history_with_capture_disabled_names_config_key(
        self, tmp_path, capsys, monkeypatch
    ):
        from siftd.storage.sqlite import create_database

        db_path = tmp_path / "empty.db"
        create_database(db_path).close()
        monkeypatch.setattr("siftd.config.get_search_log_enabled", lambda: False)
        rc = self._run(["--db", str(db_path), "search", "--history"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "search.log" in err
        assert "disabled" in err
