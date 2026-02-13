"""User configuration management for siftd.

Config file location: ~/.config/siftd/config.toml

Example config:
    [search]
    formatter = "verbose"
"""

import sys
from typing import cast

import tomlkit
import tomlkit.exceptions
from tomlkit import TOMLDocument
from tomlkit.container import Container

from siftd.paths import config_dir, config_file


def load_config() -> TOMLDocument:
    """Load config from file, returning empty document if missing or invalid."""
    path = config_file()
    if not path.exists():
        return tomlkit.document()

    try:
        return tomlkit.parse(path.read_text())
    except tomlkit.exceptions.TOMLKitError as e:
        print(f"Warning: Invalid config file {path}: {e}", file=sys.stderr)
        return tomlkit.document()


def get_config(key: str) -> str | None:
    """Get config value by dotted key path (e.g., 'ask.formatter').

    Returns None if key doesn't exist.
    """
    doc = load_config()
    parts = key.split(".")

    current = doc
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]

    # Return string representation for non-container values
    if isinstance(current, (dict, list)):
        return None
    return str(current) if current is not None else None


def _coerce_value(value: str) -> str | bool:
    """Coerce CLI string value to appropriate TOML type.

    Detects "true"/"false" (case-insensitive) as booleans.
    Everything else passes through as string.
    """
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


def set_config(key: str, value: str) -> None:
    """Set config value by dotted key path (e.g., 'ask.formatter').

    Creates intermediate tables as needed. Preserves existing comments and formatting.
    """
    path = config_file()

    # Load existing or create new
    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text())
        except tomlkit.exceptions.TOMLKitError:
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    parts = key.split(".")
    current = doc

    # Navigate/create intermediate tables
    for part in parts[:-1]:
        if part not in current:
            current[part] = tomlkit.table()
        current = current[part]

    # Set the final value (with type coercion)
    cast(Container, current)[parts[-1]] = _coerce_value(value)

    # Ensure config directory exists and write
    config_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))


def get_search_defaults() -> dict:
    """Get default values for 'siftd search' command from config.

    Returns dict with keys matching argparse attribute names.
    Only includes values that are set in config.
    """
    doc = load_config()
    defaults = {}

    search_config = doc.get("search", {})
    if isinstance(search_config, dict):
        if "formatter" in search_config:
            defaults["format"] = str(search_config["formatter"])

    return defaults


def get_query_defaults() -> dict:
    """Get default values for 'siftd query' command from config.

    Reads [query] section. Returns dict with int-valued keys only
    (limit, chars, tool_chars). Non-int values are silently skipped.
    """
    doc = load_config()
    defaults = {}

    query_config = doc.get("query", {})
    if isinstance(query_config, dict):
        for key in ("limit", "chars", "tool_chars"):
            raw = query_config.get(key)
            if raw is not None:
                try:
                    defaults[key] = int(raw)
                except (ValueError, TypeError):
                    pass  # skip non-int values

    return defaults


def get_adapter_locations(name: str) -> list[str] | None:
    """Get configured discovery locations for an adapter.

    Reads [adapters.<name>].locations as a TOML array.
    Returns None if unconfigured.
    """
    doc = load_config()
    adapters_config = doc.get("adapters", {})
    if not isinstance(adapters_config, dict):
        return None
    adapter_config = adapters_config.get(name, {})
    if not isinstance(adapter_config, dict):
        return None
    locations = adapter_config.get("locations")
    if isinstance(locations, list):
        return [str(loc) for loc in locations]
    return None


def get_sync_remotes() -> list[dict]:
    """Get all configured sync remotes.

    Reads from [sync.remotes.*] sections in config.toml.

    Returns list of dicts with keys: name, host, path, last_push.
    """
    doc = load_config()
    sync_config = doc.get("sync", {})
    if not isinstance(sync_config, dict):
        return []
    remotes_config = sync_config.get("remotes", {})
    if not isinstance(remotes_config, dict):
        return []

    remotes = []
    for name, cfg in remotes_config.items():
        if not isinstance(cfg, dict):
            continue
        remotes.append({
            "name": name,
            "host": cfg.get("host"),
            "path": str(cfg.get("path", "")),
            "last_push": cfg.get("last_push"),
        })
    return remotes


def get_sync_remote(name: str) -> dict | None:
    """Get a single sync remote by name.

    Returns dict with keys: name, host, path, last_push. Or None if not found.
    """
    for remote in get_sync_remotes():
        if remote["name"] == name:
            return remote
    return None


def set_sync_remote(name: str, host: str | None, path: str) -> None:
    """Create or update a sync remote in config.

    Example TOML:
        [sync.remotes.alcove]
        host = "alcove"
        path = "/data/siftd/team.db"
    """
    cfg_path = config_file()

    if cfg_path.exists():
        try:
            doc = tomlkit.parse(cfg_path.read_text())
        except tomlkit.exceptions.TOMLKitError:
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    # Navigate/create sync.remotes.<name>
    if "sync" not in doc:
        doc["sync"] = tomlkit.table()
    sync_tbl = cast(Container, doc["sync"])
    if "remotes" not in sync_tbl:
        sync_tbl["remotes"] = tomlkit.table()
    remotes_tbl = cast(Container, sync_tbl["remotes"])
    if name not in remotes_tbl:
        remotes_tbl[name] = tomlkit.table()

    remote_tbl = cast(Container, remotes_tbl[name])
    if host is not None:
        remote_tbl["host"] = host
    elif "host" in remote_tbl:
        del remote_tbl["host"]
    remote_tbl["path"] = path

    config_dir().mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(tomlkit.dumps(doc))


def remove_sync_remote(name: str) -> bool:
    """Remove a sync remote from config.

    Returns True if the remote existed and was removed.
    """
    cfg_path = config_file()
    if not cfg_path.exists():
        return False

    try:
        doc = tomlkit.parse(cfg_path.read_text())
    except tomlkit.exceptions.TOMLKitError:
        return False

    sync_config = doc.get("sync")
    if not isinstance(sync_config, dict):
        return False
    remotes_config = sync_config.get("remotes")
    if not isinstance(remotes_config, dict):
        return False
    if name not in remotes_config:
        return False

    del remotes_config[name]
    cfg_path.write_text(tomlkit.dumps(doc))
    return True


def update_last_push(name: str, timestamp: str) -> None:
    """Write last_push timestamp for a sync remote."""
    cfg_path = config_file()

    if cfg_path.exists():
        try:
            doc = tomlkit.parse(cfg_path.read_text())
        except tomlkit.exceptions.TOMLKitError:
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    sync_config = doc.get("sync")
    if not isinstance(sync_config, dict):
        return
    remotes_config = sync_config.get("remotes")
    if not isinstance(remotes_config, dict):
        return
    if name not in remotes_config:
        return

    cast(Container, remotes_config[name])["last_push"] = timestamp
    cfg_path.write_text(tomlkit.dumps(doc))


def get_ingestion_filter_binary() -> bool:
    """Get whether to filter binary content during ingestion.

    Reads from config [ingestion] filter_binary, defaults to True.

    Example config:
        [ingestion]
        filter_binary = false  # disable binary filtering
    """
    doc = load_config()
    ingestion_config = doc.get("ingestion", {})
    if isinstance(ingestion_config, dict):
        value = ingestion_config.get("filter_binary")
        if value is not None:
            # Handle boolean or string "true"/"false"
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() not in ("false", "0", "no")
    # Default: filtering is enabled
    return True
