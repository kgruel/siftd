"""Tests for SSH transport in siftd.api.sync using FakeSSH."""

from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from fakes.ssh import FakeSSH, FakeSSHResult

from siftd.api.sync import SyncError, _build_ssh_options, _pull_ssh, _push_ssh
from siftd.cli import _build_parser
from siftd.domain.sync import SYNC_HEADER, SyncRemote


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
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
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
        assert fake.inputs_received[0] == SYNC_HEADER + slice_data

    def test_new_remote_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        fake = FakeSSH({"siftd": FakeSSHResult(stdout='{"status":"created"}')})
        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with _patch_connect(fake):
            existed = _run(_push_ssh(_remote(), slice_path))

        assert existed is False


class TestPushSSHConnectionError:
    def test_connection_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with patch("siftd.api.sync.asyncssh.connect", side_effect=OSError("Connection refused")):
            with pytest.raises(SyncError, match="running"):
                _run(_push_ssh(_remote(), slice_path))


class TestPushSSHCommandFailure:
    def test_nonzero_exit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
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
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        slice_path = tmp_path / "slice.db"
        slice_path.write_bytes(b"data")

        with patch("siftd.api.sync.asyncssh.connect", side_effect=TimeoutError()):
            with pytest.raises(SyncError, match="timed out"):
                _run(_push_ssh(_remote(), slice_path))


class TestPullSSHSuccess:
    def test_receives_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        pull_data = b"SQLite format 3\x00remote-data"
        fake = FakeSSH.pull_success(pull_data)

        with _patch_connect(fake):
            convos, size = _run(_pull_ssh(
                _remote(), tmp_path / "local.db", None, {}, dry_run=True,
            ))

        assert convos == 1
        assert size == len(pull_data)
        assert len(fake.commands_run) == 1
        assert "db send" in fake.commands_run[0]

    def test_since_and_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        fake = FakeSSH.pull_success(b"data")

        with _patch_connect(fake):
            _run(_pull_ssh(
                _remote(), tmp_path / "local.db",
                since="2024-01-15",
                filters={
                    "workspace": "proj",
                    "tag": ["public", "release"],
                    "no_tag": ["private", "draft"],
                    "owner": "alice",
                },
                dry_run=True,
            ))

        cmd = fake.commands_run[0]
        assert "--since" in cmd and "2024-01-15" in cmd
        assert "-w" in cmd and "proj" in cmd
        # Each tag/no_tag value gets its own --tag/--no-tag flag
        assert cmd.count("--tag") == 2
        assert "public" in cmd and "release" in cmd
        assert cmd.count("--no-tag") == 2
        assert "private" in cmd and "draft" in cmd
        assert "--owner" in cmd and "alice" in cmd


class TestPullSSHCursorRoundTrip:
    """The cursor sync persists has to survive the remote's own `--since` (#21).

    `_pull_ssh` shells out to `siftd db send --since <cursor>` on the far end,
    where argparse re-parses the value — a boundary the local read path never
    crosses, because there the cursor goes straight from config into SQL. A
    cursor the remote parser rejects aborts every default pull after the first.
    """

    def test_persisted_cursor_parses_on_the_remote(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        # The expression `pull()` hands to update_last_pull, verbatim.
        cursor = datetime.now(UTC).isoformat()

        fake = FakeSSH.pull_success(b"data")
        with _patch_connect(fake):
            _run(_pull_ssh(
                _remote(), tmp_path / "local.db", cursor, {}, dry_run=True,
            ))

        argv = shlex.split(fake.commands_run[0])
        sent = argv[argv.index("--since") + 1]

        # Parse through the real CLI, the way the remote shell would.
        args = _build_parser().parse_args(["db", "send", "--since", sent])
        assert args.since is not None


class TestPullSSHEmpty:
    def test_zero_conversations(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        def respond(cmd: str, **kwargs: Any) -> FakeSSHResult:
            return FakeSSHResult(stdout="", stderr='{"conversations":0}')

        fake = FakeSSH(response_func=respond)

        with _patch_connect(fake):
            convos, size = _run(_pull_ssh(
                _remote(), tmp_path / "local.db", None, {}, dry_run=True,
            ))

        assert convos == 0
        assert size == 0


class TestPullSSHConnectionError:
    def test_connection_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        monkeypatch.setattr("siftd.config.get_config", lambda k: None)

        with patch("siftd.api.sync.asyncssh.connect", side_effect=OSError("Connection refused")):
            with pytest.raises(SyncError, match="running"):
                _run(_pull_ssh(
                    _remote(), tmp_path / "local.db", None, {}, dry_run=True,
                ))


class TestBuildSSHOptions:
    def test_empty_config(self, monkeypatch):
        monkeypatch.setattr("siftd.config_sync.get_ssh_connect_kwargs", lambda n: {})
        hostname, opts = _build_ssh_options(_remote())
        assert hostname == "box"
        assert opts == {}

    def test_identity_file(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config_sync.get_ssh_connect_kwargs",
            lambda n: {"client_keys": ["/home/user/.ssh/id_ed25519"]},
        )
        _, opts = _build_ssh_options(_remote())
        assert opts["client_keys"] == ["/home/user/.ssh/id_ed25519"]

    def test_username_and_port(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config_sync.get_ssh_connect_kwargs",
            lambda n: {"username": "deploy", "port": 2222},
        )
        _, opts = _build_ssh_options(_remote())
        assert opts["username"] == "deploy"
        assert opts["port"] == 2222

    def test_known_hosts_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config_sync.get_ssh_connect_kwargs",
            lambda n: {"known_hosts": None},
        )
        _, opts = _build_ssh_options(_remote())
        assert opts["known_hosts"] is None

    def test_connect_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "siftd.config_sync.get_ssh_connect_kwargs",
            lambda n: {"connect_timeout": 30},
        )
        _, opts = _build_ssh_options(_remote())
        assert opts["connect_timeout"] == 30
