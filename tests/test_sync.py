"""Tests for siftd db push — push conversations to a remote database."""

import io
import json
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from siftd.api.sync import SyncError, SyncRemote, sync_pull, sync_push
from siftd.cli import main
from siftd.config import (
    get_ssh_options,
    get_sync_remote,
    get_sync_remotes,
    remove_sync_remote,
    set_remote_auth,
    set_sync_remote,
    update_last_pull,
    update_last_push,
)
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


def _make_db(path, *, conversations=None):
    """Helper to create a database with optional conversations."""
    conn = create_database(path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "test-model")
    provider_id = get_or_create_provider(conn, "test_provider")

    for conv in (conversations or []):
        started = conv.get("started_at", "2024-01-15T10:00:00Z")
        conv_id = insert_conversation(
            conn,
            external_id=conv["external_id"],
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at=started,
        )
        prompt_id = insert_prompt(conn, conv_id, f"p-{conv['external_id']}", started)
        insert_prompt_content(
            conn, prompt_id, 0, "text",
            f'{{"text": "{conv.get("prompt_text", "Hello")}"}}',
        )
        response_id = insert_response(
            conn, conv_id, prompt_id, model_id, provider_id,
            f"r-{conv['external_id']}", started,
            input_tokens=100, output_tokens=50,
        )
        insert_response_content(
            conn, response_id, 0, "text",
            f'{{"text": "{conv.get("response_text", "Hi there")}"}}',
        )

    conn.commit()
    conn.close()
    return path


# --- Config CRUD tests ---


class TestConfigRemotes:
    def test_add_and_get_remote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("alcove", "alcove", "/data/team.db")
        remote = get_sync_remote("alcove")

        assert remote is not None
        assert remote["name"] == "alcove"
        assert remote["host"] == "alcove"
        assert remote["path"] == "/data/team.db"
        assert remote["last_push"] is None

    def test_add_local_remote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("nas", None, "/mnt/nas/team.db")
        remote = get_sync_remote("nas")

        assert remote is not None
        assert remote["host"] is None
        assert remote["path"] == "/mnt/nas/team.db"

    def test_list_remotes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("alcove", "alcove", "/data/team.db")
        set_sync_remote("nas", None, "/mnt/nas/team.db")

        remotes = get_sync_remotes()
        names = {r["name"] for r in remotes}
        assert names == {"alcove", "nas"}

    def test_remove_remote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("alcove", "alcove", "/data/team.db")
        assert remove_sync_remote("alcove") is True
        assert get_sync_remote("alcove") is None

    def test_remove_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert remove_sync_remote("nope") is False

    def test_update_last_push(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("alcove", "alcove", "/data/team.db")
        update_last_push("alcove", "2026-02-11T10:30:00+00:00")

        remote = get_sync_remote("alcove")
        assert remote is not None
        assert remote["last_push"] == "2026-02-11T10:30:00+00:00"

    def test_preserves_existing_config(self, tmp_path, monkeypatch):
        """Adding a remote doesn't clobber other config sections."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        from siftd.config import set_config, get_config

        set_config("search.formatter", "verbose")
        set_sync_remote("alcove", "alcove", "/data/team.db")

        assert get_config("search.formatter") == "verbose"
        assert get_sync_remote("alcove") is not None

    def test_remote_auth_config_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        set_sync_remote("team", None, "https://siftd.example.com")
        set_remote_auth("team", {"token_command": "gh auth token"})
        remote = get_sync_remote("team")
        assert remote["auth"] == {"token_command": "gh auth token"}

    def test_remote_without_auth(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        set_sync_remote("local", None, "/tmp/team.db")
        remote = get_sync_remote("local")
        assert remote["auth"] is None


# --- Local push tests ---


class TestLocalPush:
    def test_first_push_creates_db(self, tmp_path, monkeypatch):
        """First push to nonexistent target creates DB directly."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"

        remote = SyncRemote(name="test", host=None, path=str(target_path))
        result = sync_push(source, remote)

        assert result.conversations == 1
        assert not result.remote_existed
        assert not result.dry_run
        assert target_path.exists()

        # Verify conversations in target
        conn = sqlite3.connect(str(target_path))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 1

    def test_subsequent_push_merges(self, tmp_path, monkeypatch):
        """Second push merges delta into existing remote DB."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[
                {"external_id": "conv-A"},
                {"external_id": "conv-B"},
            ],
        )
        target_path = tmp_path / "remote" / "team.db"

        remote = SyncRemote(name="test", host=None, path=str(target_path))

        # First push
        result1 = sync_push(source, remote)
        assert result1.conversations == 2
        assert not result1.remote_existed

        # Second push (idempotent)
        result2 = sync_push(source, remote, push_all=True)
        assert result2.conversations == 2  # slice has 2, merge deduplicates
        assert result2.remote_existed

        conn = sqlite3.connect(str(target_path))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2

    def test_empty_slice_exits_cleanly(self, tmp_path, monkeypatch):
        """Push with no matching conversations returns 0."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(tmp_path / "source.db", conversations=[])
        target_path = tmp_path / "remote" / "team.db"

        remote = SyncRemote(name="test", host=None, path=str(target_path))
        result = sync_push(source, remote)

        assert result.conversations == 0
        assert not target_path.exists()

    def test_dry_run(self, tmp_path, monkeypatch):
        """Dry run reports counts but doesn't create remote DB."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"

        remote = SyncRemote(name="test", host=None, path=str(target_path))
        result = sync_push(source, remote, dry_run=True)

        assert result.conversations == 1
        assert result.dry_run
        assert not target_path.exists()

    def test_updates_last_push(self, tmp_path, monkeypatch):
        """Successful push updates last_push in config."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"

        set_sync_remote("test", None, str(target_path))
        remote_cfg = get_sync_remote("test")
        remote_cfg.pop("auth", None)
        remote = SyncRemote(**remote_cfg)

        sync_push(source, remote)

        updated = get_sync_remote("test")
        assert updated["last_push"] is not None

    def test_explicit_since_does_not_update_last_push(self, tmp_path, monkeypatch):
        """Explicit --since should not advance last_push."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"

        set_sync_remote("test", None, str(target_path))
        update_last_push("test", "2025-01-01T00:00:00+00:00")
        remote_cfg = get_sync_remote("test")
        remote_cfg.pop("auth", None)
        remote = SyncRemote(**remote_cfg)

        result = sync_push(source, remote, since="2024-01-01")

        updated = get_sync_remote("test")
        assert updated["last_push"] == "2025-01-01T00:00:00+00:00"
        assert result.last_push_updated is False

    def test_workspace_filter(self, tmp_path, monkeypatch):
        """Push with workspace filter only pushes matching conversations."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"

        remote = SyncRemote(name="test", host=None, path=str(target_path))

        # Non-matching workspace filter
        result = sync_push(source, remote, workspace="nonexistent")
        assert result.conversations == 0

    def test_fk_integrity(self, tmp_path, monkeypatch):
        """Remote DB passes FK check after push."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}, {"external_id": "conv-B"}],
        )
        target_path = tmp_path / "remote" / "team.db"

        remote = SyncRemote(name="test", host=None, path=str(target_path))
        sync_push(source, remote)

        conn = sqlite3.connect(str(target_path))
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == []

    def test_since_from_last_push(self, tmp_path, monkeypatch):
        """Push uses last_push as since when no explicit --since given."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[
                {"external_id": "conv-old", "started_at": "2024-01-01T10:00:00Z"},
                {"external_id": "conv-new", "started_at": "2026-02-01T10:00:00Z"},
            ],
        )
        target_path = tmp_path / "remote" / "team.db"

        # Set last_push to filter out old conversation
        remote = SyncRemote(
            name="test", host=None, path=str(target_path),
            last_push="2025-01-01",
        )
        result = sync_push(source, remote)

        assert result.conversations == 1

        conn = sqlite3.connect(str(target_path))
        ext_ids = [r[0] for r in conn.execute("SELECT external_id FROM conversations").fetchall()]
        conn.close()
        assert "conv-new" in ext_ids
        assert "conv-old" not in ext_ids


# --- SSH push tests (mocked) ---


def _ssh_mock_ok(response_json):
    """Return a mock subprocess.run that succeeds with the given JSON response."""
    def mock_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(response_json).encode()
        result.stderr = b""
        return result
    return mock_run


class TestSSHPush:
    def test_single_ssh_command(self, tmp_path, monkeypatch):
        """Verify a single ssh command with db receive is used."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps({"status": "created", "conversations": 1}).encode()
            result.stderr = b""
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_push(source, remote)

        assert result.conversations == 1
        assert not result.remote_existed  # status == "created"
        assert len(calls) == 1
        assert calls[0][0] == "ssh"
        assert "alcove" in calls[0]
        assert "db receive" in calls[0][-1]

    def test_receive_command_construction(self, tmp_path, monkeypatch):
        """Verify the ssh command includes --db and --no-fts."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps({"status": "created", "conversations": 1}).encode()
            result.stderr = b""
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            sync_push(source, remote)

        receive_cmd = calls[0][-1]
        assert "siftd --db" in receive_cmd
        assert "db receive --no-fts" in receive_cmd

    def test_merged_status_means_existed(self, tmp_path, monkeypatch):
        """Remote returning status=merged means remote_existed=True."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        mock = _ssh_mock_ok({"status": "merged", "conversations": 0})
        with patch("siftd.api.sync.subprocess.run", side_effect=mock):
            result = sync_push(source, remote)

        assert result.remote_existed

    def test_created_status_means_not_existed(self, tmp_path, monkeypatch):
        """Remote returning status=created means remote_existed=False."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        mock = _ssh_mock_ok({"status": "created", "conversations": 1})
        with patch("siftd.api.sync.subprocess.run", side_effect=mock):
            result = sync_push(source, remote)

        assert not result.remote_existed

    def test_ssh_timeout_raises_syncerror(self, tmp_path, monkeypatch):
        """SSH timeout surfaces as SyncError with friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", None))

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="timed out.*slow or unreachable"):
                sync_push(source, remote)

    def test_nonzero_exit_siftd_not_found(self, tmp_path, monkeypatch):
        """Remote 'command not found' gets friendly install hint."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = b""
            result.stderr = b"siftd: command not found"
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="not installed on alcove.*uv tool install"):
                sync_push(source, remote)

    def test_unparseable_json_raises_syncerror(self, tmp_path, monkeypatch):
        """Unparseable JSON stdout raises SyncError."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = b"not json at all"
            result.stderr = b""
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Unexpected response"):
                sync_push(source, remote)

    def test_ssh_options_from_config(self, tmp_path, monkeypatch):
        """SSH options from config are included in the command."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps({"status": "created", "conversations": 1}).encode()
            result.stderr = b""
            return result

        # Write config with SSH options
        from siftd.config import set_sync_remote
        from pathlib import Path
        import tomlkit

        set_sync_remote("test", "alcove", "/data/team.db")
        cfg_path = Path(str(tmp_path / "config")) / "siftd" / "config.toml"
        doc = tomlkit.parse(cfg_path.read_text())
        if "sync" not in doc:
            doc["sync"] = tomlkit.table()
        doc["sync"]["ssh"] = tomlkit.table()
        doc["sync"]["ssh"]["options"] = ["-o", "StrictHostKeyChecking=no"]
        doc["sync"]["ssh"]["connect_timeout_s"] = 60
        cfg_path.write_text(tomlkit.dumps(doc))

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            sync_push(source, remote)

        cmd = calls[0]
        assert "-o" in cmd
        assert "StrictHostKeyChecking=no" in cmd
        assert "ConnectTimeout=60" in cmd[cmd.index("-o", cmd.index("StrictHostKeyChecking=no")) + 1]

    def test_stdin_is_slice_file(self, tmp_path, monkeypatch):
        """Verify stdin kwarg is the opened slice file."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        stdin_files = []

        def mock_run(cmd, **kwargs):
            if "stdin" in kwargs and kwargs["stdin"] is not None:
                stdin_files.append(kwargs["stdin"])
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps({"status": "created", "conversations": 1}).encode()
            result.stderr = b""
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            sync_push(source, remote)

        assert len(stdin_files) == 1
        assert hasattr(stdin_files[0], "read")  # file-like object

    def test_connection_refused_friendly(self, tmp_path, monkeypatch):
        """Connection refused gets friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            raise OSError("Connection refused")

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Cannot connect to alcove"):
                sync_push(source, remote)

    def test_permission_denied_friendly(self, tmp_path, monkeypatch):
        """Permission denied gets friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            raise OSError("Permission denied (publickey)")

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="SSH authentication failed"):
                sync_push(source, remote)

    def test_hostname_resolution_friendly(self, tmp_path, monkeypatch):
        """Hostname resolution failure gets friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        remote = SyncRemote(name="test", host="badhost", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            raise OSError("Could not resolve hostname 'badhost'")

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Cannot resolve hostname"):
                sync_push(source, remote)

    def test_database_locked_json_error(self, tmp_path, monkeypatch):
        """Remote database locked error (JSON) gets friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = b""
            result.stderr = json.dumps(
                {"error": "database is locked", "error_type": "database_locked"}
            ).encode()
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Remote database is locked.*Wait and retry"):
                sync_push(source, remote)

    def test_generic_json_remote_error(self, tmp_path, monkeypatch):
        """Remote JSON error without special type gets clean message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = b""
            result.stderr = json.dumps({"error": "Not a valid SQLite database"}).encode()
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Remote error: Not a valid SQLite"):
                sync_push(source, remote)

    def test_raw_stderr_fallback(self, tmp_path, monkeypatch):
        """Non-JSON stderr falls back to first line."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = b""
            result.stderr = b"Traceback (most recent call last):\n  File ...\nsqlite3.OperationalError: database is locked"
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Remote error on alcove: Traceback"):
                sync_push(source, remote)


# --- CLI integration tests ---


class TestCLI:
    def test_remote_add_ssh(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = main(["db", "remote", "add", "alcove", "alcove:/data/team.db"])
        assert rc == 0
        assert "Added remote 'alcove'" in capsys.readouterr().out

        remote = get_sync_remote("alcove")
        assert remote["host"] == "alcove"
        assert remote["path"] == "/data/team.db"

    def test_remote_add_local(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = main(["db", "remote", "add", "nas", "/mnt/nas/team.db"])
        assert rc == 0
        assert "(local)" in capsys.readouterr().out

        remote = get_sync_remote("nas")
        assert remote["host"] is None

    def test_remote_list_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = main(["db", "remote", "list"])
        assert rc == 0
        assert "No remotes" in capsys.readouterr().out

    def test_remote_list_populated(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        set_sync_remote("alcove", "alcove", "/data/team.db")

        rc = main(["db", "remote", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "alcove" in out
        assert "/data/team.db" in out

    def test_remote_remove(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        set_sync_remote("alcove", "alcove", "/data/team.db")

        rc = main(["db", "remote", "remove", "alcove"])
        assert rc == 0
        assert "Removed" in capsys.readouterr().out
        assert get_sync_remote("alcove") is None

    def test_remote_remove_nonexistent(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = main(["db", "remote", "remove", "nope"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_push_no_remote(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = main(["db", "push", "nonexistent"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_push_local(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"
        set_sync_remote("local", None, str(target_path))

        rc = main(["--db", str(source), "db", "push", "local"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Pushed 1 conversations" in captured.out
        assert "(new remote database)" in captured.out
        assert "Pushing to local" in captured.err

    def test_push_dry_run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target_path = tmp_path / "remote" / "team.db"
        set_sync_remote("local", None, str(target_path))

        rc = main(["--db", str(source), "db", "push", "local", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Would push 1 conversations to local" in out
        assert not target_path.exists()

    def test_push_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(tmp_path / "source.db", conversations=[])
        target_path = tmp_path / "remote" / "team.db"
        set_sync_remote("local", None, str(target_path))

        rc = main(["--db", str(source), "db", "push", "local"])
        assert rc == 0
        assert "Nothing new to push to local" in capsys.readouterr().out

    def test_receive_valid_stdin(self, tmp_path, monkeypatch, capsys):
        """db receive with valid slice on stdin → JSON output, exit 0."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"

        # Mock stdin with the source file content
        with open(source, "rb") as f:
            data = f.read()
        mock_stdin = io.BytesIO(data)
        mock_stdin_wrapper = MagicMock()
        mock_stdin_wrapper.buffer = mock_stdin
        mock_stdin_wrapper.isatty = MagicMock(return_value=False)

        with patch("siftd.cli_db.sys.stdin", mock_stdin_wrapper):
            rc = main(["--db", str(target), "db", "receive"])

        assert rc == 0
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["status"] == "created"
        assert result["conversations"] == 1

    def test_receive_empty_stdin(self, tmp_path, monkeypatch, capsys):
        """db receive with empty stdin → error JSON on stderr, exit 1."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        target = tmp_path / "target.db"

        mock_stdin = io.BytesIO(b"")
        mock_stdin_wrapper = MagicMock()
        mock_stdin_wrapper.buffer = mock_stdin
        mock_stdin_wrapper.isatty = MagicMock(return_value=False)

        with patch("siftd.cli_db.sys.stdin", mock_stdin_wrapper):
            rc = main(["--db", str(target), "db", "receive"])

        assert rc == 1
        err = capsys.readouterr().err
        error = json.loads(err)
        assert "error" in error

    def test_receive_invalid_data(self, tmp_path, monkeypatch, capsys):
        """db receive with non-SQLite data → error JSON on stderr, exit 1."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        target = tmp_path / "target.db"

        mock_stdin = io.BytesIO(b"this is not a database at all!!")
        mock_stdin_wrapper = MagicMock()
        mock_stdin_wrapper.buffer = mock_stdin
        mock_stdin_wrapper.isatty = MagicMock(return_value=False)

        with patch("siftd.cli_db.sys.stdin", mock_stdin_wrapper):
            rc = main(["--db", str(target), "db", "receive"])

        assert rc == 1
        err = capsys.readouterr().err
        error = json.loads(err)
        assert "Not a valid SQLite" in error["error"]

    def test_receive_merge_into_existing(self, tmp_path, monkeypatch, capsys):
        """db receive into existing DB → JSON with merged status."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"
        _make_db(target, conversations=[{"external_id": "conv-B"}])

        with open(source, "rb") as f:
            data = f.read()
        mock_stdin = io.BytesIO(data)
        mock_stdin_wrapper = MagicMock()
        mock_stdin_wrapper.buffer = mock_stdin
        mock_stdin_wrapper.isatty = MagicMock(return_value=False)

        with patch("siftd.cli_db.sys.stdin", mock_stdin_wrapper):
            rc = main(["--db", str(target), "db", "receive"])

        assert rc == 0
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["status"] == "merged"


# --- SSH options tests ---


class TestSSHOptions:
    def test_global_options_applied(self, tmp_path, monkeypatch):
        """Global SSH options are returned."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        import tomlkit
        from pathlib import Path

        cfg_dir = tmp_path / "siftd"
        cfg_dir.mkdir(parents=True)
        doc = tomlkit.document()
        doc["sync"] = tomlkit.table()
        doc["sync"]["ssh"] = tomlkit.table()
        doc["sync"]["ssh"]["options"] = ["-o", "StrictHostKeyChecking=no"]
        (cfg_dir / "config.toml").write_text(tomlkit.dumps(doc))

        opts = get_ssh_options()
        assert opts == ["-o", "StrictHostKeyChecking=no"]

    def test_per_remote_overrides_global(self, tmp_path, monkeypatch):
        """Per-remote SSH options take precedence over global."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        import tomlkit
        from pathlib import Path

        cfg_dir = tmp_path / "siftd"
        cfg_dir.mkdir(parents=True)
        doc = tomlkit.document()
        doc["sync"] = tomlkit.table()
        doc["sync"]["ssh"] = tomlkit.table()
        doc["sync"]["ssh"]["options"] = ["-o", "StrictHostKeyChecking=no"]
        doc["sync"]["remotes"] = tomlkit.table()
        doc["sync"]["remotes"]["alcove"] = tomlkit.table()
        doc["sync"]["remotes"]["alcove"]["host"] = "alcove"
        doc["sync"]["remotes"]["alcove"]["path"] = "/data/team.db"
        doc["sync"]["remotes"]["alcove"]["ssh"] = tomlkit.table()
        doc["sync"]["remotes"]["alcove"]["ssh"]["options"] = ["-i", "~/.ssh/alcove_key"]
        (cfg_dir / "config.toml").write_text(tomlkit.dumps(doc))

        opts = get_ssh_options("alcove")
        assert opts == ["-i", "~/.ssh/alcove_key"]

    def test_connect_timeout_adds_option(self, tmp_path, monkeypatch):
        """connect_timeout_s adds -o ConnectTimeout=N."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        import tomlkit
        from pathlib import Path

        cfg_dir = tmp_path / "siftd"
        cfg_dir.mkdir(parents=True)
        doc = tomlkit.document()
        doc["sync"] = tomlkit.table()
        doc["sync"]["ssh"] = tomlkit.table()
        doc["sync"]["ssh"]["connect_timeout_s"] = 30
        (cfg_dir / "config.toml").write_text(tomlkit.dumps(doc))

        opts = get_ssh_options()
        assert "-o" in opts
        assert "ConnectTimeout=30" in opts

    def test_missing_config_returns_empty(self, tmp_path, monkeypatch):
        """No SSH config returns empty list."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        opts = get_ssh_options()
        assert opts == []

    def test_missing_remote_falls_back_to_global(self, tmp_path, monkeypatch):
        """Unknown remote name falls back to global SSH options."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        import tomlkit
        from pathlib import Path

        cfg_dir = tmp_path / "siftd"
        cfg_dir.mkdir(parents=True)
        doc = tomlkit.document()
        doc["sync"] = tomlkit.table()
        doc["sync"]["ssh"] = tomlkit.table()
        doc["sync"]["ssh"]["options"] = ["-v"]
        (cfg_dir / "config.toml").write_text(tomlkit.dumps(doc))

        opts = get_ssh_options("nonexistent")
        assert opts == ["-v"]


# --- Config last_pull tests ---


class TestConfigLastPull:
    def test_update_last_pull(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("alcove", "alcove", "/data/team.db")
        update_last_pull("alcove", "2026-02-20T10:30:00+00:00")

        remote = get_sync_remote("alcove")
        assert remote is not None
        assert remote["last_pull"] == "2026-02-20T10:30:00+00:00"

    def test_last_pull_initially_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        set_sync_remote("alcove", "alcove", "/data/team.db")
        remote = get_sync_remote("alcove")
        assert remote["last_pull"] is None


# --- Local pull tests ---


class TestLocalPull:
    def test_pull_from_local_remote(self, tmp_path, monkeypatch):
        """Pull from a local-path remote merges into local DB."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir()
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}, {"external_id": "conv-B"}],
        )
        local_db = tmp_path / "local.db"

        remote = SyncRemote(name="test", host=None, path=str(remote_db))
        result = sync_pull(local_db, remote)

        assert result.conversations == 2
        assert result.size_bytes > 0
        assert not result.dry_run
        assert local_db.exists()

        conn = sqlite3.connect(str(local_db))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2

    def test_pull_empty_result(self, tmp_path, monkeypatch):
        """Pull with no matching conversations returns 0."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(tmp_path / "remote" / "team.db", conversations=[])
        local_db = tmp_path / "local.db"

        remote = SyncRemote(name="test", host=None, path=str(remote_db))
        result = sync_pull(local_db, remote)

        assert result.conversations == 0

    def test_pull_dry_run(self, tmp_path, monkeypatch):
        """Dry run reports counts but doesn't create local DB."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"

        remote = SyncRemote(name="test", host=None, path=str(remote_db))
        result = sync_pull(local_db, remote, dry_run=True)

        assert result.conversations == 1
        assert result.dry_run
        assert not local_db.exists()

    def test_pull_updates_last_pull(self, tmp_path, monkeypatch):
        """Successful pull updates last_pull in config."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"

        set_sync_remote("test", None, str(remote_db))
        remote_cfg = get_sync_remote("test")
        remote_cfg.pop("auth", None)
        remote = SyncRemote(**remote_cfg)

        sync_pull(local_db, remote)

        updated = get_sync_remote("test")
        assert updated["last_pull"] is not None

    def test_explicit_since_does_not_update_last_pull(self, tmp_path, monkeypatch):
        """Explicit --since should not advance last_pull."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"

        set_sync_remote("test", None, str(remote_db))
        update_last_pull("test", "2025-01-01T00:00:00+00:00")
        remote_cfg = get_sync_remote("test")
        remote_cfg.pop("auth", None)
        remote = SyncRemote(**remote_cfg)

        result = sync_pull(local_db, remote, since="2024-01-01")

        updated = get_sync_remote("test")
        assert updated["last_pull"] == "2025-01-01T00:00:00+00:00"
        assert result.last_pull_updated is False

    def test_pull_since_from_last_pull(self, tmp_path, monkeypatch):
        """Pull uses last_pull as since when no explicit --since given."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[
                {"external_id": "conv-old", "started_at": "2024-01-01T10:00:00Z"},
                {"external_id": "conv-new", "started_at": "2026-02-01T10:00:00Z"},
            ],
        )
        local_db = tmp_path / "local.db"

        remote = SyncRemote(
            name="test", host=None, path=str(remote_db),
            last_pull="2025-01-01",
        )
        result = sync_pull(local_db, remote)

        assert result.conversations == 1

        conn = sqlite3.connect(str(local_db))
        ext_ids = [r[0] for r in conn.execute("SELECT external_id FROM conversations").fetchall()]
        conn.close()
        assert "conv-new" in ext_ids
        assert "conv-old" not in ext_ids

    def test_pull_workspace_filter(self, tmp_path, monkeypatch):
        """Pull with workspace filter only pulls matching conversations."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"

        remote = SyncRemote(name="test", host=None, path=str(remote_db))
        result = sync_pull(local_db, remote, workspace="nonexistent")

        assert result.conversations == 0

    def test_pull_remote_not_found(self, tmp_path, monkeypatch):
        """Pull from nonexistent local path raises SyncError."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host=None, path=str(tmp_path / "nope.db"))

        with pytest.raises(SyncError, match="Remote database not found"):
            sync_pull(local_db, remote)

    def test_pull_idempotent(self, tmp_path, monkeypatch):
        """Pulling the same data twice doesn't duplicate conversations."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"

        remote = SyncRemote(name="test", host=None, path=str(remote_db))
        sync_pull(local_db, remote)
        sync_pull(local_db, remote, pull_all=True)

        conn = sqlite3.connect(str(local_db))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 1


# --- SSH pull tests (mocked) ---


class TestSSHPull:
    def _make_send_response(self, tmp_path, conversations=1):
        """Create a slice DB and return (db_bytes, stderr_json)."""
        source = _make_db(
            tmp_path / "ssh-source.db",
            conversations=[{"external_id": f"conv-{i}"} for i in range(conversations)],
        )
        db_bytes = source.read_bytes()
        stderr_json = json.dumps({"conversations": conversations, "size_bytes": len(db_bytes)})
        return db_bytes, stderr_json

    def test_single_ssh_command(self, tmp_path, monkeypatch):
        """Verify a single ssh command with db send is used."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        db_bytes, stderr_json = self._make_send_response(tmp_path)
        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            # Write db bytes to the stdout file
            stdout_file = kwargs.get("stdout")
            if stdout_file and hasattr(stdout_file, "write"):
                stdout_file.write(db_bytes)
            result = MagicMock()
            result.returncode = 0
            result.stderr = stderr_json.encode()
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_pull(local_db, remote)

        assert result.conversations == 1
        assert len(calls) == 1
        assert calls[0][0] == "ssh"
        assert "alcove" in calls[0]
        assert "db send" in calls[0][-1]

    def test_send_command_construction(self, tmp_path, monkeypatch):
        """Verify the ssh command includes --db, --no-fts, and filters."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        db_bytes, stderr_json = self._make_send_response(tmp_path)
        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            stdout_file = kwargs.get("stdout")
            if stdout_file and hasattr(stdout_file, "write"):
                stdout_file.write(db_bytes)
            result = MagicMock()
            result.returncode = 0
            result.stderr = stderr_json.encode()
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            sync_pull(local_db, remote, since="2025-01-01", workspace="proj")

        send_cmd = calls[0][-1]
        assert "siftd --db" in send_cmd
        assert "db send --no-fts" in send_cmd
        assert "--since 2025-01-01" in send_cmd
        assert "-w proj" in send_cmd

    def test_ssh_empty_result(self, tmp_path, monkeypatch):
        """Remote returning 0 conversations exits cleanly."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = json.dumps({"conversations": 0, "size_bytes": 0}).encode()
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_pull(local_db, remote)

        assert result.conversations == 0
        assert not local_db.exists()

    def test_ssh_timeout_raises_syncerror(self, tmp_path, monkeypatch):
        """SSH timeout surfaces as SyncError with friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", None))

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="timed out.*slow or unreachable"):
                sync_pull(local_db, remote)

    def test_ssh_connection_refused(self, tmp_path, monkeypatch):
        """Connection refused gets friendly message."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            raise OSError("Connection refused")

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Cannot connect to alcove"):
                sync_pull(local_db, remote)

    def test_siftd_not_installed(self, tmp_path, monkeypatch):
        """Remote 'command not found' gets friendly install hint."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = b"siftd: command not found"
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="not installed on alcove"):
                sync_pull(local_db, remote)

    def test_dry_run_does_not_merge(self, tmp_path, monkeypatch):
        """Dry run queries remote but doesn't create local DB."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        db_bytes, stderr_json = self._make_send_response(tmp_path)
        local_db = tmp_path / "local.db"
        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            stdout_file = kwargs.get("stdout")
            if stdout_file and hasattr(stdout_file, "write"):
                stdout_file.write(db_bytes)
            result = MagicMock()
            result.returncode = 0
            result.stderr = stderr_json.encode()
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_pull(local_db, remote, dry_run=True)

        assert result.conversations == 1
        assert result.dry_run
        assert not local_db.exists()


# --- Pull CLI integration tests ---


class TestPullCLI:
    def test_pull_no_remote(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = main(["db", "pull", "nonexistent"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_pull_local(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"
        set_sync_remote("local", None, str(remote_db))

        rc = main(["--db", str(local_db), "db", "pull", "local"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Pulled 1 conversations" in captured.out
        assert "Pulling from local" in captured.err

    def test_pull_dry_run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(
            tmp_path / "remote" / "team.db",
            conversations=[{"external_id": "conv-A"}],
        )
        local_db = tmp_path / "local.db"
        set_sync_remote("local", None, str(remote_db))

        rc = main(["--db", str(local_db), "db", "pull", "local", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Would pull 1 conversations from local" in out
        assert not local_db.exists()

    def test_pull_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        (tmp_path / "remote").mkdir(exist_ok=True)
        remote_db = _make_db(tmp_path / "remote" / "team.db", conversations=[])
        local_db = tmp_path / "local.db"
        set_sync_remote("local", None, str(remote_db))

        rc = main(["--db", str(local_db), "db", "pull", "local"])
        assert rc == 0
        assert "Nothing new to pull from local" in capsys.readouterr().out


# --- Send CLI integration tests ---


class TestSendCLI:
    def test_send_tty_rejected(self, tmp_path, monkeypatch, capsys):
        """db send to a terminal is rejected."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        mock_stdout = MagicMock()
        mock_stdout.isatty = MagicMock(return_value=True)

        with patch("siftd.cli_db.sys.stdout", mock_stdout):
            rc = main(["--db", str(source), "db", "send"])

        assert rc == 1
        err = capsys.readouterr().err
        error = json.loads(err)
        assert "terminal" in error["error"]

    def test_send_writes_to_stdout(self, tmp_path, monkeypatch, capsys):
        """db send writes binary SQLite to stdout and metadata to stderr."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        stdout_buf = io.BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.isatty = MagicMock(return_value=False)
        mock_stdout.buffer = stdout_buf

        with patch("siftd.cli_db.sys.stdout", mock_stdout):
            rc = main(["--db", str(source), "db", "send"])

        assert rc == 0
        err = capsys.readouterr().err
        meta = json.loads(err)
        assert meta["conversations"] == 1
        assert meta["size_bytes"] > 0

        # Verify stdout is valid SQLite
        stdout_buf.seek(0)
        assert stdout_buf.read(16).startswith(b"SQLite format 3\x00")

    def test_send_empty_db(self, tmp_path, monkeypatch, capsys):
        """db send with no conversations writes metadata with 0 count."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(tmp_path / "source.db", conversations=[])

        mock_stdout = MagicMock()
        mock_stdout.isatty = MagicMock(return_value=False)
        mock_stdout.buffer = io.BytesIO()

        with patch("siftd.cli_db.sys.stdout", mock_stdout):
            rc = main(["--db", str(source), "db", "send"])

        assert rc == 0
        err = capsys.readouterr().err
        meta = json.loads(err)
        assert meta["conversations"] == 0


# --- HTTP transport tests ---


class TestHTTPPush:
    def test_push_http_posts_slice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-1"}],
        )
        set_sync_remote("team", None, "https://siftd.example.com")
        set_remote_auth("team", {"token": "test-token"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "created", "conversations": 1}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.post.return_value = mock_response
            cfg = get_sync_remote("team")
            cfg.pop("auth", None)
            remote = SyncRemote(**cfg)
            result = sync_push(source, remote)

        assert result.conversations == 1
        assert not result.dry_run
        client_instance.post.assert_called_once()
        call_args = client_instance.post.call_args
        assert "/v1/push" in call_args.args[0]


class TestHTTPPull:
    def test_pull_http_streams_slice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()
        local_db = tmp_path / "local.db"

        # Create a small valid SQLite DB to use as the pull response body
        source = _make_db(
            tmp_path / "remote-slice.db",
            conversations=[{"external_id": "remote-conv-1"}],
        )
        slice_bytes = source.read_bytes()

        set_sync_remote("team", None, "https://siftd.example.com")
        set_remote_auth("team", {"token": "test-token"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = slice_bytes
        mock_response.headers = {
            "X-Siftd-Conversations": "1",
            "X-Siftd-Size": str(len(slice_bytes)),
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.Client") as MockClient:
            client_instance = MockClient.return_value.__enter__.return_value
            client_instance.get.return_value = mock_response
            cfg = get_sync_remote("team")
            cfg.pop("auth", None)
            remote = SyncRemote(**cfg)
            result = sync_pull(local_db, remote)

        assert result.conversations == 1
        assert local_db.exists()
