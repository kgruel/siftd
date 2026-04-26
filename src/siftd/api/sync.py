"""Sync local conversations with a remote siftd database.

Push wraps slice_database + transport (ssh pipe or local copy/merge).
Pull wraps remote send + local receive (the inverse).

SSH pushes stream the slice DB over stdin to ``siftd db receive``
on the remote. SSH pulls stream from ``siftd db send`` on the remote.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh

from siftd.domain.sync import (
    SYNC_CAPABILITIES,  # noqa: F401 — re-exported for CLI
    SYNC_HEADER,
    SYNC_PROTOCOL_VERSION,
    PullResult,
    PushResult,
    SyncRemote,
    SyncStatus,
    parse_sync_header,
)
from siftd.safecall import parse_json

logger = logging.getLogger(__name__)


class SyncError(Exception):
    """Raised when a sync operation fails."""


def _is_http_remote(remote: SyncRemote) -> bool:
    """Check if remote uses HTTP transport (URL-based detection)."""
    return remote.path.startswith(("http://", "https://"))


def _receive_or_sync_error(
    source: Path,
    target: Path,
    *,
    rebuild_fts: bool = True,
    context: str = "Pull merge failed",
) -> None:
    """Call receive_database and convert known storage exceptions to SyncError.

    Re-raises SyncError unchanged. Wraps sqlite3.Error, ValueError,
    RuntimeError, and OSError (including FileNotFoundError, PermissionError)
    with context-specific messages, preserving exception chaining.
    """
    from siftd.api.receive import receive_database

    try:
        receive_database(source, target, rebuild_fts=rebuild_fts)
    except ValueError as e:
        raise SyncError(f"Pulled database is invalid: {e}") from e
    except (sqlite3.Error, RuntimeError, OSError) as e:
        raise SyncError(f"{context}: {e}") from e


def sync_push(
    db_path: Path,
    remote: SyncRemote,
    *,
    since: str | None = None,
    push_all: bool = False,
    workspace: str | None = None,
    tag: list[str] | None = None,
    no_tag: list[str] | None = None,
    owner: str | None = None,
    dry_run: bool = False,
) -> PushResult:
    """Push conversations to a remote database.

    Args:
        db_path: Path to the local siftd database.
        remote: The remote to push to.
        since: Only push conversations started after this date.
        push_all: Push all conversations (ignore last_push).
        workspace..owner: Filter kwargs (override remote config filters).
        dry_run: If True, slice and report but don't transfer.

    Returns:
        PushResult with stats.

    Raises:
        SyncError: On transport or merge failure.
        FileNotFoundError: If local database doesn't exist.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    # Strategy "full" forces push_all
    if remote.strategy == "full" and not push_all:
        push_all = True

    # Merge CLI filters with remote config filters (CLI overrides config)
    filters = _merge_filters(remote, workspace=workspace, tag=tag,
                             no_tag=no_tag, owner=owner)

    current_sig = _filter_signature(filters)
    effective_since = _resolve_since(since, push_all, remote, current_sig)
    should_update_cursor = since is None

    # Slice to a temp file (no FTS — remote doesn't need it)
    from siftd.api.slice import slice_database

    with tempfile.TemporaryDirectory(prefix="siftd-push-") as tmp:
        slice_path = Path(tmp) / "push-slice.db"
        result = slice_database(
            source_db=db_path,
            target_path=slice_path,
            since=effective_since,
            rebuild_fts=False,
            **filters,
        )

        conversations = result["conversations"]
        size_bytes = result["size_bytes"]

        if conversations == 0:
            return PushResult(
                conversations=0,
                size_bytes=0,
                remote_name=remote.name,
                remote_existed=True,
                dry_run=dry_run,
                last_push_updated=False,
            )

        if dry_run:
            return PushResult(
                conversations=conversations,
                size_bytes=size_bytes,
                remote_name=remote.name,
                remote_existed=True,
                dry_run=True,
                last_push_updated=False,
            )

        # Negotiate capabilities with the remote
        use_staged = False
        if _is_http_remote(remote):
            http_status = _preflight_http(remote)
            if http_status is None:
                logger.warning(
                    "HTTP sync preflight unavailable for %s; falling back to legacy push",
                    remote.name,
                )
            elif http_status.protocol_version > SYNC_PROTOCOL_VERSION:
                raise SyncError(
                    f"Remote sync protocol is newer than local "
                    f"(remote: {http_status.protocol_version}, local: {SYNC_PROTOCOL_VERSION}); "
                    "upgrade local siftd."
                )
            remote_existed = _push_http(remote, slice_path)
        elif remote.host:
            status = asyncio.run(_preflight_ssh(remote))
            if status is None:
                raise SyncError(
                    f"Could not negotiate sync capabilities with {remote.host}. "
                    f"Ensure siftd >= 0.6.3 is installed on the remote "
                    f"(needs 'db sync-status' command)."
                )
            if "staged" not in status.capabilities:
                caps = sorted(status.capabilities) if status.capabilities else []
                raise SyncError(
                    f"Remote {remote.host} does not support staged receive "
                    f"(capabilities: {caps}). Upgrade the remote's siftd."
                )
            use_staged = True
            remote_existed = asyncio.run(
                _push_ssh(remote, slice_path, staged=True),
            )
        else:
            remote_existed = _push_local(remote, slice_path, db_path)

    now = datetime.now(UTC).isoformat()
    last_push_updated = False
    if should_update_cursor:
        if use_staged:
            # Staged: trigger processing, only advance cursor on confirmation.
            # If processing fails the data is safely staged on the remote;
            # not advancing the cursor means next push may resend (idempotent).
            processing_confirmed = False
            if remote.host and not _is_http_remote(remote):
                try:
                    asyncio.run(_process_remote_ssh(remote))
                    processing_confirmed = True
                except SyncError:
                    pass  # Data is staged; can be processed later

            if processing_confirmed:
                from siftd.config_sync import update_last_sent

                update_last_sent(remote.name, now,
                                 filter_signature=current_sig)
                last_push_updated = True
        else:
            # Blocking: record last_push only after confirmed merge
            from siftd.config_sync import update_last_push

            update_last_push(remote.name, now,
                             filter_signature=current_sig)
            last_push_updated = True

    return PushResult(
        conversations=conversations,
        size_bytes=size_bytes,
        remote_name=remote.name,
        remote_existed=remote_existed,
        dry_run=False,
        last_push_updated=last_push_updated,
    )


def _resolve_since(
    explicit: str | None,
    push_all: bool,
    remote: SyncRemote,
    current_filter_sig: str = "",
) -> str | None:
    """Determine the effective --since value.

    Priority: explicit flag > push_all (None) > last_sent > last_push > None.
    ``last_sent`` is preferred because it records the most recent successful
    delivery (staged or blocking), whereas ``last_push`` only records
    confirmed blocking merges.

    When filters change (different signature), the stored cursor no longer
    covers the new filter set and we reset to None (full sync).
    """
    if explicit is not None:
        return explicit
    if push_all:
        return None

    cursor = remote.last_sent or remote.last_push
    if cursor is None:
        return None

    # Check filter signature — reset cursor if filters changed
    stored_sig = (remote.last_sent_filters if remote.last_sent
                  else remote.last_push_filters)
    if current_filter_sig != (stored_sig or ""):
        return None  # filters changed → full sync

    return cursor


def _filter_signature(filters: dict) -> str:
    """Compute a deterministic signature for an effective filter set.

    Returns a short hex digest. Empty string when no filters are active.
    Used to detect filter changes between sync runs so the cursor can be
    reset (avoiding silently skipping historical data).
    """
    normalized: dict = {}
    for k in sorted(filters):
        v = filters[k]
        if v is None:
            continue
        if isinstance(v, list):
            v = sorted(v)
        normalized[k] = v
    if not normalized:
        return ""
    payload = json.dumps(normalized, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _merge_filters(
    remote: SyncRemote,
    *,
    workspace: str | None = None,
    tag: list[str] | None = None,
    no_tag: list[str] | None = None,
    owner: str | None = None,
) -> dict:
    """Merge CLI filter kwargs with remote config filters.

    CLI values override config values (replace, not intersect).
    Returns a dict suitable for passing as **kwargs to slice_database().
    """
    result: dict = {}
    cfg = remote.filters

    # For each filter: use CLI value if provided, else fall back to config
    result["workspace"] = workspace if workspace is not None else (cfg.workspace if cfg else None)
    result["tag"] = tag if tag is not None else (cfg.tag if cfg else None)
    result["no_tag"] = no_tag if no_tag is not None else (cfg.no_tag if cfg else None)
    result["owner"] = owner if owner is not None else (cfg.owner if cfg else None)

    return result


async def _push_ssh(
    remote: SyncRemote, slice_path: Path, *, staged: bool = False,
) -> bool:
    """Push via asyncssh to ``siftd db receive`` on the remote.

    Streams the slice DB over stdin in a single SSH connection:
        asyncssh.connect(host, **opts) -> conn.run(cmd, input=data)

    When *staged* is True, uses ``receive --stage --no-fts`` for a fast ACK.

    Returns whether the remote DB already existed (status != "created").
    """
    assert remote.host is not None

    remote_db = shlex.quote(remote.path)
    receive_cmd = f"siftd --db {remote_db} db receive --no-fts"
    if staged:
        receive_cmd += " --stage"

    hostname, connect_opts = _build_ssh_options(remote)

    from siftd.config_sync import get_sync_timeouts

    connect_timeout, command_timeout = get_sync_timeouts(remote.name, "ssh")

    if "connect_timeout" not in connect_opts:
        connect_opts["connect_timeout"] = connect_timeout

    slice_data = SYNC_HEADER + slice_path.read_bytes()

    try:
        async with asyncssh.connect(hostname, **connect_opts) as conn:
            result = await conn.run(
                receive_cmd, input=slice_data, encoding=None,
                timeout=command_timeout,
            )
    except asyncssh.PermissionDenied as e:
        raise SyncError(_friendly_os_error(remote.host, "Permission denied")) from e
    except asyncssh.ConnectionLost as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e
    except asyncssh.DisconnectError as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e
    except asyncssh.ChannelOpenError as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e
    except OSError as e:
        if isinstance(e, TimeoutError):
            raise SyncError(
                f"Push to {hostname} timed out after {command_timeout}s. "
                "The remote may be slow or unreachable."
            ) from e
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e

    if result.returncode is not None and result.returncode != 0:
        stderr = (result.stderr.strip() if result.stderr else b"").decode()
        raise SyncError(_friendly_remote_error(remote.host, remote.path, stderr))

    stdout = (result.stdout.strip() if result.stdout else b"").decode()
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise SyncError(
            f"Unexpected response from remote: {stdout!r}"
        ) from e

    return response.get("status") != "created"


def _parse_ssh_host(host: str) -> tuple[str, str | None]:
    """Parse ``user@host`` into (hostname, username).

    Returns (host, None) when there is no ``@``.
    """
    if "@" in host:
        username, hostname = host.rsplit("@", 1)
        return hostname, username
    return host, None


def _build_ssh_options(remote: SyncRemote) -> tuple[str, dict[str, Any]]:
    """Build asyncssh connect kwargs from config for this remote.

    Parses ``user@host`` from ``remote.host`` and sets ``username`` unless
    an explicit username is already configured in ``[sync.remotes.<name>.ssh]``.

    Returns (hostname, connect_opts) so callers pass the bare hostname to
    ``asyncssh.connect()``.
    """
    from siftd.config_sync import get_ssh_connect_kwargs

    opts = get_ssh_connect_kwargs(remote.name)

    hostname = remote.host or ""
    if remote.host:
        hostname, parsed_user = _parse_ssh_host(remote.host)
        if parsed_user and "username" not in opts:
            opts["username"] = parsed_user

    return hostname, opts


async def _process_remote_ssh(remote: SyncRemote) -> None:
    """Trigger ``siftd db process`` on the remote to merge staged payloads."""
    assert remote.host is not None

    remote_db = shlex.quote(remote.path)
    cmd = f"siftd --db {remote_db} db process"

    hostname, connect_opts = _build_ssh_options(remote)

    from siftd.config_sync import get_sync_timeouts

    connect_timeout, command_timeout = get_sync_timeouts(remote.name, "ssh")
    if "connect_timeout" not in connect_opts:
        connect_opts["connect_timeout"] = connect_timeout

    try:
        async with asyncssh.connect(hostname, **connect_opts) as conn:
            result = await conn.run(cmd, encoding="utf-8", timeout=command_timeout)
    except Exception as e:
        raise SyncError(f"Remote process failed: {e}") from e

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise SyncError(f"Remote process failed: {stderr}")


def _friendly_os_error(host: str, message: str) -> str:
    """Map common OS/SSH errors to user-friendly messages."""
    if "Connection refused" in message:
        return f"Cannot connect to {host}. Is the host running and accepting SSH?"
    if "Permission denied" in message:
        return f"SSH authentication failed for {host}. Check your SSH key or credentials."
    if "Could not resolve" in message or re.search(r"Name.*not known", message):
        return f"Cannot resolve hostname '{host}'. Check the remote address."
    return f"SSH failed for {host}: {message}"


def _friendly_remote_error(host: str, path: str, stderr: str) -> str:
    """Map remote stderr to user-friendly messages."""
    # SSH-level errors (binary missing on remote)
    if "command not found" in stderr or "No such file" in stderr:
        return f"siftd is not installed on {host}. Install with: uv tool install siftd (or pipx install siftd)"

    # Try parsing structured JSON error from cmd_db_receive
    err = parse_json(stderr)
    if isinstance(err, dict):
        error_type = err.get("error_type", "")
        error_msg = err.get("error", stderr)
        if error_type == "database_locked":
            return (
                f"Remote database is locked. Another process may be using "
                f"{path} on {host}. Wait and retry."
            )
        return f"Remote error: {error_msg}"

    # Fall back to first line of raw stderr
    first_line = stderr.split("\n", 1)[0]
    return f"Remote error on {host}: {first_line}"


# ---------------------------------------------------------------------------
# Pre-flight capability negotiation
# ---------------------------------------------------------------------------


async def _preflight_ssh(remote: SyncRemote) -> SyncStatus | None:
    """Query receiver capabilities via SSH. Returns None if unsupported."""
    assert remote.host is not None

    remote_db = shlex.quote(remote.path)
    cmd = f"siftd --db {remote_db} db sync-status"

    hostname, connect_opts = _build_ssh_options(remote)

    from siftd.config_sync import get_sync_timeouts

    connect_timeout, _ = get_sync_timeouts(remote.name, "ssh")
    if "connect_timeout" not in connect_opts:
        connect_opts["connect_timeout"] = connect_timeout

    try:
        async with asyncssh.connect(hostname, **connect_opts) as conn:
            result = await conn.run(cmd, encoding="utf-8", timeout=connect_timeout)
    except Exception:
        return None

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout.strip())
        return SyncStatus.from_json(data)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _preflight_http(remote: SyncRemote) -> SyncStatus | None:
    """Query receiver capabilities via HTTP. Returns None if unsupported."""
    import httpx

    from siftd.api.auth import AuthError, acquire_token
    from siftd.config_sync import get_sync_remote, get_sync_timeouts

    remote_cfg = get_sync_remote(remote.name)
    auth = remote_cfg.get("auth") if remote_cfg else None
    headers: dict[str, str] = {}
    try:
        token = acquire_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    except AuthError:
        pass

    url = remote.path.rstrip("/") + "/api/v1/sync/status"
    connect_timeout, _ = get_sync_timeouts(remote.name, "http")

    try:
        with httpx.Client(timeout=connect_timeout) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
        return SyncStatus.from_json(resp.json())
    except Exception:
        return None


def _push_local(remote: SyncRemote, slice_path: Path, db_path: Path) -> bool:
    """Push to a local path. Returns whether the remote DB existed."""
    target = Path(remote.path)
    remote_existed = target.exists()

    if not remote_existed:
        # First push: copy the slice directly
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(slice_path, target)
        return False

    # Subsequent push: merge into the existing DB
    from siftd.api.merge import merge_database

    try:
        merge_database(
            target_db=target,
            source_path=slice_path,
            rebuild_fts=False,
        )
    except (RuntimeError, FileNotFoundError) as e:
        raise SyncError(f"Local merge failed: {e}") from e

    return True


def _push_http(remote: SyncRemote, slice_path: Path) -> bool:
    """Push via HTTP POST to remote /api/v1/push endpoint.

    Returns whether remote DB already existed.
    """
    import httpx

    from siftd.api.auth import AuthError, acquire_token
    from siftd.config_sync import get_sync_remote

    remote_cfg = get_sync_remote(remote.name)
    auth = remote_cfg.get("auth") if remote_cfg else None
    headers: dict[str, str] = {}
    try:
        token = acquire_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    except AuthError:
        pass  # --no-auth server

    url = remote.path.rstrip("/") + "/api/v1/push"
    data = slice_path.read_bytes()

    from siftd.config_sync import get_sync_timeouts

    connect_timeout, command_timeout = get_sync_timeouts(remote.name, "http")

    try:
        with httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=command_timeout,
                write=command_timeout,
                pool=connect_timeout,
            ),
        ) as client:
            resp = client.post(
                url, content=data,
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise SyncError(f"Push to {remote.path} failed: HTTP {e.response.status_code}") from e
    except httpx.ConnectError as e:
        raise SyncError(f"Cannot connect to {remote.path}: {e}") from e

    body = resp.json()
    return body.get("status") != "created"


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def sync_pull(
    db_path: Path,
    remote: SyncRemote,
    *,
    since: str | None = None,
    pull_all: bool = False,
    workspace: str | None = None,
    tag: list[str] | None = None,
    no_tag: list[str] | None = None,
    owner: str | None = None,
    dry_run: bool = False,
) -> PullResult:
    """Pull conversations from a remote database.

    Args:
        db_path: Path to the local siftd database.
        remote: The remote to pull from.
        since: Only pull conversations started after this date.
        pull_all: Pull all conversations (ignore last_pull).
        workspace..owner: Filter kwargs (override remote config filters).
        dry_run: If True, query remote but don't merge locally.

    Returns:
        PullResult with stats.

    Raises:
        SyncError: On transport or merge failure.
    """
    # Strategy "full" forces pull_all
    if remote.strategy == "full" and not pull_all:
        pull_all = True

    # Merge CLI filters with remote config filters
    filters = _merge_filters(remote, workspace=workspace, tag=tag,
                             no_tag=no_tag, owner=owner)

    current_sig = _filter_signature(filters)
    effective_since = _resolve_pull_since(since, pull_all, remote, current_sig)
    should_update_last_pull = since is None

    if _is_http_remote(remote):
        conversations, size_bytes = _pull_http(
            remote, db_path, effective_since, filters, dry_run,
        )
    elif remote.host:
        conversations, size_bytes = asyncio.run(
            _pull_ssh(remote, db_path, effective_since, filters, dry_run)
        )
    else:
        conversations, size_bytes = _pull_local(
            remote, db_path, effective_since, filters, dry_run,
        )

    if conversations == 0:
        return PullResult(
            conversations=0,
            size_bytes=0,
            remote_name=remote.name,
            dry_run=dry_run,
            last_pull_updated=False,
        )

    last_pull_updated = False
    if should_update_last_pull and not dry_run:
        from siftd.config_sync import update_last_pull

        now = datetime.now(UTC).isoformat()
        update_last_pull(remote.name, now,
                         filter_signature=current_sig)
        last_pull_updated = True

    return PullResult(
        conversations=conversations,
        size_bytes=size_bytes,
        remote_name=remote.name,
        dry_run=dry_run,
        last_pull_updated=last_pull_updated,
    )


def _resolve_pull_since(
    explicit: str | None,
    pull_all: bool,
    remote: SyncRemote,
    current_filter_sig: str = "",
) -> str | None:
    """Determine the effective --since value for pull.

    Priority: explicit flag > pull_all (None) > last_pull > None (all).
    Resets cursor when filter signature changes.
    """
    if explicit is not None:
        return explicit
    if pull_all:
        return None

    cursor = remote.last_pull
    if cursor is None:
        return None

    if current_filter_sig != (remote.last_pull_filters or ""):
        return None  # filters changed → full sync

    return cursor


async def _pull_ssh(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    filters: dict,
    dry_run: bool,
) -> tuple[int, int]:
    """Pull via asyncssh by running ``siftd db send`` on the remote.

    Streams the remote slice DB over stdout:
        asyncssh.connect(host, **opts) -> conn.run(send_cmd)

    Returns (conversations, size_bytes).
    """
    assert remote.host is not None

    remote_db = shlex.quote(remote.path)
    send_cmd = f"siftd --db {remote_db} db send --no-fts"
    if since is not None:
        send_cmd += f" --since {shlex.quote(since)}"
    workspace = filters.get("workspace")
    if workspace is not None:
        send_cmd += f" -w {shlex.quote(workspace)}"
    for t in filters.get("tag") or []:
        send_cmd += f" --tag {shlex.quote(t)}"
    for t in filters.get("no_tag") or []:
        send_cmd += f" --no-tag {shlex.quote(t)}"
    owner = filters.get("owner")
    if owner is not None:
        send_cmd += f" --owner {shlex.quote(owner)}"

    hostname, connect_opts = _build_ssh_options(remote)

    from siftd.config_sync import get_sync_timeouts

    connect_timeout, command_timeout = get_sync_timeouts(remote.name, "ssh")

    if "connect_timeout" not in connect_opts:
        connect_opts["connect_timeout"] = connect_timeout

    try:
        async with asyncssh.connect(hostname, **connect_opts) as conn:
            result = await conn.run(
                send_cmd, encoding=None, timeout=command_timeout,
            )
    except asyncssh.PermissionDenied as e:
        raise SyncError(_friendly_os_error(remote.host, "Permission denied")) from e
    except asyncssh.ConnectionLost as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e
    except asyncssh.DisconnectError as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e
    except asyncssh.ChannelOpenError as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e
    except OSError as e:
        if isinstance(e, TimeoutError):
            raise SyncError(
                f"Pull from {hostname} timed out after {command_timeout}s. "
                "The remote may be slow or unreachable."
            ) from e
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e

    if result.returncode is not None and result.returncode != 0:
        stderr = (result.stderr.strip() if result.stderr else b"").decode()
        raise SyncError(_friendly_remote_error(remote.host, remote.path, stderr))

    # Parse metadata from stderr (JSON line from db send)
    stderr_text = (result.stderr.strip() if result.stderr else b"").decode()
    meta = _parse_send_metadata(stderr_text)
    conversations = meta.get("conversations", 0)

    if conversations == 0:
        return 0, 0

    # stdout is binary DB data (encoding=None gives us bytes directly).
    # Strip the protocol header if present; validate version.
    raw_bytes = result.stdout or b""
    remote_version = parse_sync_header(raw_bytes)
    if remote_version is not None:
        if remote_version > SYNC_PROTOCOL_VERSION:
            raise SyncError(
                f"Remote {hostname} uses sync protocol version {remote_version}, "
                f"max supported locally is {SYNC_PROTOCOL_VERSION}. "
                "Upgrade the local installation."
            )
        raw_bytes = raw_bytes[8:]

    size_bytes = len(raw_bytes)

    if dry_run:
        return conversations, size_bytes

    # Write to temp file and merge into local DB
    with tempfile.NamedTemporaryFile(
        prefix="siftd-pull-", suffix=".db", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tmp_path.write_bytes(raw_bytes)
        _receive_or_sync_error(tmp_path, local_db)
        return conversations, size_bytes
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _pull_local(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    filters: dict,
    dry_run: bool,
) -> tuple[int, int]:
    """Pull from a local-path remote. Returns (conversations, size_bytes)."""
    source = Path(remote.path)
    if not source.exists():
        raise SyncError(f"Remote database not found: {source}")

    from siftd.api.slice import slice_database

    with tempfile.TemporaryDirectory(prefix="siftd-pull-") as tmp:
        slice_path = Path(tmp) / "pull-slice.db"
        result = slice_database(
            source_db=source,
            target_path=slice_path,
            since=since,
            rebuild_fts=False,
            **filters,
        )

        conversations = result["conversations"]
        size_bytes = result["size_bytes"]

        if conversations == 0:
            return 0, 0

        if dry_run:
            return conversations, size_bytes

        _receive_or_sync_error(slice_path, local_db)
        return conversations, size_bytes


def _pull_http(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    filters: dict,
    dry_run: bool,
) -> tuple[int, int]:
    """Pull via HTTP GET from remote /api/v1/pull endpoint.

    Returns (conversations, size_bytes).
    """
    import httpx

    from siftd.api.auth import AuthError, acquire_token
    from siftd.config_sync import get_sync_remote

    remote_cfg = get_sync_remote(remote.name)
    auth = remote_cfg.get("auth") if remote_cfg else None
    headers: dict[str, str] = {}
    try:
        token = acquire_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    except AuthError:
        pass

    url = remote.path.rstrip("/") + "/api/v1/pull"
    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    workspace = filters.get("workspace")
    if workspace is not None:
        params["workspace"] = workspace
    owner = filters.get("owner")
    if owner is not None:
        params["owner"] = owner
    tag = filters.get("tag")
    if tag:
        params["tag"] = tag
    no_tag = filters.get("no_tag")
    if no_tag:
        params["no_tag"] = no_tag

    from siftd.config_sync import get_sync_timeouts

    connect_timeout, command_timeout = get_sync_timeouts(remote.name, "http")

    try:
        with httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=command_timeout,
                write=command_timeout,
                pool=connect_timeout,
            ),
        ) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise SyncError(f"Pull from {remote.path} failed: HTTP {e.response.status_code}") from e
    except httpx.ConnectError as e:
        raise SyncError(f"Cannot connect to {remote.path}: {e}") from e

    conversations = int(resp.headers.get("X-Siftd-Conversations", 0))
    if conversations == 0:
        return 0, 0

    size_bytes = len(resp.content)

    if dry_run:
        return conversations, size_bytes

    with tempfile.NamedTemporaryFile(
        prefix="siftd-pull-http-", suffix=".db", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tmp_path.write_bytes(resp.content)
        _receive_or_sync_error(tmp_path, local_db)
        return conversations, size_bytes
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _parse_send_metadata(stderr_text: str) -> dict:
    """Parse the JSON metadata line from ``siftd db send`` stderr."""
    # db send writes a single JSON line to stderr
    for line in reversed(stderr_text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            result = parse_json(line)
            if isinstance(result, dict):
                return result
    return {}
