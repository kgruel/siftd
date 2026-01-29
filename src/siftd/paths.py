"""XDG Base Directory paths for siftd.

Follows XDG Base Directory Specification:
- XDG_DATA_HOME (~/.local/share) - database, persistent data
- XDG_CONFIG_HOME (~/.config) - configuration files
- XDG_CACHE_HOME (~/.cache) - cache files
"""

import os
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
    """Return the default database path."""
    return data_dir() / "siftd.db"


def embeddings_db_path() -> Path:
    """Return the embeddings database path (derived data, separate from main DB)."""
    return data_dir() / "embeddings.db"


def ensure_dirs() -> None:
    """Create all XDG directories if they don't exist."""
    data_dir().mkdir(parents=True, exist_ok=True)
    config_dir().mkdir(parents=True, exist_ok=True)
    queries_dir().mkdir(parents=True, exist_ok=True)
    adapters_dir().mkdir(parents=True, exist_ok=True)
    formatters_dir().mkdir(parents=True, exist_ok=True)
    cache_dir().mkdir(parents=True, exist_ok=True)
