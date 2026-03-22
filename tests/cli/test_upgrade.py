"""Tests for siftd upgrade command and version check."""

import json

from siftd.cli.upgrade import (
    _background_check,
    _cache_is_fresh,
    _fetch_latest_version,
    _is_newer,
    _read_cache,
    _upgrade_command,
    _write_cache,
    cmd_upgrade,
    maybe_print_notice,
    maybe_start_check,
)


class TestIsNewer:
    def test_newer(self):
        assert _is_newer("0.5.1", "0.5.0")

    def test_equal(self):
        assert not _is_newer("0.5.0", "0.5.0")

    def test_older(self):
        assert not _is_newer("0.4.9", "0.5.0")

    def test_major_bump(self):
        assert _is_newer("1.0.0", "0.9.9")

    def test_malformed_graceful(self):
        assert not _is_newer("bad", "0.5.0")


class TestCache:
    def test_write_and_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        _write_cache("0.6.0")
        cache = _read_cache()
        assert cache is not None
        assert cache["latest"] == "0.6.0"
        assert "checked_at" in cache

    def test_read_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        assert _read_cache() is None

    def test_read_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        (tmp_path / "update-check.json").write_text("not json")
        assert _read_cache() is None

    def test_fresh_within_interval(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        _write_cache("0.6.0")
        assert _cache_is_fresh()

    def test_stale_after_interval(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        path = tmp_path / "update-check.json"
        path.write_text(json.dumps({
            "latest": "0.6.0",
            "checked_at": "2020-01-01T00:00:00+00:00",
        }))
        assert not _cache_is_fresh()


class TestNotice:
    def test_no_notice_when_current(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        monkeypatch.setattr("siftd.cli.upgrade._get_version", lambda: "0.5.0")
        monkeypatch.setattr("siftd.cli.upgrade.sys.stderr.isatty", lambda: True)
        _write_cache("0.5.0")
        maybe_print_notice()
        assert capsys.readouterr().err == ""

    def test_notice_when_newer(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        monkeypatch.setattr("siftd.cli.upgrade._get_version", lambda: "0.5.0")
        monkeypatch.setattr("siftd.cli.upgrade.sys.stderr.isatty", lambda: True)
        _write_cache("0.6.0")
        maybe_print_notice()
        err = capsys.readouterr().err
        assert "0.6.0 available" in err
        assert "siftd upgrade" in err

    def test_no_notice_when_not_tty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        monkeypatch.setattr("siftd.cli.upgrade._get_version", lambda: "0.5.0")
        monkeypatch.setattr("siftd.cli.upgrade.sys.stderr.isatty", lambda: False)
        _write_cache("0.6.0")
        maybe_print_notice()
        assert capsys.readouterr().err == ""

    def test_no_notice_when_env_disabled(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("siftd.cli.upgrade.state_dir", lambda: tmp_path)
        monkeypatch.setenv("SIFTD_NO_UPDATE_CHECK", "1")
        monkeypatch.setattr("siftd.cli.upgrade.sys.stderr.isatty", lambda: True)
        _write_cache("0.6.0")
        maybe_print_notice()
        assert capsys.readouterr().err == ""


class TestUpgradeCommandAndChecks:
    def test_fetch_latest_version_and_background(self, monkeypatch):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self):
                return b'{"info":{"version":"9.9.9"}}'

        monkeypatch.setitem(__import__("sys").modules, "urllib.request", type("M", (), {"urlopen": lambda *a, **k: _Resp()})())
        assert _fetch_latest_version() == "9.9.9"

        called = []
        monkeypatch.setattr("siftd.cli.upgrade._fetch_latest_version", lambda: "1.2.3")
        monkeypatch.setattr("siftd.cli.upgrade._write_cache", lambda latest: called.append(latest))
        _background_check()
        assert called == ["1.2.3"]

    def test_maybe_start_check_branches(self, monkeypatch):
        monkeypatch.setenv("SIFTD_NO_UPDATE_CHECK", "1")
        maybe_start_check()
        monkeypatch.delenv("SIFTD_NO_UPDATE_CHECK")

        monkeypatch.setattr("siftd.config.get_config", lambda key: "false")
        maybe_start_check()

        monkeypatch.setattr("siftd.config.get_config", lambda key: "true")
        monkeypatch.setattr("siftd.cli.upgrade._cache_is_fresh", lambda: True)
        maybe_start_check()

        started = []

        class _T:
            def __init__(self, target, daemon):
                started.append((target, daemon))

            def start(self):
                started.append("started")

        monkeypatch.setattr("siftd.cli.upgrade._cache_is_fresh", lambda: False)
        monkeypatch.setattr("siftd.cli.upgrade.threading.Thread", _T)
        maybe_start_check()
        assert started and "started" in started

    def test_upgrade_command_builder(self, monkeypatch):
        monkeypatch.setattr("siftd.cli.upgrade.shutil.which", lambda _: None)
        assert _upgrade_command("pip_venv")[0] == "pip"
        monkeypatch.setattr("siftd.cli.upgrade.shutil.which", lambda _: "/bin/uv")
        assert _upgrade_command("pip_user")[0] == "uv"
        assert _upgrade_command("unknown") is None

    def test_cmd_upgrade_paths(self, monkeypatch, capsys):
        monkeypatch.setattr("siftd.cli.upgrade._get_version", lambda: "0.5.0")
        monkeypatch.setattr("siftd.cli.upgrade.detect_install_method", lambda: "pip_venv")

        monkeypatch.setattr("siftd.cli.upgrade._fetch_latest_version", lambda: None)
        assert cmd_upgrade(type("A", (), {"check": False})()) == 1

        monkeypatch.setattr("siftd.cli.upgrade._fetch_latest_version", lambda: "0.5.0")
        monkeypatch.setattr("siftd.cli.upgrade._write_cache", lambda latest: None)
        assert cmd_upgrade(type("A", (), {"check": False})()) == 0

        monkeypatch.setattr("siftd.cli.upgrade._fetch_latest_version", lambda: "0.6.0")
        assert cmd_upgrade(type("A", (), {"check": True})()) == 0

        monkeypatch.setattr("siftd.cli.upgrade.detect_install_method", lambda: "editable")
        assert cmd_upgrade(type("A", (), {"check": False})()) == 0

        monkeypatch.setattr("siftd.cli.upgrade.detect_install_method", lambda: "unknown")
        assert cmd_upgrade(type("A", (), {"check": False})()) == 1

        monkeypatch.setattr("siftd.cli.upgrade.detect_install_method", lambda: "brew")

        class _R:
            returncode = 0

        calls = []
        monkeypatch.setattr("siftd.cli.upgrade.subprocess.run", lambda cmd, **k: (calls.append(cmd), _R())[1])
        seen = []
        monkeypatch.setattr("siftd.cli.upgrade._write_cache", lambda latest: seen.append(latest))
        assert cmd_upgrade(type("A", (), {"check": False})()) == 0
        assert ["brew", "update"] in calls
        assert "0.5.0" in seen

        monkeypatch.setattr("siftd.cli.upgrade.detect_install_method", lambda: "pipx")
        monkeypatch.setattr("siftd.cli.upgrade.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        assert cmd_upgrade(type("A", (), {"check": False})()) == 1
