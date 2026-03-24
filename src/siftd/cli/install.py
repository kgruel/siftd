"""CLI handler for 'siftd install' — install optional extras and bundled components."""

import argparse
import importlib.resources
import json
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path


def _editable_source_url() -> str | None:
    """Return the file:// URL from direct_url.json if this is an editable install."""
    try:
        dist = distribution("siftd")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            data = json.loads(direct_url_text)
            if data.get("dir_info", {}).get("editable"):
                return data.get("url", "")
    except (FileNotFoundError, TypeError, json.JSONDecodeError):
        pass
    return None


def detect_install_method() -> str:
    """Detect how siftd was installed.

    Returns one of: 'uv_tool', 'uv_tool_editable', 'pipx', 'pip_venv',
    'pip_user', 'editable', 'unknown'
    """
    # Check tool-managed venvs first — these need their own install flow
    # even when the install is editable (e.g. `uv tool install -e .`)
    venv_path = sys.prefix
    if "/uv/tools/" in venv_path or "\\uv\\tools\\" in venv_path:
        if _editable_source_url():
            return "uv_tool_editable"
        return "uv_tool"
    if "/pipx/venvs/" in venv_path or "\\pipx\\venvs\\" in venv_path:
        return "pipx"
    if "/Cellar/" in venv_path or "/homebrew/" in venv_path.lower():
        return "brew"

    # Check editable via PEP 610 direct_url.json
    if _editable_source_url():
        return "editable"

    # Regular venv vs user install
    if sys.prefix != sys.base_prefix:
        return "pip_venv"

    # Check if in user site-packages
    import site

    try:
        dist = distribution("siftd")
        files = dist.files
        if files:
            location = str(Path(files[0].locate()).parent)
            user_site = site.getusersitepackages()
            if user_site and location.startswith(user_site):
                return "pip_user"
    except Exception:
        pass

    return "unknown"


def embed_installed() -> bool:
    """Check if embed dependencies are already installed."""
    from importlib.util import find_spec

    return find_spec("fastembed") is not None


def _pip_cmd() -> str:
    """Return 'uv pip' if uv is on PATH, else 'pip'."""
    return "uv pip" if shutil.which("uv") else "pip"


def install_hint(extra: str) -> str:
    """Return the install command appropriate for the detected install method.

    Produces a human-readable command string like ``uv tool install 'siftd[serve]' --force``
    that matches how the user originally installed siftd.
    """
    method = detect_install_method()
    pkg = f"siftd[{extra}]"
    pip = _pip_cmd()

    if method == "uv_tool_editable":
        source_url = _editable_source_url() or ""
        source_path = source_url[7:] if source_url.startswith("file://") else "."
        return f"uv tool install -e '{source_path}[{extra}]' --force"

    templates = {
        "uv_tool": f"uv tool install '{pkg}' --force",
        "pipx": f"pipx install '{pkg}' --force",
        "brew": f"$(brew --prefix siftd)/libexec/bin/python -m pip install '{pkg}'",
        "pip_venv": f"{pip} install '{pkg}'",
        "pip_user": f"{pip} install --user '{pkg}'",
        "editable": f"{pip} install -e '.[{extra}]'",
    }
    return templates.get(method, f"{pip} install '{pkg}'")


# Human-readable labels
METHOD_LABELS: dict[str, str] = {
    "uv_tool": "uv tool",
    "uv_tool_editable": "uv tool (editable)",
    "pipx": "pipx",
    "brew": "Homebrew",
    "pip_venv": "pip (venv)",
    "pip_user": "pip (user)",
    "editable": "editable install",
}


def _install_commands(extra: str, *, source_path: str | None = None) -> dict[str, list[str]]:
    """Build subprocess command lists for installing a given extra."""
    pkg = f"siftd[{extra}]"
    pip = _pip_cmd().split()  # ["uv", "pip"] or ["pip"]
    cmds: dict[str, list[str]] = {
        "uv_tool": ["uv", "tool", "install", pkg, "--force"],
        "pipx": ["pipx", "install", pkg, "--force"],
        "brew": [sys.prefix + "/bin/python", "-m", "pip", "install", pkg],
        "pip_venv": [*pip, "install", pkg],
        "pip_user": [*pip, "install", "--user", pkg],
        "editable": [*pip, "install", "-e", f".[{extra}]"],
    }
    if source_path:
        cmds["uv_tool_editable"] = ["uv", "tool", "install", "-e", f"{source_path}[{extra}]", "--force"]
    return cmds


def _run_extra_install(args, extra: str, *, is_installed, already_msg: str, success_msg: str) -> int:
    """Common flow: detect method, run subprocess, verify."""
    if is_installed():
        print(already_msg)
        return 0

    method = detect_install_method()
    method_label = METHOD_LABELS.get(method, method)

    if method == "unknown":
        print("Could not detect installation method.")
        print()
        print(f"Try: {install_hint(extra)}")
        return 1

    # Resolve source path for editable installs
    source_url = _editable_source_url() or ""
    source_path = source_url[7:] if source_url.startswith("file://") else None

    cmds = _install_commands(extra, source_path=source_path)
    cmd = cmds[method]

    # For plain editable installs, we need to be in the project directory
    cwd = None
    if method == "editable":
        cwd = source_path
        if not cwd:
            print("Detected editable install but could not find project root.")
            print()
            print("Run from your project directory:")
            print(f"  pip install -e '.[{extra}]'")
            return 1

    cmd_str = install_hint(extra)

    if args.dry_run:
        print(f"Detected: {method_label}")
        print(f"Would run: {cmd_str}")
        if cwd:
            print(f"In directory: {cwd}")
        return 0

    print(f"Detected: {method_label}")
    print(f"Running: {cmd_str}")
    if cwd:
        print(f"In directory: {cwd}")
    print()

    try:
        result = subprocess.run(cmd, cwd=cwd, check=False)
        if result.returncode != 0:
            print()
            print(f"Command failed with exit code {result.returncode}")
            print(f"You may need to run manually: {cmd_str}")
            return result.returncode
    except FileNotFoundError:
        pkg_manager = cmd[0]
        print(f"Error: '{pkg_manager}' not found in PATH")
        print()
        if pkg_manager == "uv":
            print("Install uv: https://docs.astral.sh/uv/getting-started/installation/")
        elif pkg_manager == "pipx":
            print("Install pipx: https://pipx.pypa.io/stable/installation/")
        return 1

    # Verify
    print()
    if is_installed():
        print(success_msg)
    else:
        print(f"Warning: Installation completed but [{extra}] dependencies not detected.", file=sys.stderr)
        print("You may need to restart your shell or check for errors above.", file=sys.stderr)

    return 0


def _install_embed(args) -> int:
    """Install embed optional dependencies."""
    return _run_extra_install(
        args,
        "embed",
        is_installed=embed_installed,
        already_msg=(
            "Embed dependencies already installed.\n\n"
            "Semantic search is ready:\n"
            "  siftd search --index    # build embeddings index\n"
            '  siftd search "query"    # search'
        ),
        success_msg=(
            "Embed dependencies installed successfully.\n\n"
            "Next steps:\n"
            "  siftd search --index    # build embeddings index\n"
            '  siftd search "query"    # search'
        ),
    )


def _serve_installed() -> bool:
    """Check if serve dependencies are already installed."""
    from importlib.util import find_spec

    return find_spec("litestar") is not None


def _install_serve(args) -> int:
    """Install serve optional dependencies."""
    return _run_extra_install(
        args,
        "serve",
        is_installed=_serve_installed,
        already_msg=(
            "Serve dependencies already installed.\n\n"
            "Start the server:\n"
            "  siftd serve"
        ),
        success_msg=(
            "Serve dependencies installed successfully.\n\n"
            "Start the server:\n"
            "  siftd serve"
        ),
    )


def _find_plugin_source() -> Path | None:
    """Locate the bundled plugin directory.

    Checks importlib.resources first (wheel installs), then falls back
    to repo root for editable installs.
    """
    ref = importlib.resources.files("siftd").joinpath("plugin")
    # as_file works for both wheel (returns extracted path) and editable (returns fs path)
    with importlib.resources.as_file(ref) as path:
        if path.is_dir():
            return path

    # Editable install fallback: plugin/ lives at repo root, not src/siftd/plugin/
    if detect_install_method() == "editable":
        try:
            dist = distribution("siftd")
            direct_url_text = dist.read_text("direct_url.json")
            if direct_url_text:
                data = json.loads(direct_url_text)
                url = data.get("url", "")
                if url.startswith("file://"):
                    repo_root = Path(url[7:])
                    candidate = repo_root / "plugin"
                    if candidate.is_dir():
                        return candidate
        except Exception:
            pass

    return None


def _install_skill(args) -> int:
    """Install the /siftd skill for the specified harness.

    Supports multiple harnesses. Claude Code and Pi get the structured
    skill (SKILL.md + reference/). Other harnesses get a rendered
    plain-markdown instructions file.
    """
    from siftd.skill_gen import HARNESS_INFO, render_instructions

    source_path = _find_plugin_source()
    if source_path is None:
        print("Error: Bundled plugin files not found in this installation.", file=sys.stderr)
        return 1

    skill_source = source_path / "skills" / "siftd"
    if not skill_source.is_dir():
        print("Error: Skill directory not found in bundled plugin.", file=sys.stderr)
        return 1

    harness = getattr(args, "harness", None) or "claude_code"
    scope = getattr(args, "scope", "user")

    if harness not in HARNESS_INFO:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        print(f"Available: {', '.join(HARNESS_INFO)}", file=sys.stderr)
        return 1

    info = HARNESS_INFO[harness]
    scope_dirs = info.get("scope_dirs", {})

    # If harness has exactly one scope, use it regardless of what --scope says
    if len(scope_dirs) == 1:
        scope = next(iter(scope_dirs))

    if scope not in scope_dirs:
        available = ", ".join(scope_dirs)
        print(f"{info['display_name']} only supports scope: {available}", file=sys.stderr)
        return 1

    raw_target = scope_dirs[scope]
    if raw_target.startswith("~/"):
        # Use Path.home() so monkeypatching works in tests
        base = Path.home() / raw_target[2:]
    elif raw_target == "~":
        base = Path.home()
    elif raw_target.startswith("/"):
        base = Path(raw_target)
    else:
        # Relative path — resolve against cwd
        base = Path.cwd() / raw_target

    fmt = info.get("format", "instructions")

    if fmt == "skill":
        # Structured: copy SKILL.md + reference/ directory
        target = base

        if args.dry_run:
            print(f"Harness: {info['display_name']}")
            print(f"Source:  {skill_source}")
            print(f"Target:  {target}")
            print(f"Scope:   {scope}")
            return 0

        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_source, target)

        if not (target / "SKILL.md").exists():
            print("Warning: Skill copied but SKILL.md not found.", file=sys.stderr)
            return 1

        # Check for Claude Code plugin overlap
        if harness == "claude_code":
            plugin_user = Path.home() / ".claude" / "plugins" / "siftd"
            plugin_project = Path.cwd() / ".claude" / "plugins" / "siftd"
            if plugin_user.exists() or plugin_project.exists():
                print("Note: the plugin is also installed and already includes this skill.", file=sys.stderr)
                print("You may want to remove one to avoid duplicate /siftd skill entries.", file=sys.stderr)

        print(f"Installed skill to {target}")
        print(f"Harness: {info['display_name']}")

    else:
        # Instructions: render plain markdown to a single file
        filename = info.get("filename", "siftd.md")
        target = base / filename

        reference_dir = skill_source / "reference"

        if args.dry_run:
            print(f"Harness: {info['display_name']}")
            print(f"Target:  {target}")
            print(f"Scope:   {scope}")
            return 0

        content = render_instructions(reference_dir)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

        print(f"Installed instructions to {target}")
        print(f"Harness: {info['display_name']}")

    return 0


def _install_plugin(args) -> int:
    """Install the bundled Claude Code plugin (hooks + commands + skill)."""
    source_path = _find_plugin_source()
    if source_path is None:
        print("Error: Bundled plugin files not found in this installation.", file=sys.stderr)
        return 1

    # Determine target directory
    scope = getattr(args, "scope", "user")
    if scope == "project":
        target = Path.cwd() / ".claude" / "plugins" / "siftd"
    else:
        target = Path.home() / ".claude" / "plugins" / "siftd"

    if args.dry_run:
        print(f"Source: {source_path}")
        print(f"Target: {target}")
        print(f"Scope:  {scope}")
        return 0

    # Clean replace — remove stale files (symlinks from dev-mode --plugin-dir)
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_path, target)

    # Verify manifest exists
    manifest = target / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        print("Warning: Plugin copied but plugin.json not found at expected location.", file=sys.stderr)
        return 1

    # Check if standalone skill is also installed and clean it up
    skill_user = Path.home() / ".claude" / "skills" / "siftd"
    skill_project = Path.cwd() / ".claude" / "skills" / "siftd"
    for stale_skill in (skill_user, skill_project):
        if stale_skill.is_symlink():
            stale_skill.unlink()
            print(f"Removed standalone skill symlink at {stale_skill} (plugin includes it)")
        elif stale_skill.exists():
            shutil.rmtree(stale_skill)
            print(f"Removed standalone skill at {stale_skill} (plugin includes it)")

    print(f"Installed plugin to {target}")
    print(f"Scope: {scope}")
    return 0


def cmd_install(args) -> int:
    """Install optional dependencies or bundled components."""
    if not args.extra:
        from siftd.skill_gen import HARNESS_INFO

        print("Available components:\n")
        print("  skill    Teach your agent to use siftd (supports multiple harnesses)")
        print("  plugin   Full Claude Code plugin: skill + hooks + commands")
        print("  embed    Semantic search dependencies")
        print("  serve    HTTP server dependencies")
        print()
        print("Supported harnesses for 'skill':")
        for key, info in HARNESS_INFO.items():
            print(f"  {key:<14} {info['display_name']}")
        print()
        print("Usage:")
        print("  siftd install skill                          # Claude Code (default)")
        print("  siftd install skill --harness codex_cli      # Codex CLI")
        print("  siftd install skill --harness gemini_cli     # Gemini CLI")
        print("  siftd install plugin                         # Full Claude Code plugin")
        return 0

    if args.extra == "embed":
        return _install_embed(args)
    elif args.extra == "serve":
        return _install_serve(args)
    elif args.extra == "skill":
        return _install_skill(args)
    elif args.extra == "plugin":
        return _install_plugin(args)

    # argparse choices should prevent reaching here
    print(f"Unknown extra: {args.extra}")
    return 1


def build_install_parser(subparsers) -> None:
    """Add the 'install' subparser to the CLI."""
    p_install = subparsers.add_parser(
        "install",
        help="Install optional dependencies or bundled components",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd install skill                         # Claude Code skill (default)
  siftd install skill --harness codex_cli     # Codex CLI instructions
  siftd install skill --harness gemini_cli    # Gemini CLI instructions
  siftd install skill --harness pi_agent      # Pi Agent skill
  siftd install plugin                        # full Claude Code plugin
  siftd install plugin --scope project        # plugin for current project only
  siftd install embed                         # semantic search dependencies
  siftd install serve                         # HTTP server dependencies""",
    )
    p_install.add_argument(
        "extra",
        nargs="?",
        default=None,
        choices=["embed", "serve", "skill", "plugin"],
        help="Component to install (skill: search workflow, plugin: skill + hooks + commands, embed: semantic search, serve: HTTP server)",
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be run without executing",
    )
    p_install.add_argument(
        "--scope",
        choices=["user", "project"],
        default="user",
        help="Install scope: user (home dir) or project (current dir)",
    )
    p_install.add_argument(
        "--harness",
        default=None,
        metavar="NAME",
        help="Target harness for skill install (claude_code, codex_cli, gemini_cli, pi_agent, copilot_cli, aider)",
    )
    p_install.set_defaults(func=cmd_install)
