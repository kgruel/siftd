"""Tests for 'siftd install plugin' and 'siftd install skill' commands."""

import os
import stat
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from siftd.cli.install import (
    _editable_source_url,
    _find_plugin_source,
    _install_commands,
    _install_plugin,
    _install_skill,
    _run_extra_install,
    cmd_install,
    detect_install_method,
    install_hint,
)


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


class TestInstallHelpers:
    def test_editable_source_url_parsing(self, monkeypatch):
        class _Dist:
            def read_text(self, name):
                assert name == "direct_url.json"
                return '{"url":"file:///tmp/src","dir_info":{"editable":true}}'

        monkeypatch.setattr("siftd.cli.install.distribution", lambda name: _Dist())
        assert _editable_source_url() == "file:///tmp/src"

        class _BadDist:
            def read_text(self, name):
                return "{bad"

        monkeypatch.setattr("siftd.cli.install.distribution", lambda name: _BadDist())
        assert _editable_source_url() is None

    def test_detect_install_method_variants(self, monkeypatch):
        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: None)
        monkeypatch.setattr("siftd.cli.install.sys.prefix", "/x/uv/tools/y")
        assert detect_install_method() == "uv_tool"

        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: "file:///src")
        assert detect_install_method() == "uv_tool_editable"

        monkeypatch.setattr("siftd.cli.install.sys.prefix", "/x/pipx/venvs/y")
        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: None)
        assert detect_install_method() == "pipx"

        monkeypatch.setattr("siftd.cli.install.sys.prefix", "/opt/homebrew/Cellar/siftd")
        assert detect_install_method() == "brew"

    def test_install_hint_and_commands(self, monkeypatch):
        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "editable")
        monkeypatch.setattr("siftd.cli.install._pip_cmd", lambda: "pip")
        assert "pip install -e" in install_hint("embed")

        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "uv_tool_editable")
        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: "file:///tmp/src")
        assert "uv tool install -e '/tmp/src[embed]'" in install_hint("embed")

        cmds = _install_commands("serve", source_path="/tmp/src")
        assert "uv_tool_editable" in cmds
        assert cmds["pip_user"][0] in {"uv", "pip"}

    def test_run_extra_install_paths(self, monkeypatch, capsys):
        args = SimpleNamespace(dry_run=False)

        rc = _run_extra_install(
            args,
            "embed",
            is_installed=lambda: True,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 0

        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "unknown")
        rc = _run_extra_install(
            args,
            "embed",
            is_installed=lambda: False,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 1

        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "editable")
        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: "")
        rc = _run_extra_install(
            args,
            "embed",
            is_installed=lambda: False,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 1

        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "pip_venv")
        monkeypatch.setattr("siftd.cli.install.install_hint", lambda extra: "pip install x")
        monkeypatch.setattr("siftd.cli.install._install_commands", lambda extra, source_path=None: {"pip_venv": ["pip", "install", "x"]})

        class _Res:
            returncode = 2

        monkeypatch.setattr("siftd.cli.install.subprocess.run", lambda *a, **k: _Res())
        rc = _run_extra_install(
            args,
            "embed",
            is_installed=lambda: False,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 2

        monkeypatch.setattr("siftd.cli.install.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        monkeypatch.setattr("siftd.cli.install._install_commands", lambda extra, source_path=None: {"pip_venv": ["uv"]})
        rc = _run_extra_install(
            args,
            "embed",
            is_installed=lambda: False,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_cmd_install_help_and_unknown(self, capsys):
        rc = cmd_install(SimpleNamespace(extra=None))
        assert rc == 0
        assert "Available components" in capsys.readouterr().out

        rc = cmd_install(SimpleNamespace(extra="nope"))
        assert rc == 1

    def test_find_plugin_source_editable_fallback(self, monkeypatch, tmp_path):
        fake_repo = tmp_path / "repo"
        (fake_repo / "plugin").mkdir(parents=True)

        class _Dist:
            def read_text(self, name):
                return '{"url":"file://' + str(fake_repo) + '"}'

        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "editable")
        monkeypatch.setattr("siftd.cli.install.distribution", lambda name: _Dist())
        monkeypatch.setattr("siftd.cli.install.importlib.resources.as_file", lambda ref: __import__("contextlib").nullcontext(tmp_path / "missing"))
        assert _find_plugin_source() == fake_repo / "plugin"


class TestInstallRemainingBranches:
    def test_detect_install_method_pip_paths(self, monkeypatch):
        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: None)
        monkeypatch.setattr("siftd.cli.install.sys.prefix", "/venv")
        monkeypatch.setattr("siftd.cli.install.sys.base_prefix", "/base")
        assert detect_install_method() == "pip_venv"

        monkeypatch.setattr("siftd.cli.install.sys.base_prefix", "/venv")

        class _F:
            def locate(self):
                return "/users/me/.local/lib/python/site-packages/siftd/__init__.py"

        class _Dist:
            files = [_F()]

        monkeypatch.setattr("siftd.cli.install.distribution", lambda name: _Dist())
        import site

        monkeypatch.setattr(site, "getusersitepackages", lambda: "/users/me/.local/lib/python/site-packages")
        assert detect_install_method() == "pip_user"

    def test_embed_serve_import_checks(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "fastembed", object())
        monkeypatch.setitem(__import__("sys").modules, "litestar", object())
        from siftd.cli.install import _serve_installed, embed_installed

        assert embed_installed()
        assert _serve_installed()

    def test_run_extra_install_dryrun_and_verify_warning(self, monkeypatch, capsys):
        monkeypatch.setattr("siftd.cli.install.detect_install_method", lambda: "editable")
        monkeypatch.setattr("siftd.cli.install._editable_source_url", lambda: "file:///tmp/src")
        monkeypatch.setattr("siftd.cli.install._install_commands", lambda extra, source_path=None: {"editable": ["pip", "install"]})
        monkeypatch.setattr("siftd.cli.install.install_hint", lambda extra: "pip install -e")

        rc = _run_extra_install(
            SimpleNamespace(dry_run=True),
            "embed",
            is_installed=lambda: False,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 0

        class _Res:
            returncode = 0

        monkeypatch.setattr("siftd.cli.install.subprocess.run", lambda *a, **k: _Res())
        rc = _run_extra_install(
            SimpleNamespace(dry_run=False),
            "embed",
            is_installed=lambda: False,
            already_msg="already",
            success_msg="ok",
        )
        assert rc == 0
        assert "Warning: Installation completed" in capsys.readouterr().err

    def test_install_skill_and_plugin_error_branches(self, tmp_path, monkeypatch):
        # _install_skill: missing bundled files
        monkeypatch.setattr("siftd.cli.install._find_plugin_source", lambda: None)
        assert _install_skill(_make_skill_args()) == 1

        bad = tmp_path / "plugin"
        bad.mkdir()
        monkeypatch.setattr("siftd.cli.install._find_plugin_source", lambda: bad)
        assert _install_skill(_make_skill_args()) == 1

        # _install_plugin: missing source
        monkeypatch.setattr("siftd.cli.install._find_plugin_source", lambda: None)
        assert _install_plugin(_make_args()) == 1

    def test_install_skill_scope_resolution_and_warns(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(tmp_path)

        # build custom harness map to hit ~, /abs, relative and single-scope branches
        monkeypatch.setitem(
            __import__("sys").modules,
            "siftd.skill_gen",
            SimpleNamespace(
                HARNESS_INFO={
                    "one": {"display_name": "One", "scope_dirs": {"user": "~"}, "format": "instructions", "filename": "x.md"},
                    "abs": {"display_name": "Abs", "scope_dirs": {"user": str(tmp_path / "abs")}, "format": "instructions", "filename": "a.md"},
                    "rel": {"display_name": "Rel", "scope_dirs": {"user": "rel-dir"}, "format": "instructions", "filename": "r.md"},
                },
                render_instructions=lambda ref: "content",
            ),
        )

        source = tmp_path / "srcp"
        (source / "skills" / "siftd" / "reference").mkdir(parents=True)
        (source / "skills" / "siftd" / "SKILL.md").write_text("x")
        monkeypatch.setattr("siftd.cli.install._find_plugin_source", lambda: source)

        assert _install_skill(_make_skill_args(harness="one", scope="project")) == 0
        assert (fake_home / "x.md").exists()
        assert _install_skill(_make_skill_args(harness="abs")) == 0
        assert (tmp_path / "abs" / "a.md").exists()
        assert _install_skill(_make_skill_args(harness="rel")) == 0
        assert (tmp_path / "rel-dir" / "r.md").exists()

    def test_cmd_install_dispatch(self, monkeypatch):
        monkeypatch.setattr("siftd.cli.install._install_embed", lambda args: 11)
        monkeypatch.setattr("siftd.cli.install._install_serve", lambda args: 12)
        monkeypatch.setattr("siftd.cli.install._install_skill", lambda args: 13)
        monkeypatch.setattr("siftd.cli.install._install_plugin", lambda args: 14)
        assert cmd_install(SimpleNamespace(extra="embed")) == 11
        assert cmd_install(SimpleNamespace(extra="serve")) == 12
        assert cmd_install(SimpleNamespace(extra="skill")) == 13
        assert cmd_install(SimpleNamespace(extra="plugin")) == 14
