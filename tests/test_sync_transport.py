"""Tests for SSH transport in siftd.api.sync using FakeSSH."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from siftd.api.sync import SyncError, _build_ssh_options, _pull_ssh, _push_ssh
from siftd.domain.sync import SyncRemote
from fakes.ssh import FakeSSH, FakeSSHResult


def _remote(path="/r/db", name="t", host="box", **kw):
    return SyncRemote(name=name, path=path, host=host, **kw)


def _run(coro):
    """Run an async function synchronously for testing."""
    return asyncio.run(coro)


def _patch_connect(fake: FakeSSH):
    """Patch asyncssh.connect to return a FakeSSH instance.

    asyncssh.connect returns an _ACMWrapper (supports both await and
    async with). FakeSSH already implements __aenter__/__aexit__, so
    returning it directly from a sync callable satisfies ``async with``.
    """
    def mock_connect(host, **kwargs):
        fake.connect_kwargs = kwargs
        return fake

    return patch("siftd.api.sync.asyncssh.connect", side_effect=mock_connect)


class TestPushSSHSuccess:
    def test_sends_data_and_parses_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        fake = FakeSSH.push_success()
        slice_path = tmp_path / "slice.db"
        slice_data = b"SQLite format 3\x00test"
        slice_path.write_bytes(slice_data)
        remote = _remote()

        with _patch_connect(fake):
            existed = _run(_push_ssh(remote, slice_path))

        assert existed is True  # status "ok" != "created"
        assert len(fake.commands_run) == 1
        assert "siftd" in fake.commands_run[0]
        assert "db receive" in fake.commands_run[0]
        assert fake.inputs_received[0] == slice_data

    def test_new_remote_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        fake = FakeSSH({"siftd": FakeSSHResult(stdout='{"status":"created"}')})
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with _patch_connect(fake):
            existed = _run(_push_ssh(_remote(), slice_path))

        assert existed is False


class TestPushSSHConnectionError:
    def test_connection_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with patch("siftd.api.sync.asyncssh.connect", side_effect=OSError("Connection refused")):
            with pytest.raises(SyncError, match="running"):
                _run(_push_ssh(_remote(), slice_path))


class TestPushSSHCommandFailure:
    def test_nonzero_exit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        fake = FakeSSH({"siftd": FakeSSHResult(
            stderr="database is locked",
            returncode=1,
        )})
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with _patch_connect(fake):
            with pytest.raises(SyncError, match="database is locked"):
                _run(_push_ssh(_remote(), slice_path))


class TestPushSSHTimeout:
    def test_connect_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with patch("siftd.api.sync.asyncssh.connect", side_effect=TimeoutError()):
            with pytest.raises(SyncError, match="timed out"):
                _run(_push_ssh(_remote(), slice_path))


class TestPullSSHSuccess:
    def test_receives_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        pull_data = b"SQLite format 3\x00remote-data"
        fake = FakeSSH.pull_success(pull_data)

        with _patch_connect(fake):
            convos, size = _run(_pull_ssh(
                _remote(), tmp_path / "local.db", None, None, dry_run=True,
            ))

        assert convos == 1
        assert size == len(pull_data)
        assert len(fake.commands_run) == 1
        assert "db send" in fake.commands_run[0]

    def test_since_and_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        fake = FakeSSH.pull_success(b"data")

        with _patch_connect(fake):
            _run(_pull_ssh(
                _remote(), tmp_path / "local.db",
                since="2024-01", workspace="proj", dry_run=True,
            ))

        cmd = fake.commands_run[0]
        assert "--since" in cmd
        assert "2024-01" in cmd
        assert "-w" in cmd
        assert "proj" in cmd


class TestPullSSHEmpty:
    def test_zero_conversations(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        def respond(cmd: str, **kwargs: Any) -> FakeSSHResult:
            return FakeSSHResult(stdout="", stderr='{"conversations":0}')

        fake = FakeSSH(response_func=respond)

        with _patch_connect(fake):
            convos, size = _run(_pull_ssh(
                _remote(), tmp_path / "local.db", None, None, dry_run=True,
            ))

        assert convos == 0
        assert size == 0


class TestPullSSHConnectionError:
    def test_connection_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        with patch("siftd.api.sync.asyncssh.connect", side_effect=OSError("Connection refused")):
            with pytest.raises(SyncError, match="running"):
                _run(_pull_ssh(
                    _remote(), tmp_path / "local.db", None, None, dry_run=True,
                ))


class TestBuildSSHOptions:
    def test_empty_config(self, monkeypatch):
        monkeypatch.setattr("siftd.config.get_ssh_connect_kwargs", lambda n: {})
        result = _build_ssh_options(_remote())
        assert result == {}

    def test_identity_file(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config.get_ssh_connect_kwargs",
            lambda n: {"client_keys": ["/home/user/.ssh/id_ed25519"]},
        )
        result = _build_ssh_options(_remote())
        assert result["client_keys"] == ["/home/user/.ssh/id_ed25519"]

    def test_username_and_port(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config.get_ssh_connect_kwargs",
            lambda n: {"username": "deploy", "port": 2222},
        )
        result = _build_ssh_options(_remote())
        assert result["username"] == "deploy"
        assert result["port"] == 2222

    def test_known_hosts_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config.get_ssh_connect_kwargs",
            lambda n: {"known_hosts": None},
        )
        result = _build_ssh_options(_remote())
        assert result["known_hosts"] is None

    def test_connect_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config.get_ssh_connect_kwargs",
            lambda n: {"connect_timeout": 30},
        )
        result = _build_ssh_options(_remote())
        assert result["connect_timeout"] == 30
