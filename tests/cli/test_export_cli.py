"""Tests for siftd cli export — cmd_export and build_export_parser."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from siftd.cli.export import build_export_parser, cmd_export


def _make_args(**overrides):
    """Build a minimal args namespace for cmd_export."""
    defaults = {
        "db": None,
        "conversation_id": None,
        "last": None,
        "workspace": None,
        "tag": None,
        "no_tag": None,
        "since": None,
        "before": None,
        "search": None,
        "no_header": False,
        "thinking": False,
        "tools": False,
        "brief": False,
        "full": False,
        "chars": None,
        "json": False,
        "output": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestBuildExportParser:
    def test_parser_registers_subcommand(self):
        import argparse

        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_export_parser(subs)
        # Should parse without error
        ns = parent.parse_args(["export", "--last"])
        assert ns.func is cmd_export
        assert ns.last == 1

    def test_parser_last_with_count(self):
        import argparse

        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_export_parser(subs)
        ns = parent.parse_args(["export", "--last", "5"])
        assert ns.last == 5

    def test_parser_accepts_conversation_id(self):
        import argparse

        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_export_parser(subs)
        ns = parent.parse_args(["export", "01HX4G7K"])
        assert ns.conversation_id == "01HX4G7K"

    def test_parser_latest_alias_without_count(self):
        import argparse

        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_export_parser(subs)
        ns = parent.parse_args(["export", "--latest"])
        assert ns.last == 1

    def test_parser_latest_alias_with_count(self):
        import argparse

        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_export_parser(subs)
        ns = parent.parse_args(["export", "--latest", "5"])
        assert ns.last == 5


class TestCmdExport:
    def test_operation_construction_md(self, monkeypatch, tmp_path):
        """Verify Operation is built with correct params for markdown export."""
        captured = {}

        def fake_execute(op):
            captured["op"] = op
            return SimpleNamespace(count=1, content="# export", media_type="text/markdown", filename="out.md")

        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr("siftd.api.dispatch.execute", fake_execute)

        args = _make_args(last=2, db=str(tmp_path / "db.db"))
        rc = cmd_export(args)
        assert rc == 0

        op = captured["op"]
        assert op.path == "/api/v1/export"
        assert op.method == "GET"
        assert op.params["format"] == "md"
        assert op.params["last"] == 2
        assert op.render_method == "raw"

    def test_operation_construction_json(self, monkeypatch, tmp_path):
        """Verify --json flag sets format param."""
        captured = {}

        def fake_execute(op):
            captured["op"] = op
            return SimpleNamespace(count=1, content="{}", media_type="application/json", filename="out.json")

        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr("siftd.api.dispatch.execute", fake_execute)

        args = _make_args(json=True, db=str(tmp_path / "db.db"))
        rc = cmd_export(args)
        assert rc == 0
        assert captured["op"].params["format"] == "json"

    def test_default_last_when_no_id(self, monkeypatch, tmp_path):
        """When no conversation_id and no --last, defaults to last=1."""
        captured = {}

        def fake_execute(op):
            captured["op"] = op
            return SimpleNamespace(count=1, content="x", media_type="text/markdown", filename="out.md")

        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr("siftd.api.dispatch.execute", fake_execute)

        args = _make_args(db=str(tmp_path / "db.db"))
        cmd_export(args)
        assert captured["op"].params["last"] == 1

    def test_file_not_found_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: (_ for _ in ()).throw(FileNotFoundError("missing db")))

        rc = cmd_export(_make_args(db=str(tmp_path / "db.db")))
        assert rc == 1
        assert "missing db" in capsys.readouterr().out

    def test_sqlite_fts_error_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr(
            "siftd.api.dispatch.execute",
            lambda _op: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: conversations_fts")),
        )

        rc = cmd_export(_make_args(db=str(tmp_path / "db.db")))
        assert rc == 1
        assert "FTS index not found" in capsys.readouterr().err

    def test_sqlite_syntax_error_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr(
            "siftd.api.dispatch.execute",
            lambda _op: (_ for _ in ()).throw(sqlite3.OperationalError("fts5 syntax error")),
        )

        rc = cmd_export(_make_args(db=str(tmp_path / "db.db")))
        assert rc == 1
        assert "Invalid search query" in capsys.readouterr().err

    def test_generic_sqlite_error_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr(
            "siftd.api.dispatch.execute",
            lambda _op: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
        )

        rc = cmd_export(_make_args(db=str(tmp_path / "db.db")))
        assert rc == 1
        assert "Database error" in capsys.readouterr().err

    def test_zero_count_returns_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda _op: SimpleNamespace(count=0))

        rc = cmd_export(_make_args(db=str(tmp_path / "db.db")))
        assert rc == 1
        assert "No conversations found" in capsys.readouterr().out

    def test_output_to_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr(
            "siftd.api.dispatch.execute",
            lambda _op: SimpleNamespace(count=2, content="exported text", media_type="text/markdown", filename="out.md"),
        )

        outfile = tmp_path / "result.md"
        rc = cmd_export(_make_args(output=str(outfile), db=str(tmp_path / "db.db")))
        assert rc == 0
        assert outfile.read_text() == "exported text"
        assert "Exported 2 session(s)" in capsys.readouterr().out

    def test_full_flag_enables_tools_in_fidelity(self, monkeypatch, tmp_path):
        """--full should put 'tools' into the fidelity visible set."""
        captured = {}

        def fake_execute(op):
            captured["op"] = op
            return SimpleNamespace(count=1, content="x", media_type="text/markdown", filename="out.md")

        monkeypatch.setattr("siftd.cli.export.resolve_db", lambda _a: tmp_path / "db.db")
        monkeypatch.setattr("siftd.api.dispatch.execute", fake_execute)

        args = _make_args(full=True, db=str(tmp_path / "db.db"))
        cmd_export(args)
        assert captured["op"].params["fidelity"].shows("tools")
