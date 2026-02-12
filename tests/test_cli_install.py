"""Tests for 'siftd install plugin' command."""

import os
import stat
from argparse import Namespace
from pathlib import Path

from siftd.cli_install import _find_plugin_source, _install_plugin


def _make_args(dry_run=False, scope="user") -> Namespace:
    return Namespace(extra="plugin", dry_run=dry_run, scope=scope)


class TestPluginBundled:
    """Verify plugin files are discoverable."""

    def test_plugin_files_exist(self):
        """Plugin source can be located (wheel or editable)."""
        source = _find_plugin_source()
        assert source is not None
        assert source.is_dir()
        assert (source / ".claude-plugin" / "plugin.json").exists()


class TestInstallPluginUser:
    """Install plugin to user-scope target."""

    def test_copies_to_user_scope(self, tmp_path, monkeypatch):
        """Plugin files are copied to ~/.claude/plugins/siftd/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_plugin(_make_args())

        assert rc == 0
        target = fake_home / ".claude" / "plugins" / "siftd"
        assert target.is_dir()
        assert (target / ".claude-plugin" / "plugin.json").exists()

    def test_idempotent_removes_stale(self, tmp_path, monkeypatch):
        """Re-installing removes old files before copying."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        target = fake_home / ".claude" / "plugins" / "siftd"
        target.mkdir(parents=True)
        stale = target / "leftover.txt"
        stale.write_text("stale")

        rc = _install_plugin(_make_args())

        assert rc == 0
        assert not stale.exists()
        assert (target / ".claude-plugin" / "plugin.json").exists()


    def test_replaces_symlink(self, tmp_path, monkeypatch):
        """Symlink at target (e.g. from dev-mode) is replaced with real dir."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        target = fake_home / ".claude" / "plugins" / "siftd"
        target.parent.mkdir(parents=True)
        target.symlink_to(tmp_path / "elsewhere")

        rc = _install_plugin(_make_args())

        assert rc == 0
        assert not target.is_symlink()
        assert target.is_dir()
        assert (target / ".claude-plugin" / "plugin.json").exists()


class TestInstallPluginProject:
    """Install plugin to project-scope target."""

    def test_copies_to_project_scope(self, tmp_path, monkeypatch):
        """--scope project installs to cwd/.claude/plugins/siftd/."""
        monkeypatch.chdir(tmp_path)

        rc = _install_plugin(_make_args(scope="project"))

        assert rc == 0
        target = tmp_path / ".claude" / "plugins" / "siftd"
        assert target.is_dir()
        assert (target / ".claude-plugin" / "plugin.json").exists()


class TestInstallPluginDryRun:
    """--dry-run prints plan without writing."""

    def test_dry_run_no_write(self, tmp_path, monkeypatch, capsys):
        """--dry-run shows source/target but creates nothing."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_plugin(_make_args(dry_run=True))

        assert rc == 0
        assert not (fake_home / ".claude" / "plugins" / "siftd").exists()
        captured = capsys.readouterr()
        assert "Source:" in captured.out
        assert "Target:" in captured.out
        assert "Scope:" in captured.out


class TestInstallPluginPermissions:
    """Shell scripts preserve executable bits."""

    def test_shell_scripts_executable(self, tmp_path, monkeypatch):
        """Shell scripts in plugin/scripts/ retain +x after copy."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _install_plugin(_make_args())

        target = fake_home / ".claude" / "plugins" / "siftd"
        scripts = list(target.glob("scripts/*.sh"))
        assert len(scripts) > 0, "Expected shell scripts in plugin/scripts/"
        for script in scripts:
            mode = os.stat(script).st_mode
            assert mode & stat.S_IXUSR, f"{script.name} missing owner execute bit"


class TestInstallPluginCLI:
    """Integration test via CLI main()."""

    def test_install_plugin_via_main(self, tmp_path, monkeypatch):
        """siftd install plugin works end-to-end through CLI dispatch."""
        from siftd.cli import main

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = main(["install", "plugin"])

        assert rc == 0
        assert (fake_home / ".claude" / "plugins" / "siftd" / ".claude-plugin" / "plugin.json").exists()
