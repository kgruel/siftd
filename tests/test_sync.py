"""Tests for siftd db push — push conversations to a remote database."""

import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from siftd.api.sync import SyncError, SyncRemote, sync_push
from siftd.cli import main
from siftd.config import (
    get_sync_remote,
    get_sync_remotes,
    remove_sync_remote,
    set_sync_remote,
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


class TestSSHPush:
    def test_scp_command_construction(self, tmp_path, monkeypatch):
        """Verify scp is called with correct arguments for first push."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        scp_calls = []

        def mock_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            result = MagicMock()
            if cmd[0] == "ssh" and "test" in cmd and "-f" in cmd:
                # _remote_file_exists → file doesn't exist
                result.returncode = 1
                return result
            if cmd[0] == "scp":
                scp_calls.append(cmd)
                result.returncode = 0
                return result
            result.returncode = 0
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_push(source, remote)

        assert result.conversations == 1
        assert not result.remote_existed
        assert len(scp_calls) == 1
        assert scp_calls[0][0] == "scp"
        assert "alcove:/data/team.db" in scp_calls[0][2]

    def test_ssh_merge_for_existing_remote(self, tmp_path, monkeypatch):
        """When remote exists, verify scp to temp + ssh merge + rm temp."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")
        ssh_commands = []

        def mock_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            result = MagicMock()
            if cmd[0] == "ssh":
                ssh_commands.append(cmd)
                if "test" in cmd and "-f" in cmd:
                    # _remote_file_exists → file exists
                    result.returncode = 0
                    return result
                if "command" in cmd and "-v" in cmd:
                    # _require_remote_siftd → found
                    result.returncode = 0
                    result.stdout = b"/usr/local/bin/siftd"
                    return result
                # ssh merge or rm
                result.returncode = 0
                result.stdout = b""
                return result
            if cmd[0] == "scp":
                result.returncode = 0
                return result
            result.returncode = 0
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_push(source, remote)

        assert result.conversations == 1
        assert result.remote_existed

        # Check that ssh merge command was called
        merge_cmds = [c for c in ssh_commands if any("db merge" in str(arg) for arg in c)]
        assert len(merge_cmds) == 1

    def test_scp_failure(self, tmp_path, monkeypatch):
        """scp failure raises SyncError."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            result = MagicMock()
            if cmd[0] == "ssh" and "test" in cmd and "-f" in cmd:
                result.returncode = 1  # doesn't exist
                return result
            if cmd[0] == "scp":
                result.returncode = 1
                result.stderr = b"Permission denied"
                return result
            result.returncode = 0
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="Permission denied"):
                sync_push(source, remote)

    def test_siftd_not_found(self, tmp_path, monkeypatch):
        """Missing siftd on remote raises SyncError."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            result = MagicMock()
            if cmd[0] == "ssh" and "test" in cmd and "-f" in cmd:
                result.returncode = 0  # file exists
                return result
            if cmd[0] == "ssh" and "command" in cmd:
                # siftd not found
                raise __import__("subprocess").CalledProcessError(1, cmd)
            result.returncode = 0
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="does not have siftd installed"):
                sync_push(source, remote)

    def test_ssh_timeout_raises_syncerror(self, tmp_path, monkeypatch):
        """SSH timeout surfaces as SyncError."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            if cmd[0] == "ssh" and "test" in cmd and "-f" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", None))
            from unittest.mock import MagicMock
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            with pytest.raises(SyncError, match="timed out"):
                sync_push(source, remote)

    def test_cleanup_failure_ignored(self, tmp_path, monkeypatch):
        """Cleanup rm failure shouldn't fail a successful push."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )

        remote = SyncRemote(name="test", host="alcove", path="/data/team.db")

        def mock_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            result = MagicMock()
            if cmd[0] == "ssh":
                if "test" in cmd and "-f" in cmd:
                    result.returncode = 0
                    return result
                if "command" in cmd and "-v" in cmd:
                    result.returncode = 0
                    result.stdout = b"/usr/local/bin/siftd"
                    return result
                if "rm -f" in cmd[-1]:
                    result.returncode = 1
                    result.stderr = b"rm failed"
                    return result
                result.returncode = 0
                result.stdout = b""
                return result
            if cmd[0] == "scp":
                result.returncode = 0
                return result
            result.returncode = 0
            return result

        with patch("siftd.api.sync.subprocess.run", side_effect=mock_run):
            result = sync_push(source, remote)

        assert result.conversations == 1
        assert result.remote_existed


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
        out = capsys.readouterr().out
        assert "Pushed 1 conversation(s)" in out
        assert "Created new remote database" in out

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
        assert "[dry run]" in out
        assert not target_path.exists()

    def test_push_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(tmp_path / "source.db", conversations=[])
        target_path = tmp_path / "remote" / "team.db"
        set_sync_remote("local", None, str(target_path))

        rc = main(["--db", str(source), "db", "push", "local"])
        assert rc == 0
        assert "No new conversations" in capsys.readouterr().out
