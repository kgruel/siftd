"""Sync-specific configuration accessors.

This module holds sync domain accessors/mutators extracted from ``siftd.config``.
Config infrastructure (schema, load/save primitives) remains in ``siftd.config``.
"""

from typing import cast

import tomlkit
import tomlkit.exceptions
from tomlkit.container import Container

from siftd.config import _write_config, load_config
from siftd.paths import config_file


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


def get_ssh_connect_kwargs(remote_name: str | None = None) -> dict:
    """Build asyncssh connect kwargs from config.

    Returns a dict suitable for passing to ``asyncssh.connect(host, **kwargs)``.

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
