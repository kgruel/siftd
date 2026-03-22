"""Doctor findings cache — save/load fixable findings for `doctor fix`."""

from __future__ import annotations

import json
from pathlib import Path


def _fixes_cache_path() -> Path:
    from siftd.paths import state_dir

    return state_dir() / "doctor-fixes.json"


def save_findings_cache(findings: list) -> Path | None:
    """Save fixable findings to XDG state. Returns path if any saved."""
    fixable = [f for f in findings if f.fix_available and f.fix_command]
    if not fixable:
        clear_findings_cache()
        return None

    path = _fixes_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Deduplicate by fix_command, keep first finding for context
    seen = set()
    entries = []
    for f in fixable:
        if f.fix_command not in seen:
            entries.append({
                "fix_command": f.fix_command,
                "check": f.check,
                "severity": f.severity,
                "message": f.message,
            })
            seen.add(f.fix_command)

    path.write_text(json.dumps(entries, indent=2))
    return path


def load_findings_cache() -> list[dict] | None:
    """Load cached fixable findings. Returns None if no cache exists."""
    path = _fixes_cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_findings_cache() -> None:
    """Remove the findings cache file."""
    path = _fixes_cache_path()
    if path.exists():
        path.unlink()
