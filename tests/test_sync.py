"""Tests for siftd.api.sync — sync protocol utilities."""

import json

import pytest

from siftd.api.sync import (
    SyncError,
    _build_ssh_options,
    _friendly_os_error,
    _friendly_remote_error,
    _is_http_remote,
    _parse_send_metadata,
    _push_local,
    _resolve_since,
    sync_pull,
    sync_push,
)
from siftd.domain.sync import SyncRemote
from siftd.storage.sqlite import open_database


def _remote(path="/r/db", name="t", host=None, **kw):
    return SyncRemote(name=name, path=path, host=host, **kw)


def _db(tmp_path, name="l.db"):
    p = tmp_path / name
    open_database(p).close()
    return p


class TestIsHttpRemote:
    def test_local(self):
        assert not _is_http_remote(_remote())

    def test_http(self):
        assert _is_http_remote(_remote("http://x"))

    def test_https(self):
        assert _is_http_remote(_remote("https://x"))


class TestResolveSince:
    def test_explicit(self):
        assert _resolve_since("2024-01", False, _remote()) == "2024-01"

    def test_push_all(self):
        assert _resolve_since(None, True, _remote()) is None

    def test_last_push(self):
        assert _resolve_since(None, False, _remote(last_push="2024-06")) == "2024-06"

    def test_no_history(self):
        assert _resolve_since(None, False, _remote()) is None


class TestFriendlyOsError:
    def test_refused(self):
        assert "running" in _friendly_os_error("h", "Connection refused")

    def test_denied(self):
        assert "authentication" in _friendly_os_error("h", "Permission denied")

    def test_resolve(self):
        assert "resolve" in _friendly_os_error("h", "Could not resolve hostname")

    def test_name_unknown(self):
        assert "resolve" in _friendly_os_error("h", "Name or service not known")

    def test_generic(self):
        assert "SSH failed" in _friendly_os_error("h", "other")


class TestFriendlyRemoteError:
    def test_not_found(self):
        assert "not installed" in _friendly_remote_error("h", "/db", "command not found")

    def test_db_locked(self):
        assert "locked" in _friendly_remote_error("h", "/db",
                                                   json.dumps({"error_type": "database_locked", "error": "x"}))

    def test_json_err(self):
        assert "bad" in _friendly_remote_error("h", "/db", json.dumps({"error": "bad"}))

    def test_raw(self):
        assert "err" in _friendly_remote_error("h", "/db", "err\nmore")


class TestBuildSshOptions:
    def test_returns_list(self, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_options", lambda n: ["-o", "X=Y"])
        assert "-o" in _build_ssh_options(_remote())


class TestPushLocal:
    def test_first(self, tmp_path):
        s = tmp_path / "s.db"
        s.write_bytes(b"SQLite format 3")
        t = tmp_path / "r" / "db"
        assert not _push_local(_remote(path=str(t)), s, tmp_path / "l.db")
        assert t.exists()

    def test_merge(self, tmp_path):
        assert _push_local(
            _remote(path=str(_db(tmp_path, "r.db"))),
            _db(tmp_path, "s.db"), tmp_path / "l.db",
        )


class TestSyncPush:
    def test_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sync_push(tmp_path / "x.db", _remote())

    def test_empty(self, tmp_path):
        assert sync_push(_db(tmp_path), _remote(path=str(tmp_path / "r.db"))).conversations == 0

    def test_dry(self, tmp_path):
        assert sync_push(_db(tmp_path), _remote(path=str(tmp_path / "r.db")), dry_run=True).dry_run


class TestSyncPull:
    def test_missing(self, tmp_path):
        with pytest.raises(SyncError, match="not found"):
            sync_pull(_db(tmp_path), _remote(path=str(tmp_path / "x.db")))

    def test_empty(self, tmp_path):
        assert sync_pull(_db(tmp_path), _remote(path=str(_db(tmp_path, "r.db")))).conversations == 0


class TestParseSendMetadata:
    def test_json(self):
        assert _parse_send_metadata('{"n": 5}') == {"n": 5}

    def test_after_noise(self):
        assert _parse_send_metadata('warn\n{"ok": true}') == {"ok": True}

    def test_no_json(self):
        assert _parse_send_metadata("nope") == {}

    def test_empty(self):
        assert _parse_send_metadata("") == {}
