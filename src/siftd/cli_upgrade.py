"""CLI handler for 'siftd upgrade' — check for and install updates."""

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from siftd.cli_common import _get_version
from siftd.cli_install import METHOD_LABELS, detect_install_method
from siftd.paths import state_dir

_CHECK_INTERVAL_S = 86400  # 24 hours
_CACHE_FILE = "update-check.json"
_PYPI_URL = "https://pypi.org/pypi/siftd/json"


# ---------------------------------------------------------------------------
# Version check cache
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    return state_dir() / _CACHE_FILE


def _read_cache() -> dict | None:
    path = _cache_path()
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "latest" in data and "checked_at" in data:
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _write_cache(latest: str) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "latest": latest,
        "checked_at": datetime.now(UTC).isoformat(),
    }))


def _cache_is_fresh() -> bool:
    cache = _read_cache()
    if cache is None:
        return False
    try:
        checked = datetime.fromisoformat(cache["checked_at"])
        age = (datetime.now(UTC) - checked).total_seconds()
        return age < _CHECK_INTERVAL_S
    except (ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# PyPI version fetch
# ---------------------------------------------------------------------------


def _fetch_latest_version() -> str | None:
    """Fetch the latest version from PyPI. Returns None on failure."""
    try:
        from urllib.request import urlopen

        with urlopen(_PYPI_URL, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("info", {}).get("version")
    except Exception:
        return None


def _is_newer(latest: str, current: str) -> bool:
    """Compare version strings. Returns True if latest > current."""
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        pass
    # Fallback: tuple comparison on dot-separated integers
    try:
        lat = tuple(int(x) for x in latest.split("."))
        cur = tuple(int(x) for x in current.split("."))
        return lat > cur
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Background check (non-blocking, fire-and-forget)
# ---------------------------------------------------------------------------


def _background_check() -> None:
    """Fetch latest version and update the cache. Runs in a daemon thread."""
    latest = _fetch_latest_version()
    if latest:
        _write_cache(latest)


def maybe_start_check() -> None:
    """Start a background version check if the cache is stale.

    Called from cli.py after command dispatch. Non-blocking — the thread
    is daemonic so it won't prevent process exit.
    """
    if os.environ.get("SIFTD_NO_UPDATE_CHECK"):
        return
    # Lazy import to avoid circular; config reads paths
    from siftd.config import get_config

    if get_config("update.check") == "false":
        return
    if _cache_is_fresh():
        return
    t = threading.Thread(target=_background_check, daemon=True)
    t.start()


def maybe_print_notice() -> None:
    """Print an update notice to stderr if a newer version is cached.

    Only prints if stderr is a TTY and the user hasn't disabled checks.
    """
    if not sys.stderr.isatty():
        return
    if os.environ.get("SIFTD_NO_UPDATE_CHECK"):
        return
    from siftd.config import get_config

    if get_config("update.check") == "false":
        return

    cache = _read_cache()
    if cache is None:
        return
    current = _get_version()
    latest = cache.get("latest", "")
    if latest and _is_newer(latest, current):
        print(
            f"\nsiftd {latest} available (current: {current})"
            " — run `siftd upgrade` to update",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Upgrade command
# ---------------------------------------------------------------------------


def _upgrade_command(method: str) -> list[str] | None:
    """Return the shell command to run the upgrade."""
    uv = shutil.which("uv")
    pip = "uv pip" if uv else "pip"
    commands: dict[str, list[str]] = {
        "uv_tool": ["uv", "tool", "upgrade", "siftd"],
        "pipx": ["pipx", "upgrade", "siftd"],
        "brew": ["brew", "upgrade", "siftd"],
        "pip_venv": [*pip.split(), "install", "--upgrade", "siftd"],
        "pip_user": [*pip.split(), "install", "--upgrade", "--user", "siftd"],
    }
    return commands.get(method)


def cmd_upgrade(args) -> int:
    """Check for updates and upgrade siftd."""
    current = _get_version()
    method = detect_install_method()
    method_label = METHOD_LABELS.get(method, method)

    check_only = getattr(args, "check", False)

    print(f"siftd {current} (installed via {method_label})", file=sys.stderr)

    # Always fetch fresh when user explicitly runs upgrade
    print("Checking PyPI for updates...", file=sys.stderr)
    latest = _fetch_latest_version()
    if latest is None:
        print("Error: Could not reach PyPI", file=sys.stderr)
        return 1

    _write_cache(latest)

    if not _is_newer(latest, current):
        print("Already up to date.", file=sys.stderr)
        return 0

    print(f"Update available: {current} → {latest}", file=sys.stderr)

    if check_only:
        return 0

    if method == "editable":
        print("Editable install detected — upgrade manually (git pull).", file=sys.stderr)
        return 0

    cmd = _upgrade_command(method)
    if cmd is None:
        print(f"Don't know how to upgrade for install method '{method}'.", file=sys.stderr)
        print("Try: pip install --upgrade siftd", file=sys.stderr)
        return 1

    print(f"Running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd)
    return result.returncode


def build_upgrade_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "upgrade",
        help="Check for and install updates",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without installing",
    )
    p.set_defaults(func=cmd_upgrade)
