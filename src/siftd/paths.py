"""XDG Base Directory paths for siftd.

Follows XDG Base Directory Specification:
- XDG_DATA_HOME (~/.local/share) - database, persistent data
- XDG_CONFIG_HOME (~/.config) - configuration files
- XDG_CACHE_HOME (~/.cache) - cache files
- XDG_STATE_HOME (~/.local/state) - runtime state (sessions, etc.)
"""

import contextlib
import hashlib
import os
import tempfile
import tomllib
from pathlib import Path

APP_NAME = "siftd"


def _get_xdg_path(env_var: str, default: str) -> Path:
    """Get XDG path from environment or use default."""
    return Path(os.environ.get(env_var, default)).expanduser()


def data_dir() -> Path:
    """Return the data directory (~/.local/share/siftd)."""
    base = _get_xdg_path("XDG_DATA_HOME", "~/.local/share")
    return base / APP_NAME


def config_dir() -> Path:
    """Return the config directory (~/.config/siftd)."""
    base = _get_xdg_path("XDG_CONFIG_HOME", "~/.config")
    return base / APP_NAME


def cache_dir() -> Path:
    """Return the cache directory (~/.cache/siftd)."""
    base = _get_xdg_path("XDG_CACHE_HOME", "~/.cache")
    return base / APP_NAME


def state_dir() -> Path:
    """Return the state directory (~/.local/state/siftd)."""
    base = _get_xdg_path("XDG_STATE_HOME", "~/.local/state")
    return base / APP_NAME


def credentials_dir() -> Path:
    """Return the OAuth credentials directory (~/.local/state/siftd/credentials)."""
    return state_dir() / "credentials"


def credential_file(issuer: str) -> Path:
    """Return the stored-credential file for an OIDC issuer.

    Keyed by a hash of the issuer URL (mirrors ``session_id_file``) so issuer
    URLs containing slashes, schemes, or ports collapse to a flat,
    filesystem-safe filename.
    """
    issuer_hash = hashlib.sha256(issuer.encode()).hexdigest()[:12]
    return credentials_dir() / f"{issuer_hash}.json"


def atomic_write_secure(
    path: Path,
    content: str | bytes,
    *,
    file_mode: int = 0o600,
    dir_mode: int = 0o700,
) -> None:
    """Atomically write ``content`` to ``path`` with restrictive permissions.

    Ensures the parent directory exists and is mode ``dir_mode`` (default 0o700),
    writes to a temp file in that same directory at ``file_mode`` (default 0o600),
    then atomically ``os.replace``s the target. Closing the gap where the file
    could be world-readable or observed half-written. Shared by config writes
    and credential storage; the only sanctioned way to persist sensitive files.
    """
    data = content.encode() if isinstance(content, str) else content
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(dir_mode)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        os.write(fd, data)
        os.fchmod(fd, file_mode)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def session_id_file(workspace_path: str) -> Path:
    """Return the session ID file for a workspace.

    Uses a hash of the workspace path to create a unique directory:
    ~/.local/state/siftd/sessions/<workspace-hash>/session-id
    """
    workspace_hash = hashlib.sha256(workspace_path.encode()).hexdigest()[:12]
    return state_dir() / "sessions" / workspace_hash / "session-id"


def queries_dir() -> Path:
    """Return the queries directory (~/.config/siftd/queries)."""
    return config_dir() / "queries"


def adapters_dir() -> Path:
    """Return the adapters directory (~/.config/siftd/adapters)."""
    return config_dir() / "adapters"


def formatters_dir() -> Path:
    """Return the formatters directory (~/.config/siftd/formatters)."""
    return config_dir() / "formatters"


def config_file() -> Path:
    """Return the config file path (~/.config/siftd/config.toml)."""
    return config_dir() / "config.toml"


def db_path() -> Path:
    """Return the database path, checking config override first.

    Reads ``[db].path`` directly from config.toml using stdlib ``tomllib`` so
    this module stays independent from ``siftd.config``.
    """
    cfg = config_file()
    if cfg.exists():
        try:
            doc = tomllib.loads(cfg.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            doc = None
        if isinstance(doc, dict):
            db = doc.get("db")
            if isinstance(db, dict) and "path" in db:
                value = db.get("path")
                if value is not None and not isinstance(value, (dict, list)):
                    override = str(value)
                    if override:
                        return Path(override).expanduser()
    return data_dir() / "siftd.db"


def embeddings_db_path() -> Path:
    """Return the embeddings database path (derived data, separate from main DB)."""
    return data_dir() / "embeddings.db"


def inbox_dir() -> Path:
    """Return the sync inbox directory (~/.local/share/siftd/inbox)."""
    return data_dir() / "inbox"


def ensure_dirs() -> None:
    """Create all XDG directories if they don't exist."""
    data_dir().mkdir(parents=True, exist_ok=True)
    config_dir().mkdir(parents=True, exist_ok=True)
    queries_dir().mkdir(parents=True, exist_ok=True)
    adapters_dir().mkdir(parents=True, exist_ok=True)
    formatters_dir().mkdir(parents=True, exist_ok=True)
    cache_dir().mkdir(parents=True, exist_ok=True)
    inbox_dir().mkdir(parents=True, exist_ok=True)
