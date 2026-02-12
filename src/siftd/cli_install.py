"""CLI handler for 'siftd install' — install optional extras and bundled components."""

import argparse
import importlib.resources
import json
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path


def detect_install_method() -> str:
    """Detect how siftd was installed.

    Returns one of: 'uv_tool', 'pipx', 'pip_venv', 'pip_user', 'editable', 'unknown'
    """
    # Check editable first via PEP 610 direct_url.json
    try:
        dist = distribution("siftd")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            data = json.loads(direct_url_text)
            if data.get("dir_info", {}).get("editable"):
                return "editable"
    except (FileNotFoundError, TypeError, json.JSONDecodeError):
        pass

    # Check path patterns in sys.prefix
    venv_path = sys.prefix
    if "/uv/tools/" in venv_path or "\\uv\\tools\\" in venv_path:
        return "uv_tool"
    if "/pipx/venvs/" in venv_path or "\\pipx\\venvs\\" in venv_path:
        return "pipx"
    if "/Cellar/" in venv_path or "/homebrew/" in venv_path.lower():
        return "brew"

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
    try:
        import fastembed  # noqa: F401

        return True
    except ImportError:
        return False


# Command templates for each install method
INSTALL_COMMANDS: dict[str, list[str]] = {
    "uv_tool": ["uv", "tool", "install", "siftd[embed]", "--force"],
    "pipx": ["pipx", "install", "siftd[embed]", "--force"],
    "brew": [sys.prefix + "/bin/python", "-m", "pip", "install", "siftd[embed]"],
    "pip_venv": ["pip", "install", "siftd[embed]"],
    "pip_user": ["pip", "install", "--user", "siftd[embed]"],
    "editable": ["pip", "install", "-e", ".[embed]"],
}

# Human-readable labels
METHOD_LABELS: dict[str, str] = {
    "uv_tool": "uv tool",
    "pipx": "pipx",
    "brew": "Homebrew",
    "pip_venv": "pip (venv)",
    "pip_user": "pip (user)",
    "editable": "editable install",
}


def _install_embed(args) -> int:
    """Install embed optional dependencies."""
    # Check if already installed
    if embed_installed():
        print("Embed dependencies already installed.")
        print()
        print("Semantic search is ready:")
        print("  siftd search --index    # build embeddings index")
        print('  siftd search "query"    # search')
        return 0

    # Detect installation method
    method = detect_install_method()
    method_label = METHOD_LABELS.get(method, method)

    if method == "unknown":
        print("Could not detect installation method.")
        print()
        print("Try one of these commands:")
        print()
        print("  # If installed via uv tool:")
        print("  uv tool install 'siftd[embed]' --force")
        print()
        print("  # If installed via pipx:")
        print("  pipx install 'siftd[embed]' --force")
        print()
        print("  # If installed via pip in a venv:")
        print("  pip install 'siftd[embed]'")
        print()
        print("  # If installed via pip --user:")
        print("  pip install --user 'siftd[embed]'")
        return 1

    cmd = INSTALL_COMMANDS[method]

    # For editable installs, we need to be in the project directory
    cwd = None
    if method == "editable":
        # Try to find project root from direct_url.json
        try:
            dist = distribution("siftd")
            direct_url_text = dist.read_text("direct_url.json")
            if direct_url_text:
                data = json.loads(direct_url_text)
                url = data.get("url", "")
                if url.startswith("file://"):
                    cwd = url[7:]  # Strip file://
        except Exception:
            pass

        if not cwd:
            print("Detected editable install but could not find project root.")
            print()
            print("Run from your project directory:")
            print("  pip install -e '.[embed]'")
            return 1

    if method == "brew":
        cmd_str = "$(brew --prefix siftd)/libexec/bin/python -m pip install 'siftd[embed]'"
    else:
        cmd_str = " ".join(
            f"'{c}'" if "[" in c else c for c in cmd
        )

    if args.dry_run:
        print(f"Detected: {method_label}")
        print(f"Would run: {cmd_str}")
        if cwd:
            print(f"In directory: {cwd}")
        return 0

    # Execute the command
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
        # Package manager not found
        pkg_manager = cmd[0]
        print(f"Error: '{pkg_manager}' not found in PATH")
        print()
        if pkg_manager == "uv":
            print("Install uv: https://docs.astral.sh/uv/getting-started/installation/")
        elif pkg_manager == "pipx":
            print("Install pipx: https://pipx.pypa.io/stable/installation/")
        return 1

    # Verify installation
    print()
    if embed_installed():
        print("Embed dependencies installed successfully.")
        print()
        print("Next steps:")
        print("  siftd search --index    # build embeddings index")
        print('  siftd search "query"    # search')
    else:
        print("Warning: Installation completed but embed dependencies not detected.", file=sys.stderr)
        print("You may need to restart your shell or check for errors above.", file=sys.stderr)

    return 0


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


def _install_plugin(args) -> int:
    """Install the bundled Claude Code plugin."""
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

    print(f"Installed plugin to {target}")
    print(f"Scope: {scope}")
    return 0


def cmd_install(args) -> int:
    """Install optional dependencies or bundled components."""
    if args.extra == "embed":
        return _install_embed(args)
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
  siftd install embed             # install semantic search dependencies
  siftd install embed --dry-run   # show what would be installed
  siftd install plugin            # install Claude Code plugin (user scope)
  siftd install plugin --scope project  # install for current project only""",
    )
    p_install.add_argument(
        "extra",
        choices=["embed", "plugin"],
        help="Component to install (embed: semantic search deps, plugin: Claude Code plugin)",
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
        help="Plugin install scope: user (~/.claude/plugins/) or project (.claude/plugins/)",
    )
    p_install.set_defaults(func=cmd_install)
