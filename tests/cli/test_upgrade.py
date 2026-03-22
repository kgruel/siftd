"""Tests for siftd upgrade command and version check."""

import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

from siftd.cli.upgrade import (
    _cache_path,
    _fetch_latest_version,
    _is_newer,
    _read_cache,
    _write_cache,
    _cache_is_fresh,
    maybe_print_notice,
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
