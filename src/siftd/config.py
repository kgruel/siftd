"""User configuration management for siftd.

Config file location: ~/.config/siftd/config.toml

Example config:
    [search]
    formatter = "verbose"
"""

import contextlib
import os
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple, cast

import tomlkit
import tomlkit.exceptions
from tomlkit import TOMLDocument
from tomlkit.container import Container

from siftd.paths import config_dir, config_file


def _ensure_config_dir() -> Path:
    """Create config directory with mode 0o700 if needed, return path."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _write_config(path: Path, content: str) -> None:
    """Write config atomically with restrictive permissions (0o600).

    Writes to a temp file in the same directory, then replaces the target.
    This avoids a window where the file is readable by other users.
    """
    _ensure_config_dir()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class _SchemaEntry(NamedTuple):
    pattern: str
    expected: str
    validator: Callable[[object], bool]
    description: str = ""
    default: str = ""


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_int_like(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value)
            return True
        except ValueError:
            return False
    return False


def _is_bool_like(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("true", "false", "0", "1", "yes", "no")
    return False


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


_CONFIG_SCHEMA: list[_SchemaEntry] = [
    # Database
    _SchemaEntry("db.path", "string", _is_str,
                 "Override default database path", "~/.local/share/siftd/siftd.db"),
    # Tools
    _SchemaEntry("tools.limit", "int", _is_int_like,
                 "Default result limit for tool-search", "20"),
    # Query
    _SchemaEntry("query.limit", "int", _is_int_like,
                 "Default conversation list limit", "20"),
    _SchemaEntry("query.chars", "int", _is_int_like,
                 "Max characters per turn in list view", "200"),
    _SchemaEntry("query.tool_chars", "int", _is_int_like,
                 "Max characters for tool content in detail view", "120"),
    # Ingestion
    _SchemaEntry("ingestion.filter_binary", "bool", _is_bool_like,
                 "Skip binary content blobs during ingest", "true"),
    # Serve
    _SchemaEntry("serve.delegate", "bool", _is_bool_like,
                 "CLI delegates read ops to running serve instance", "true"),
    _SchemaEntry("serve.url", "string", _is_str,
                 "Explicit serve URL for delegation (skips auto-discovery)", ""),
    _SchemaEntry("serve.db", "string", _is_str,
                 "Database path for serve (overrides db.path)", ""),
    _SchemaEntry("serve.host", "string", _is_str,
                 "Bind address", "127.0.0.1"),
    _SchemaEntry("serve.port", "int", _is_int_like,
                 "Listen port", "8484"),
    _SchemaEntry("serve.fts_rebuild", "string", _is_str,
                 "When to rebuild FTS index: on_push, scheduled, off", "on_push"),
    # Serve auth — static_token, OIDC, or introspection
    _SchemaEntry("serve.auth.static_token", "string", _is_str,
                 "Static bearer token for auth (supports env:VAR syntax)", ""),
    _SchemaEntry("serve.auth.delegation_token", "string", _is_str,
                 "Bearer token used by the CLI delegation client (supports env:VAR syntax)", ""),
    _SchemaEntry("serve.auth.identity", "string", _is_str,
                 "User identity for static_token mode", "local"),
    _SchemaEntry("serve.auth.issuer", "string", _is_str,
                 "OIDC issuer URL for JWT validation", ""),
    _SchemaEntry("serve.auth.audience", "string", _is_str,
                 "OIDC audience claim", "siftd"),
    _SchemaEntry("serve.auth.identity_claim", "string", _is_str,
                 "Token claim to use as user identity", "sub"),
    _SchemaEntry("serve.auth.jwks_url", "string", _is_str,
                 "JWKS URL (auto-discovered from issuer if omitted)", ""),
    _SchemaEntry("serve.auth.introspection_url", "string", _is_str,
                 "RFC 7662 token introspection endpoint", ""),
    _SchemaEntry("serve.auth.client_id", "string", _is_str,
                 "Client ID for introspection auth", ""),
    _SchemaEntry("serve.auth.client_secret", "string", _is_str,
                 "Client secret for introspection (supports env:VAR syntax)", ""),
    _SchemaEntry("serve.auth.required_scopes", "list[string]", _is_str_list,
                 "Scopes the token must have for any access (all-of)", ""),
    _SchemaEntry("serve.auth.write_scopes", "list[string]", _is_str_list,
                 "Additional scopes required for write operations (any-of)", ""),
    # Adapters
    _SchemaEntry("adapters.*.locations", "list[string]", _is_str_list,
                 "Override discovery paths for a specific adapter", ""),
    # Sync — global defaults
    _SchemaEntry("sync.connect_timeout_s", "int", _is_int_like,
                 "TCP/SSH handshake timeout in seconds", "30"),
    _SchemaEntry("sync.command_timeout_s", "int", _is_int_like,
                 "Total operation timeout (transfer + remote processing)", "600"),
    # Sync — SSH transport defaults
    _SchemaEntry("sync.ssh.options", "list[string]", _is_str_list,
                 "Extra SSH options passed to asyncssh connect", ""),
    _SchemaEntry("sync.ssh.connect_timeout_s", "int", _is_int_like,
                 "SSH connection timeout in seconds", "30"),
    _SchemaEntry("sync.ssh.command_timeout_s", "int", _is_int_like,
                 "SSH command timeout in seconds", "600"),
    # Sync — HTTP transport defaults
    _SchemaEntry("sync.http.connect_timeout_s", "int", _is_int_like,
                 "HTTP connection timeout in seconds", "30"),
    _SchemaEntry("sync.http.command_timeout_s", "int", _is_int_like,
                 "HTTP request timeout in seconds", "600"),
    # Sync — per-remote settings
    _SchemaEntry("sync.remotes.*.host", "string", _is_str,
                 "SSH host for a named remote", ""),
    _SchemaEntry("sync.remotes.*.path", "string", _is_str,
                 "Remote database path", ""),
    _SchemaEntry("sync.remotes.*.last_push", "string", _is_str,
                 "Timestamp of last confirmed push (managed by siftd)", ""),
    _SchemaEntry("sync.remotes.*.last_pull", "string", _is_str,
                 "Timestamp of last pull (managed by siftd)", ""),
    _SchemaEntry("sync.remotes.*.last_sent", "string", _is_str,
                 "Timestamp of last staged delivery (managed by siftd)", ""),
    _SchemaEntry("sync.remotes.*.connect_timeout_s", "int", _is_int_like,
                 "Per-remote connection timeout override", ""),
    _SchemaEntry("sync.remotes.*.command_timeout_s", "int", _is_int_like,
                 "Per-remote command timeout override", ""),
    _SchemaEntry("sync.remotes.*.ssh.options", "list[string]", _is_str_list,
                 "Per-remote SSH options (overrides sync.ssh.options)", ""),
    _SchemaEntry("sync.remotes.*.ssh.connect_timeout_s", "int", _is_int_like,
                 "Per-remote SSH connection timeout", ""),
    _SchemaEntry("sync.remotes.*.ssh.command_timeout_s", "int", _is_int_like,
                 "Per-remote SSH command timeout", ""),
    _SchemaEntry("sync.remotes.*.http.connect_timeout_s", "int", _is_int_like,
                 "Per-remote HTTP connection timeout", ""),
    _SchemaEntry("sync.remotes.*.http.command_timeout_s", "int", _is_int_like,
                 "Per-remote HTTP request timeout", ""),
    # Sync — strategy and filters
    _SchemaEntry("sync.strategy", "string", _is_str,
                 "Default sync strategy: incremental or full", "incremental"),
    _SchemaEntry("sync.remotes.*.strategy", "string", _is_str,
                 "Per-remote sync strategy override", ""),
    _SchemaEntry("sync.remotes.*.filters.workspace", "string", _is_str,
                 "Default workspace filter for this remote", ""),
    _SchemaEntry("sync.remotes.*.filters.tag", "list[string]", _is_str_list,
                 "Only sync conversations with these tags", ""),
    _SchemaEntry("sync.remotes.*.filters.no_tag", "list[string]", _is_str_list,
                 "Exclude conversations with these tags", ""),
    _SchemaEntry("sync.remotes.*.filters.owner", "string", _is_str,
                 "Default owner filter for this remote", ""),
    # Update
    _SchemaEntry("update.check", "bool", _is_bool_like,
                 "Check PyPI for updates after commands (24h interval)", "true"),
]


def _match_schema(key: str) -> _SchemaEntry | None:
    parts = key.split(".")
    for entry in _CONFIG_SCHEMA:
        pattern_parts = entry.pattern.split(".")
        if len(pattern_parts) != len(parts):
            continue
        if all(p == "*" or p == k for p, k in zip(pattern_parts, parts)):
            return entry
    return None


def _iter_config_items(obj: object, prefix: str = "") -> Iterable[tuple[str, object]]:
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        key_str = str(key)
        full_key = f"{prefix}.{key_str}" if prefix else key_str
        yield full_key, value
        if isinstance(value, dict):
            yield from _iter_config_items(value, full_key)


def _warn_validation(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _validate_config(doc: TOMLDocument) -> None:
    for key, value in _iter_config_items(doc):
        entry = _match_schema(key)
        if entry is None:
            if not isinstance(value, dict):
                _warn_validation(f"Unknown config key '{key}'")
            continue
        if not entry.validator(value):
            _warn_validation(
                f"Config key '{key}' expects {entry.expected} but found {type(value).__name__}"
            )


def _ensure_known_key(key: str) -> None:
    if _match_schema(key) is None:
        raise ValueError(f"Unknown config key '{key}'")


def load_config() -> TOMLDocument:
    """Load config from file, returning empty document if missing or invalid."""
    path = config_file()
    if not path.exists():
        return tomlkit.document()

    try:
        doc = tomlkit.parse(path.read_text())
    except tomlkit.exceptions.TOMLKitError as e:
        print(f"Warning: Invalid config file {path}: {e}", file=sys.stderr)
        return tomlkit.document()
    return doc


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


def get_config_table(prefix: str) -> dict | None:
    """Get a config section as a dict by dotted prefix (e.g., 'serve.auth').

    Returns None if the section doesn't exist or isn't a table.
    """
    doc = load_config()
    current = doc
    for part in prefix.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]

    if not isinstance(current, dict):
        return None
    return dict(current)


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
    Raises ValueError for unknown keys.
    """
    _ensure_known_key(key)
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

    _write_config(path, tomlkit.dumps(doc))


def _ensure_parent_table(doc: TOMLDocument, parts: list[str]) -> Container:
    """Return parent container for a dotted key, creating tables as needed."""
    current: Container = doc
    for part in parts[:-1]:
        if part not in current:
            current[part] = tomlkit.table()
        elif not isinstance(current[part], dict):
            raise ValueError(f"Config path '{'.'.join(parts[:-1])}' is not a table")
        current = cast(Container, current[part])
    return current


def _get_parent_table(doc: TOMLDocument, parts: list[str]) -> Container | None:
    """Return parent container for a dotted key, or None if missing/invalid."""
    current: Container = doc
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return None
        current = cast(Container, current[part])
    if not isinstance(current, dict):
        return None
    return current


def append_config_list(key: str, value: str) -> bool:
    """Append a value to a list-valued config key.

    Creates intermediate tables and list if missing. Returns True if changed.
    Raises ValueError if the key exists and is not a list.
    """
    path = config_file()

    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text())
        except tomlkit.exceptions.TOMLKitError:
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    parts = key.split(".")
    parent = _ensure_parent_table(doc, parts)
    leaf = parts[-1]

    existing = parent.get(leaf)
    if existing is None:
        arr = tomlkit.array()
        arr.append(value)
        parent[leaf] = arr
        _write_config(path, tomlkit.dumps(doc))
        return True

    if not isinstance(existing, list):
        raise ValueError(f"Config key '{key}' is not a list")

    if value in existing:
        return False

    existing.append(value)
    _write_config(path, tomlkit.dumps(doc))
    return True


def remove_config_list(key: str, value: str) -> bool:
    """Remove a value from a list-valued config key.

    Returns True if a value was removed, False otherwise.
    Raises ValueError if the key exists and is not a list.
    """
    path = config_file()
    if not path.exists():
        return False

    try:
        doc = tomlkit.parse(path.read_text())
    except tomlkit.exceptions.TOMLKitError:
        return False

    parts = key.split(".")
    parent = _get_parent_table(doc, parts)
    if parent is None:
        return False
    leaf = parts[-1]

    existing = parent.get(leaf)
    if existing is None:
        return False
    if not isinstance(existing, list):
        raise ValueError(f"Config key '{key}' is not a list")

    changed = False
    while value in existing:
        existing.remove(value)
        changed = True

    if not changed:
        return False

    _write_config(path, tomlkit.dumps(doc))
    return True



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


def get_tools_defaults() -> dict:
    """Get default values for 'siftd tools' command from config.

    Reads [tools] section. Returns dict with int-valued keys only (limit).
    """
    doc = load_config()
    defaults = {}

    tools_config = doc.get("tools", {})
    if isinstance(tools_config, dict):
        for key in ("limit",):
            raw = tools_config.get(key)
            if raw is not None:
                try:
                    defaults[key] = int(raw)
                except (ValueError, TypeError):
                    pass

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


def _parse_sync_filters(cfg: dict) -> dict:
    """Parse a [sync.remotes.*.filters] table into a SyncFilters-compatible dict."""
    result: dict = {}
    workspace = cfg.get("workspace")
    if isinstance(workspace, str):
        result["workspace"] = workspace
    tag = cfg.get("tag")
    if isinstance(tag, list):
        result["tag"] = [str(t) for t in tag]
    no_tag = cfg.get("no_tag")
    if isinstance(no_tag, list):
        result["no_tag"] = [str(t) for t in no_tag]
    owner = cfg.get("owner")
    if isinstance(owner, str):
        result["owner"] = owner
    return result


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
        entry: dict = {
            "name": name,
            "host": cfg.get("host"),
            "path": str(cfg.get("path", "")),
            "last_push": cfg.get("last_push"),
            "last_pull": cfg.get("last_pull"),
            "last_sent": cfg.get("last_sent"),
            "last_push_filters": cfg.get("last_push_filters", ""),
            "last_pull_filters": cfg.get("last_pull_filters", ""),
            "last_sent_filters": cfg.get("last_sent_filters", ""),
            "auth": dict(cfg["auth"]) if "auth" in cfg and isinstance(cfg.get("auth"), dict) else None,
        }
        strategy = cfg.get("strategy")
        if isinstance(strategy, str):
            entry["strategy"] = strategy
        filters_cfg = cfg.get("filters")
        if isinstance(filters_cfg, dict):
            entry["filters"] = _parse_sync_filters(filters_cfg)
        remotes.append(entry)
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

    _write_config(cfg_path, tomlkit.dumps(doc))


def set_remote_auth(name: str, auth: dict) -> None:
    """Set auth config for a sync remote."""
    cfg_path = config_file()
    doc = tomlkit.parse(cfg_path.read_text())
    sync_tbl = cast(Container, doc["sync"])
    remotes_tbl = cast(Container, sync_tbl["remotes"])
    remote_tbl = cast(Container, remotes_tbl[name])
    remote_tbl["auth"] = auth
    _write_config(cfg_path, tomlkit.dumps(doc))


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
    _write_config(cfg_path, tomlkit.dumps(doc))
    return True


def update_last_push(
    name: str, timestamp: str, *, filter_signature: str = "",
) -> None:
    """Write last_push timestamp and filter signature for a sync remote."""
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

    remote = cast(Container, remotes_config[name])
    remote["last_push"] = timestamp
    remote["last_push_filters"] = filter_signature
    _write_config(cfg_path, tomlkit.dumps(doc))


def update_last_pull(
    name: str, timestamp: str, *, filter_signature: str = "",
) -> None:
    """Write last_pull timestamp and filter signature for a sync remote."""
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

    remote = cast(Container, remotes_config[name])
    remote["last_pull"] = timestamp
    remote["last_pull_filters"] = filter_signature
    _write_config(cfg_path, tomlkit.dumps(doc))


def update_last_sent(
    name: str, timestamp: str, *, filter_signature: str = "",
) -> None:
    """Write last_sent timestamp and filter signature for a sync remote."""
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

    remote = cast(Container, remotes_config[name])
    remote["last_sent"] = timestamp
    remote["last_sent_filters"] = filter_signature
    _write_config(cfg_path, tomlkit.dumps(doc))


def get_ssh_options(remote_name: str | None = None) -> list[str]:
    """Build SSH CLI options from config.

    Per-remote ``sync.remotes.<name>.ssh.options`` takes precedence over
    global ``sync.ssh.options``.  The ``sync.ssh.connect_timeout_s`` value
    is always appended as ``-o ConnectTimeout=N`` (unless overridden by a
    per-remote options list).

    Returns a flat list of strings suitable for splicing into a subprocess
    command (e.g. ``["ssh"] + get_ssh_options("alcove") + [host, ...]``).
    """
    doc = load_config()
    sync_config = doc.get("sync", {})
    if not isinstance(sync_config, dict):
        return []

    # Check per-remote options first
    if remote_name is not None:
        remotes_config = sync_config.get("remotes", {})
        if isinstance(remotes_config, dict):
            remote_cfg = remotes_config.get(remote_name, {})
            if isinstance(remote_cfg, dict):
                ssh_cfg = remote_cfg.get("ssh", {})
                if isinstance(ssh_cfg, dict):
                    opts = ssh_cfg.get("options")
                    if isinstance(opts, list):
                        return [str(o) for o in opts]

    # Fall back to global options
    ssh_config = sync_config.get("ssh", {})
    if not isinstance(ssh_config, dict):
        return []

    result: list[str] = []

    opts = ssh_config.get("options")
    if isinstance(opts, list):
        result.extend(str(o) for o in opts)

    timeout = ssh_config.get("connect_timeout_s")
    if timeout is not None:
        try:
            result.extend(["-o", f"ConnectTimeout={int(timeout)}"])
        except (ValueError, TypeError):
            pass

    return result


def get_ssh_connect_kwargs(remote_name: str | None = None) -> dict:
    """Build asyncssh connect kwargs from config.

    Reads the same config sections as ``get_ssh_options`` but returns a dict
    suitable for passing to ``asyncssh.connect(host, **kwargs)``.

    Supported config keys (under ``[sync.ssh]`` or ``[sync.remotes.<name>.ssh]``):
        identity_file  -> client_keys=[path]
        username       -> username=user
        port           -> port=N
        known_hosts    -> known_hosts=path | None (if "none")
        connect_timeout_s -> connect_timeout=N
    """
    doc = load_config()
    sync_config = doc.get("sync", {})
    if not isinstance(sync_config, dict):
        return {}

    # Resolve per-remote SSH config, falling back to global
    ssh_cfg: dict = {}
    if remote_name is not None:
        remotes_config = sync_config.get("remotes", {})
        if isinstance(remotes_config, dict):
            remote_cfg = remotes_config.get(remote_name, {})
            if isinstance(remote_cfg, dict):
                per_remote_ssh = remote_cfg.get("ssh", {})
                if isinstance(per_remote_ssh, dict):
                    ssh_cfg = dict(per_remote_ssh)

    if not ssh_cfg:
        global_ssh = sync_config.get("ssh", {})
        if isinstance(global_ssh, dict):
            ssh_cfg = dict(global_ssh)

    if not ssh_cfg:
        return {}

    result: dict = {}

    identity = ssh_cfg.get("identity_file")
    if identity is not None:
        result["client_keys"] = [str(identity)]

    username = ssh_cfg.get("username")
    if username is not None:
        result["username"] = str(username)

    port = ssh_cfg.get("port")
    if port is not None:
        try:
            result["port"] = int(port)
        except (ValueError, TypeError):
            pass

    known_hosts = ssh_cfg.get("known_hosts")
    if known_hosts is not None:
        val = str(known_hosts).lower()
        if val == "none":
            result["known_hosts"] = None
        else:
            result["known_hosts"] = str(known_hosts)

    timeout = ssh_cfg.get("connect_timeout_s")
    if timeout is not None:
        try:
            result["connect_timeout"] = int(timeout)
        except (ValueError, TypeError):
            pass

    return result


# ---------------------------------------------------------------------------
# Sync timeout resolution
# ---------------------------------------------------------------------------

_DEFAULT_CONNECT_TIMEOUT = 30
_DEFAULT_COMMAND_TIMEOUT = 600


def get_sync_timeouts(
    remote_name: str | None = None,
    transport: str = "ssh",
) -> tuple[int, int]:
    """Return (connect_timeout, command_timeout) for a sync operation.

    Resolution order (first non-None wins):
      1. sync.remotes.<name>.<transport>.connect_timeout_s / command_timeout_s
      2. sync.remotes.<name>.connect_timeout_s / command_timeout_s
      3. sync.<transport>.connect_timeout_s / command_timeout_s
      4. sync.connect_timeout_s / sync.command_timeout_s
      5. Hardcoded defaults (30, 600)
    """
    doc = load_config()
    sync_config = doc.get("sync", {})
    if not isinstance(sync_config, dict):
        return _DEFAULT_CONNECT_TIMEOUT, _DEFAULT_COMMAND_TIMEOUT

    def _int_or_none(d: dict, key: str) -> int | None:
        val = d.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    # Layer 1: per-remote transport-specific
    remote_transport: dict = {}
    # Layer 2: per-remote general
    remote_general: dict = {}
    if remote_name is not None:
        remotes = sync_config.get("remotes", {})
        if isinstance(remotes, dict):
            rcfg = remotes.get(remote_name, {})
            if isinstance(rcfg, dict):
                remote_general = rcfg
                tsub = rcfg.get(transport, {})
                if isinstance(tsub, dict):
                    remote_transport = tsub

    # Layer 3: transport-global
    transport_global = sync_config.get(transport, {})
    if not isinstance(transport_global, dict):
        transport_global = {}

    # Layer 4: sync-global
    sync_global = sync_config

    # Resolve connect_timeout
    connect = (
        _int_or_none(remote_transport, "connect_timeout_s")
        or _int_or_none(remote_general, "connect_timeout_s")
        or _int_or_none(transport_global, "connect_timeout_s")
        or _int_or_none(sync_global, "connect_timeout_s")
        or _DEFAULT_CONNECT_TIMEOUT
    )

    # Resolve command_timeout
    command = (
        _int_or_none(remote_transport, "command_timeout_s")
        or _int_or_none(remote_general, "command_timeout_s")
        or _int_or_none(transport_global, "command_timeout_s")
        or _int_or_none(sync_global, "command_timeout_s")
        or _DEFAULT_COMMAND_TIMEOUT
    )

    return connect, command


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
