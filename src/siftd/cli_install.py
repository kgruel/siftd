"""CLI handler for 'siftd install' — install optional dependencies."""

import argparse
import json
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
    "pip_venv": ["pip", "install", "siftd[embed]"],
    "pip_user": ["pip", "install", "--user", "siftd[embed]"],
    "editable": ["pip", "install", "-e", ".[embed]"],
}

# Human-readable labels
METHOD_LABELS: dict[str, str] = {
    "uv_tool": "uv tool",
    "pipx": "pipx",
    "pip_venv": "pip (venv)",
    "pip_user": "pip (user)",
    "editable": "editable install",
}


def cmd_install(args) -> int:
    """Install optional dependencies."""
    extra = args.extra

    if extra != "embed":
        print(f"Unknown extra: {extra}")
        print("Available extras: embed")
        return 1

    # Check if already installed
    if embed_installed():
        print("Embed dependencies already installed.")
        print()
        print("Semantic search is ready:")
        print("  siftd ask --index    # build embeddings index")
        print('  siftd ask "query"    # search')
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
        print("  uv tool install siftd[embed] --force")
        print()
        print("  # If installed via pipx:")
        print("  pipx install siftd[embed] --force")
        print()
        print("  # If installed via pip in a venv:")
        print("  pip install siftd[embed]")
        print()
        print("  # If installed via pip --user:")
        print("  pip install --user siftd[embed]")
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

    cmd_str = " ".join(cmd)

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
        print("  siftd ask --index    # build embeddings index")
        print('  siftd ask "query"    # search')
    else:
        print("Warning: Installation completed but embed dependencies not detected.", file=sys.stderr)
        print("You may need to restart your shell or check for errors above.", file=sys.stderr)

    return 0


def build_install_parser(subparsers) -> None:
    """Add the 'install' subparser to the CLI."""
    p_install = subparsers.add_parser(
        "install",
        help="Install optional dependencies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd install embed             # install semantic search dependencies
  siftd install embed --dry-run   # show what would be installed""",
    )
    p_install.add_argument(
        "extra",
        choices=["embed"],
        help="Optional extra to install",
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be run without executing",
    )
    p_install.set_defaults(func=cmd_install)
