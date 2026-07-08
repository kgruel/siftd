"""CLI smoke tests — verify commands parse and run without import errors."""

import sys

import pytest
from conftest import FIXTURES_DIR

from siftd.cli import _relax_output_encoding, main


def test_help_exits_zero():
    """siftd --help exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_db_stats_with_db(test_db):
    """siftd --db <path> db stats runs successfully."""
    rc = main(["--db", str(test_db), "db", "stats"])
    assert rc == 0


def test_query_with_db(test_db):
    """siftd --db <path> query lists conversations."""
    rc = main(["--db", str(test_db), "query"])
    assert rc == 0


def test_main_installs_ascii_icons_on_nonunicode_stream(test_db, monkeypatch):
    """main() drives the icon lever: when stdout can't render Unicode (a pipe or a
    LANG=C TTY) the ambient IconSet becomes ASCII process-wide, so every glyph
    consumer degrades from one control point."""
    from painted import ASCII_ICONS, current_icons

    monkeypatch.setattr("siftd.output.common.prefers_ascii", lambda *a, **k: True)
    main(["--db", str(test_db), "query"])
    assert current_icons() is ASCII_ICONS


def test_main_keeps_unicode_icons_on_capable_tty(test_db, monkeypatch):
    """On a Unicode-capable stream the lever doesn't fire — the ambient set stays
    the default Unicode IconSet that use_theme installed (the rail keeps ◆/│/·)."""
    from painted import IconSet, current_icons

    monkeypatch.setattr("siftd.output.common.prefers_ascii", lambda *a, **k: False)
    main(["--db", str(test_db), "query"])
    assert current_icons() == IconSet()


def test_unknown_subcommand():
    """Unknown subcommand prints help and exits non-zero."""
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-command"])
    assert exc_info.value.code != 0


def test_tag_bulk_apply(test_db, capsys):
    """siftd tag <id> tag1 tag2 tag3 applies all tags in one call."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", conv_id, "alpha", "beta", "gamma"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Applied tag 'alpha'" in captured.out
    assert "Applied tag 'beta'" in captured.out
    assert "Applied tag 'gamma'" in captured.out

    # Verify all three tags are persisted
    conn = open_database(test_db)
    tags = conn.execute(
        """SELECT t.name FROM tag_assignments ta
           JOIN tags t ON t.id = ta.tag_id
           WHERE ta.target_kind = 'conversation' AND ta.target_id = ?
           ORDER BY t.name""",
        (conv_id,),
    ).fetchall()
    conn.close()
    assert [r["name"] for r in tags] == ["alpha", "beta", "gamma"]


def test_tag_bulk_remove(test_db, capsys):
    """siftd tag --remove <id> tag1 tag2 removes multiple tags."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    # Apply first
    main(["--db", str(test_db), "tag", conv_id, "alpha", "beta", "gamma"])
    # Remove two
    rc = main(["--db", str(test_db), "tag", "--remove", conv_id, "alpha", "gamma"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Removed tag 'alpha'" in captured.out
    assert "Removed tag 'gamma'" in captured.out

    # Only beta should remain
    conn = open_database(test_db)
    tags = conn.execute(
        """SELECT t.name FROM tag_assignments ta
           JOIN tags t ON t.id = ta.tag_id
           WHERE ta.target_kind = 'conversation' AND ta.target_id = ?""",
        (conv_id,),
    ).fetchall()
    conn.close()
    assert [r["name"] for r in tags] == ["beta"]


def test_tag_colon_path_prompt(test_db, capsys):
    """siftd tag <conv>:prompt:1 <tag> tags the first prompt event."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations ORDER BY started_at LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", f"{conv_id}:prompt:1", "colon:prompt-tag"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Applied tag 'colon:prompt-tag'" in captured.out

    conn = open_database(test_db)
    row = conn.execute(
        "SELECT ta.target_kind FROM tag_assignments ta "
        "JOIN tags t ON t.id=ta.tag_id WHERE t.name='colon:prompt-tag'"
    ).fetchone()
    conn.close()
    assert row is not None and row["target_kind"] == "prompt"


def test_tag_colon_path_exchange(test_db, capsys):
    """siftd tag <conv>:exchange:1 <tag> tags with target_kind='exchange'."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations ORDER BY started_at LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", f"{conv_id}:exchange:1", "colon:exchange-tag"])
    assert rc == 0

    conn = open_database(test_db)
    row = conn.execute(
        "SELECT ta.target_kind FROM tag_assignments ta "
        "JOIN tags t ON t.id=ta.tag_id WHERE t.name='colon:exchange-tag'"
    ).fetchone()
    conn.close()
    assert row is not None and row["target_kind"] == "exchange"


def test_tag_colon_path_invalid_kind(test_db, capsys):
    """siftd tag <conv>:badkind:1 <tag> prints error and exits 1."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", f"{conv_id}:badkind:1", "some-tag"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Invalid target kind" in captured.out or "Invalid target kind" in captured.err


def test_tag_colon_path_out_of_range(test_db, capsys):
    """siftd tag <conv>:prompt:999 <tag> prints error (out of range) and exits 1."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", f"{conv_id}:prompt:999", "some-tag"])
    assert rc == 1


class TestIngestCommand:
    """Smoke tests for siftd ingest command."""

    def test_ingest_creates_db(self, tmp_path, capsys, monkeypatch):
        """siftd ingest creates database if it doesn't exist."""
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        db_path = tmp_path / "new.db"
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        rc = main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        assert rc == 0
        assert db_path.exists()
        captured = capsys.readouterr()
        assert "Creating database" in captured.out

    def test_ingest_with_existing_db(self, test_db, capsys, monkeypatch):
        """siftd ingest works with existing database."""
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        rc = main([
            "--db", str(test_db),
            "ingest",
            "--adapter", "claude_code",
            "--path", "/nonexistent/path",  # No files, but should still run
        ])

        assert rc == 0
        captured = capsys.readouterr()
        assert "Using database" in captured.out

    def test_ingest_verbose_flag(self, tmp_path, capsys):
        """siftd ingest --verbose shows skipped files."""
        db_path = tmp_path / "test.db"

        # First ingest
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        # Second ingest with verbose - should show skipped
        rc = main([
            "--db", str(db_path),
            "ingest",
            "--verbose",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        assert rc == 0
        captured = capsys.readouterr()
        # An all-skipped run is an empty-state: the verbose skip-reason breakdown
        # rides the status.info "all up to date" detail (ℹ, stderr).
        assert "unchanged" in captured.err

    def test_ingest_unknown_adapter(self, tmp_path, capsys):
        """siftd ingest with unknown adapter returns error."""
        db_path = tmp_path / "test.db"

        rc = main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "nonexistent_adapter",
        ])

        assert rc == 1
        captured = capsys.readouterr()
        assert "No adapters matched" in captured.err


class TestBackfillCommand:
    """Smoke tests for siftd backfill command."""

    def test_backfill_derivative_tags(self, test_db, capsys):
        """siftd backfill --derivative-tags runs successfully."""
        rc = main(["--db", str(test_db), "backfill", "--derivative-tags"])

        assert rc == 0
        captured = capsys.readouterr()
        # Should indicate completion (may find 0 or more)
        assert "derivative" in captured.out.lower() or "tagged" in captured.out.lower() or "No" in captured.out

    def test_backfill_shell_tags(self, test_db_with_tool_tags, capsys):
        """siftd backfill --shell-tags runs on database with tool calls."""
        rc = main(["--db", str(test_db_with_tool_tags), "backfill", "--shell-tags"])

        assert rc == 0
        # Should complete without error

    def test_backfill_missing_db(self, tmp_path, capsys):
        """siftd backfill with missing database returns error."""
        rc = main(["--db", str(tmp_path / "missing.db"), "backfill", "--derivative-tags"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Database" in captured.err


class TestQuerySqlRemoved:
    """`query sql` was removed outright (C3, docs/dev/cli-verb-coherence-2026-07-07.md).

    It now exits 2 with a hint to `siftd report`, same treatment as the
    `query <id>` detail-view removal. `report`'s own behavior (list/run/vars)
    is covered by tests/cli/test_report.py.
    """

    def test_query_sql_exits_2_with_report_hint(self, test_db, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--db", str(test_db), "query", "sql"])

        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "unrecognized arguments" in captured.err
        assert "siftd report" in captured.err

    def test_query_sql_with_name_exits_2_with_report_hint(self, test_db, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--db", str(test_db), "query", "sql", "count"])

        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "siftd report" in captured.err


class TestAdaptersCommand:
    """Tests for siftd adapters command."""

    def test_adapters_json(self, capsys):
        """siftd adapters --json outputs JSON array of adapter info."""
        import json

        rc = main(["adapters", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        # Should have at least built-in adapters
        assert len(data) > 0
        # Each entry should have name, origin, locations
        for item in data:
            assert "name" in item
            assert "origin" in item
            assert "locations" in item

    def test_adapters_json_includes_builtin(self, capsys):
        """siftd adapters --json includes built-in adapters."""
        import json

        rc = main(["adapters", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        names = [a["name"] for a in data]
        # Should include at least claude_code
        assert "claude_code" in names


class TestFTS5ErrorHandling:
    """Tests for FTS5 query syntax error handling."""

    def test_query_no_fts_table_gives_helpful_error(self, test_db, capsys):
        """siftd export -s on DB without FTS table gives 'run ingest' hint."""
        # Drop the FTS table from the existing test database
        import sqlite3
        conn = sqlite3.connect(test_db)
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.commit()
        conn.close()

        rc = main(["--db", str(test_db), "export", "-s", "test"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "FTS index not found" in captured.err
        assert "ingest" in captured.err.lower()

    def test_export_no_fts_table_gives_helpful_error(self, test_db, capsys):
        """siftd export -s on DB without FTS table gives 'run ingest' hint."""
        # Drop the FTS table from the existing test database
        import sqlite3
        conn = sqlite3.connect(test_db)
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.commit()
        conn.close()

        rc = main(["--db", str(test_db), "export", "-s", "test"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "FTS index not found" in captured.err
        assert "ingest" in captured.err.lower()

    def test_export_malformed_fts5_incomplete_or(self, test_db, capsys):
        """siftd export -s 'foo OR' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "export", "-s", "foo OR"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err

    def test_export_malformed_fts5_incomplete_and(self, test_db, capsys):
        """siftd export -s 'incomplete AND' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "export", "-s", "incomplete AND"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err

    def test_export_valid_fts5_still_works(self, test_db):
        """siftd export -s with valid FTS5 query still works."""
        # Uses valid query; may find 0 results but shouldn't error
        rc = main(["--db", str(test_db), "export", "-s", "hello"])
        # rc could be 0 (found) or 1 (not found), but not a crash
        assert rc in (0, 1)


class TestRelaxOutputEncoding:
    """The entry-point hardening that keeps non-ASCII conversation content from
    crashing a strict-ASCII stream (LANG=C / PYTHONIOENCODING=ascii)."""

    @staticmethod
    def _ascii_stream():
        import io

        return io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")

    def test_degrades_non_ascii_content_instead_of_crashing(self, monkeypatch):
        """Pins the mechanism: a strict-ASCII stream raises on a conversation
        em-dash (the production crash); after relaxing, the same content becomes a
        visible, reversible escape and the write survives."""
        # Baseline — strict ASCII is exactly the crash the fix targets.
        before = self._ascii_stream()
        with pytest.raises(UnicodeEncodeError):
            before.write("em-dash —")
            before.flush()

        out, err = self._ascii_stream(), self._ascii_stream()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        _relax_output_encoding()

        # Both streams now degrade rather than raise; encoding stays ASCII.
        assert out.errors == "backslashreplace"
        assert err.errors == "backslashreplace"
        assert out.encoding == "ascii"

        out.write("em-dash —\n")
        out.flush()
        assert out.buffer.getvalue().decode("ascii") == "em-dash \\u2014\n"

    def test_tolerates_streams_that_lack_or_reject_reconfigure(self, monkeypatch):
        """Best-effort: pytest capture / odd redirects either have no reconfigure
        or reject it — the hardening step must never become its own crash."""
        import io

        class NoReconfigure(io.StringIO):
            pass  # StringIO has no reconfigure → getattr returns None, skipped

        class RejectsReconfigure(io.StringIO):
            def reconfigure(self, **kwargs):
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(sys, "stdout", NoReconfigure())
        monkeypatch.setattr(sys, "stderr", RejectsReconfigure())
        _relax_output_encoding()  # must return cleanly, no exception
