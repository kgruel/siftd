"""User configuration management for siftd.

Config file location: ~/.config/siftd/config.toml

Example config:
    [search]
    formatter = "verbose"
"""

import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple, cast

import tomlkit
import tomlkit.exceptions
from tomlkit import TOMLDocument
from tomlkit.container import Container

from siftd.paths import atomic_write_secure, config_file


def _write_config(path: Path, content: str) -> None:
    """Write config atomically with restrictive permissions (dir 0o700, file 0o600)."""
    atomic_write_secure(path, content)


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


_SIZE_RE = re.compile(r"^(\d+)\s*(B|KB|MB|GB|TB)?$", re.IGNORECASE)
# SI/decimal prefixes — deliberately matches Caddy's go-humanize convention
# where 'MB' = 1_000_000 bytes (not 1024²). IEC names (MiB/GiB) are not
# supported; they are not accepted by Caddy's request_body directive.
_SIZE_SUFFIXES: dict[str, int] = {
    "": 1,
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
}


def _is_size_like(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, str):
        return bool(_SIZE_RE.match(value.strip()))
    return False


def parse_size_bytes(value: str | int) -> int:
    """Parse a byte-count string with optional SI suffix into bytes.

    Accepts plain non-negative integers or strings like '500MB', '1GB', '10240'.
    Uses SI/decimal prefixes (1 MB = 1_000_000 bytes) to match Caddy's
    go-humanize convention for request_body max_size.
    """
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"Size must be non-negative, got {value!r}")
        return value
    m = _SIZE_RE.match(value.strip())
    if not m:
        raise ValueError(f"Cannot parse size: {value!r}")
    n, suffix = int(m.group(1)), (m.group(2) or "").upper()
    return n * _SIZE_SUFFIXES[suffix]


_CONFIG_SCHEMA: list[_SchemaEntry] = [
    # Database
    _SchemaEntry("db.path", "string", _is_str,
                 "Override default database path", "~/.local/share/siftd/siftd.db"),
    # Query
    _SchemaEntry("query.limit", "int", _is_int_like,
                 "Default conversation list limit", "20"),
    _SchemaEntry("query.chars", "int", _is_int_like,
                 "Max characters per turn in list view", "200"),
    _SchemaEntry("query.tool_chars", "int", _is_int_like,
                 "Max characters for tool content in detail view", "120"),
    # UI
    _SchemaEntry("ui.theme", "string", _is_str,
                 "Terminal colour theme (values: siftd, nord); terminal only — does not affect the web UI", "siftd"),
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
    _SchemaEntry("serve.request_max_body_size", "int or size string", _is_size_like,
                 "Maximum request body size (e.g. '500MB', '1GB', bytes as int). Uses SI prefixes (1 MB = 1 000 000 bytes) matching Caddy. Must be changed in lockstep with Caddyfile request_body max_size.", "500MB"),
    # Serve auth — static_token, OIDC, or introspection
    _SchemaEntry("serve.auth.static_token", "string", _is_str,
                 "Static bearer token the SERVER validates against (supports env:VAR syntax)", ""),
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
    # Browser SSO — the server advertises these PUBLIC params at GET /auth/config
    # so the browser UI can run an auth-code+PKCE login (the browser is a client,
    # exactly like the CLI device-code path). Only effective in issuer (JWKS) mode;
    # unset browser_client_id leaves the UI on manual token-paste. The browser
    # discovers authorize/token endpoints itself from issuer/.well-known.
    _SchemaEntry("serve.auth.browser_client_id", "string", _is_str,
                 "PUBLIC OAuth client ID the browser UI uses for auth-code+PKCE login "
                 "(usually the same value as auth.client_id). Empty disables browser SSO.", ""),
    _SchemaEntry("serve.auth.browser_scopes", "list[string]", _is_str_list,
                 "Scopes the browser requests at login as a TOML array; offline_access yields a "
                 "refresh token. Defaults to ['openid','profile','email','offline_access'].", ""),
    # Client-side token acquisition (distinct from serve.auth.* which is serve
    # VALIDATION config). These configure how the CLI ACQUIRES a bearer via
    # `siftd auth login` (device-code) and refreshes it — see credentials.py.
    _SchemaEntry("auth.token", "string", _is_str,
                 "Static bearer the CLI SENDS to serve (supports env:/file:/literal). "
                 "For a shared-secret setup, match serve.auth.static_token.", ""),
    _SchemaEntry("auth.issuer", "string", _is_str,
                 "OIDC issuer URL the CLI acquires tokens from (`siftd auth login`)", ""),
    _SchemaEntry("auth.client_id", "string", _is_str,
                 "PUBLIC device-code client ID (NOT serve.auth.client_id, the confidential introspection client)", ""),
    _SchemaEntry("auth.scope", "string", _is_str,
                 "Space-delimited scopes requested at login (e.g. 'openid offline_access')", "openid offline_access"),
    _SchemaEntry("auth.device_authorization_endpoint", "string", _is_str,
                 "Device authorization endpoint (auto-discovered from issuer if omitted)", ""),
    _SchemaEntry("auth.token_endpoint", "string", _is_str,
                 "Token endpoint (auto-discovered from issuer if omitted)", ""),
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
    # Tag prefixes — convention table for tag families. User-defined prefix
    # names map to the colon-prefix string used by `siftd query -l <prefix>:`
    # and the FTS5 LIKE matcher in tag_condition().
    _SchemaEntry("tag_prefixes.*", "string", _is_str,
                 "User-defined tag-prefix conventions (e.g. research = \"research:\")", ""),
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



# Built-in tag-prefix conventions. User config under [tag_prefixes] merges
# over these defaults — see get_tag_prefixes(). Names are advisory; the
# actual matcher (`siftd query -l <prefix>:`) is independent of this table.
DEFAULT_TAG_PREFIXES: dict[str, str] = {
    "decision": "decision:",
    "research": "research:",
    "useful": "useful:",
    "rationale": "rationale:",
    "genesis": "genesis:",
}


def get_tag_prefixes() -> dict[str, str]:
    """Return the resolved tag-prefix convention table.

    Built-in defaults (DEFAULT_TAG_PREFIXES) merged with user-defined
    entries from the [tag_prefixes] section of ~/.config/siftd/config.toml.
    User entries override defaults of the same name. Non-string values are
    silently skipped.
    """
    resolved: dict[str, str] = dict(DEFAULT_TAG_PREFIXES)
    user_table = get_config_table("tag_prefixes") or {}
    for name, value in user_table.items():
        if isinstance(value, str):
            resolved[str(name)] = value
    return resolved


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
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import get_sync_remotes as _get_sync_remotes

    return _get_sync_remotes()


def get_sync_remote(name: str) -> dict | None:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import get_sync_remote as _get_sync_remote

    return _get_sync_remote(name)


def set_sync_remote(name: str, host: str | None, path: str) -> None:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import set_sync_remote as _set_sync_remote

    _set_sync_remote(name, host, path)


def set_remote_auth(name: str, auth: dict) -> None:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import set_remote_auth as _set_remote_auth

    _set_remote_auth(name, auth)


def remove_sync_remote(name: str) -> bool:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import remove_sync_remote as _remove_sync_remote

    return _remove_sync_remote(name)


def update_last_push(
    name: str, timestamp: str, *, filter_signature: str = "",
) -> None:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import update_last_push as _update_last_push

    _update_last_push(name, timestamp, filter_signature=filter_signature)


def update_last_pull(
    name: str, timestamp: str, *, filter_signature: str = "",
) -> None:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import update_last_pull as _update_last_pull

    _update_last_pull(name, timestamp, filter_signature=filter_signature)


def update_last_sent(
    name: str, timestamp: str, *, filter_signature: str = "",
) -> None:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import update_last_sent as _update_last_sent

    _update_last_sent(name, timestamp, filter_signature=filter_signature)


def get_ssh_connect_kwargs(remote_name: str | None = None) -> dict:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import get_ssh_connect_kwargs as _get_ssh_connect_kwargs

    return _get_ssh_connect_kwargs(remote_name)


def get_sync_timeouts(
    remote_name: str | None = None,
    transport: str = "ssh",
) -> tuple[int, int]:
    """Backward-compatible sync accessor re-export."""
    from siftd.config_sync import get_sync_timeouts as _get_sync_timeouts

    return _get_sync_timeouts(remote_name, transport)


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
