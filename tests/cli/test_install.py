"""Tests for 'siftd install plugin' and 'siftd install skill' commands."""

import os
import stat
from argparse import Namespace
from pathlib import Path

from siftd.cli.install import _find_plugin_source, _install_plugin, _install_skill


def _make_args(dry_run=False, scope="user") -> Namespace:
    return Namespace(extra="plugin", dry_run=dry_run, scope=scope)


def _make_skill_args(dry_run=False, scope="user", harness=None) -> Namespace:
    return Namespace(extra="skill", dry_run=dry_run, scope=scope, harness=harness)


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


class TestInstallSkill:
    """Install skill-only (no hooks, no commands)."""

    def test_copies_skill_to_user_scope(self, tmp_path, monkeypatch):
        """Skill files are copied to ~/.claude/skills/siftd/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_skill(_make_skill_args())

        assert rc == 0
        target = fake_home / ".claude" / "skills" / "siftd"
        assert target.is_dir()
        assert (target / "SKILL.md").exists()
        assert (target / "reference" / "search.md").exists()
        assert (target / "reference" / "query.md").exists()
        assert (target / "reference" / "tags.md").exists()

    def test_no_hooks_or_commands(self, tmp_path, monkeypatch):
        """Skill install does not include plugin artifacts."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _install_skill(_make_skill_args())

        target = fake_home / ".claude" / "skills" / "siftd"
        assert not (target / "hooks").exists()
        assert not (target / "scripts").exists()
        assert not (target / "commands").exists()
        assert not (target / ".claude-plugin").exists()

    def test_copies_skill_to_project_scope(self, tmp_path, monkeypatch):
        """--scope project installs to cwd/.claude/skills/siftd/."""
        monkeypatch.chdir(tmp_path)

        rc = _install_skill(_make_skill_args(scope="project"))

        assert rc == 0
        target = tmp_path / ".claude" / "skills" / "siftd"
        assert (target / "SKILL.md").exists()

    def test_dry_run_no_write(self, tmp_path, monkeypatch, capsys):
        """--dry-run shows plan without writing."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_skill(_make_skill_args(dry_run=True))

        assert rc == 0
        assert not (fake_home / ".claude" / "skills" / "siftd").exists()
        captured = capsys.readouterr()
        assert "Source:" in captured.out

    def test_plugin_install_removes_standalone_skill(self, tmp_path, monkeypatch):
        """Installing plugin removes standalone skill to avoid duplicates."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Install skill first
        _install_skill(_make_skill_args())
        skill_target = fake_home / ".claude" / "skills" / "siftd"
        assert skill_target.exists()

        # Install plugin — should clean up the standalone skill
        _install_plugin(_make_args())
        assert not skill_target.exists()
        assert (fake_home / ".claude" / "plugins" / "siftd" / ".claude-plugin" / "plugin.json").exists()

    def test_install_skill_via_main(self, tmp_path, monkeypatch):
        """siftd install skill works end-to-end through CLI dispatch."""
        from siftd.cli import main

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = main(["install", "skill"])

        assert rc == 0
        assert (fake_home / ".claude" / "skills" / "siftd" / "SKILL.md").exists()

    def test_install_codex_cli_renders_instructions(self, tmp_path, monkeypatch):
        """Codex CLI gets a rendered plain-markdown file, not SKILL.md."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_skill(_make_skill_args(harness="codex_cli"))

        assert rc == 0
        target = fake_home / ".codex" / "siftd.md"
        assert target.exists()
        content = target.read_text()
        # Should have quick reference, not Claude Code frontmatter
        assert "skill-interface-version" not in content
        assert "siftd search" in content
        assert "siftd query" in content
        assert "Tag conventions" in content

    def test_install_gemini_cli(self, tmp_path, monkeypatch):
        """Gemini CLI gets instructions in ~/.gemini/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_skill(_make_skill_args(harness="gemini_cli"))

        assert rc == 0
        assert (fake_home / ".gemini" / "siftd.md").exists()

    def test_install_pi_agent_gets_skill(self, tmp_path, monkeypatch):
        """Pi Agent gets structured SKILL.md + reference/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_skill(_make_skill_args(harness="pi_agent"))

        assert rc == 0
        target = fake_home / ".pi" / "agent" / "skills" / "siftd"
        assert (target / "SKILL.md").exists()
        assert (target / "reference" / "search.md").exists()

    def test_install_unknown_harness_fails(self, tmp_path, monkeypatch):
        """Unknown harness returns error."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        rc = _install_skill(_make_skill_args(harness="nonexistent"))

        assert rc == 1
