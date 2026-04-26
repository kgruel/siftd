"""Tests for siftd cli serve — cmd_serve startup logic and build_serve_parser."""

import argparse
import sys
import types
from types import ModuleType, SimpleNamespace

import pytest

from siftd.cli.serve import build_serve_parser, cmd_serve
from siftd.serve import require_serve


class TestBuildServeParser:
    def test_parser_registers_subcommand(self):
        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_serve_parser(subs)
        ns = parent.parse_args(["serve", "--no-auth"])
        assert ns.func is cmd_serve
        assert ns.no_auth is True

    def test_parser_host_and_port(self):
        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_serve_parser(subs)
        ns = parent.parse_args(["serve", "--host", "127.0.0.1", "--port", "9999"])
        assert ns.host == "127.0.0.1"
        assert ns.port == 9999

    def test_parser_defaults(self):
        parent = argparse.ArgumentParser()
        subs = parent.add_subparsers()
        build_serve_parser(subs)
        ns = parent.parse_args(["serve"])
        assert ns.no_auth is False
        assert ns.host is None
        assert ns.port is None


class TestCmdServe:
    def test_import_error_returns_1(self, monkeypatch, capsys):
        """When litestar is not available, cmd_serve prints an install hint and returns 1."""

        def bad_require():
            raise ImportError("siftd[serve] requires the [serve] extra.")

        monkeypatch.setattr("siftd.serve.require_serve", bad_require)

        args = SimpleNamespace(no_auth=True, host=None, port=None, db=None)
        rc = cmd_serve(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "serve" in err.lower()

    def _setup_serve_mocks(self, monkeypatch, tmp_path, captured):
        """Shared setup: mock require_serve, create_app, uvicorn, paths."""
        monkeypatch.setattr("siftd.serve.require_serve", lambda: None)

        # Fake siftd.serve.app module to avoid litestar import
        fake_app_mod = ModuleType("siftd.serve.app")

        def fake_create_app(*, db_path, auth_config, fts_rebuild):
            captured["db_path"] = db_path
            captured["auth_config"] = auth_config
            captured["fts_rebuild"] = fts_rebuild
            return "fake_app"

        fake_app_mod.create_app = fake_create_app
        monkeypatch.setitem(sys.modules, "siftd.serve.app", fake_app_mod)

        # Fake uvicorn module
        fake_uvicorn = ModuleType("uvicorn")
        fake_uvicorn.run = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        state_dir = tmp_path / "state"
        monkeypatch.setattr("siftd.paths.state_dir", lambda: state_dir)
        monkeypatch.setattr("siftd.paths.db_path", lambda: tmp_path / "default.db")

    def test_no_auth_flag_sets_auth_config_none(self, monkeypatch, tmp_path):
        """--no-auth should pass auth_config=None to create_app."""
        captured = {}
        self._setup_serve_mocks(monkeypatch, tmp_path, captured)

        args = SimpleNamespace(no_auth=True, host=None, port=None, db=None)
        rc = cmd_serve(args)
        assert rc == 0
        assert captured["auth_config"] is None

    def test_auth_config_loaded_when_no_auth_false(self, monkeypatch, tmp_path):
        """Without --no-auth, auth config is loaded from config table."""
        captured = {}
        self._setup_serve_mocks(monkeypatch, tmp_path, captured)
        monkeypatch.setattr("siftd.config.get_config_table", lambda k: {"issuer": "https://idp"})

        args = SimpleNamespace(no_auth=False, host=None, port=None, db=None)
        rc = cmd_serve(args)
        assert rc == 0
        assert captured["auth_config"] == {"issuer": "https://idp"}

    def test_explicit_db_path_used(self, monkeypatch, tmp_path):
        """Explicit --db argument takes precedence over config and defaults."""
        captured = {}
        self._setup_serve_mocks(monkeypatch, tmp_path, captured)

        from pathlib import Path

        explicit_db = tmp_path / "team.db"
        args = SimpleNamespace(no_auth=True, host=None, port=None, db=str(explicit_db))
        rc = cmd_serve(args)
        assert rc == 0
        assert captured["db_path"] == explicit_db

    def test_serve_state_file_written_and_cleaned(self, monkeypatch, tmp_path):
        """cmd_serve writes a serve.json state file and cleans it up."""
        captured = {}
        self._setup_serve_mocks(monkeypatch, tmp_path, captured)

        args = SimpleNamespace(no_auth=True, host=None, port=None, db=None)
        rc = cmd_serve(args)
        assert rc == 0
        # State file should be cleaned up in finally block
        state_dir = tmp_path / "state"
        assert not (state_dir / "serve.json").exists()

    def test_uvicorn_missing_returns_1(self, monkeypatch, capsys):
        """When uvicorn is absent, cmd_serve prints an install hint and returns 1."""
        monkeypatch.setitem(sys.modules, "litestar", types.ModuleType("litestar"))
        monkeypatch.setitem(sys.modules, "uvicorn", None)

        args = SimpleNamespace(no_auth=True, host=None, port=None, db=None)
        rc = cmd_serve(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "serve" in err.lower()
        assert "ModuleNotFoundError" not in err


class TestRequireServe:
    def test_litestar_missing_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "litestar", None)
        with pytest.raises(ImportError, match=r"\[serve\]"):
            require_serve()

    def test_uvicorn_missing_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "litestar", types.ModuleType("litestar"))
        monkeypatch.setitem(sys.modules, "uvicorn", None)
        with pytest.raises(ImportError, match=r"\[serve\]"):
            require_serve()

    def test_both_present_returns_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "litestar", types.ModuleType("litestar"))
        monkeypatch.setitem(sys.modules, "uvicorn", types.ModuleType("uvicorn"))
        assert require_serve() is None
