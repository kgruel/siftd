"""Sync local conversations with a remote siftd database.

Push wraps slice_database + transport (ssh pipe or local copy/merge).
Pull wraps remote send + local receive (the inverse).

SSH pushes stream the slice DB over stdin to ``siftd db receive``
on the remote. SSH pulls stream from ``siftd db send`` on the remote.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh

from siftd.domain.sync import PullResult, PushResult, SyncRemote
from siftd.safecall import parse_json


class SyncError(Exception):
    """Raised when a sync operation fails."""


def _is_http_remote(remote: SyncRemote) -> bool:
    """Check if remote uses HTTP transport (URL-based detection)."""
    return remote.path.startswith(("http://", "https://"))


def sync_push(
    db_path: Path,
    remote: SyncRemote,
    *,
    since: str | None = None,
    push_all: bool = False,
    workspace: str | None = None,
    dry_run: bool = False,
) -> PushResult:
    """Push conversations to a remote database.

    Args:
        db_path: Path to the local siftd database.
        remote: The remote to push to.
        since: Only push conversations started after this date.
        push_all: Push all conversations (ignore last_push).
        workspace: Filter by workspace substring.
        dry_run: If True, slice and report but don't transfer.

    Returns:
        PushResult with stats.

    Raises:
        SyncError: On transport or merge failure.
        FileNotFoundError: If local database doesn't exist.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    effective_since = _resolve_since(since, push_all, remote)
    should_update_last_push = since is None

    # Slice to a temp file (no FTS — remote doesn't need it)
    from siftd.api.slice import slice_database

    with tempfile.TemporaryDirectory(prefix="siftd-push-") as tmp:
        slice_path = Path(tmp) / "push-slice.db"
        result = slice_database(
            source_db=db_path,
            target_path=slice_path,
            since=effective_since,
            workspace=workspace,
            rebuild_fts=False,
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

        if _is_http_remote(remote):
            remote_existed = _push_http(remote, slice_path)
        elif remote.host:
            remote_existed = asyncio.run(_push_ssh(remote, slice_path))
        else:
            remote_existed = _push_local(remote, slice_path, db_path)

    last_push_updated = False
    if should_update_last_push:
        from siftd.config import update_last_push

        now = datetime.now(UTC).isoformat()
        update_last_push(remote.name, now)
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
) -> str | None:
    """Determine the effective --since value.

    Priority: explicit flag > push_all (None) > last_push > None (all).
    """
    if explicit is not None:
        return explicit
    if push_all:
        return None
    return remote.last_push


async def _push_ssh(remote: SyncRemote, slice_path: Path) -> bool:
    """Push via asyncssh to ``siftd db receive`` on the remote.

    Streams the slice DB over stdin in a single SSH connection:
        asyncssh.connect(host, **opts) -> conn.run(cmd, input=data)

    Returns whether the remote DB already existed (status != "created").
    """
    assert remote.host is not None

    remote_db = shlex.quote(remote.path)
    receive_cmd = f"siftd --db {remote_db} db receive --no-fts"

    hostname, connect_opts = _build_ssh_options(remote)

    from siftd.config import get_config

    timeout_raw = get_config("sync.ssh.connect_timeout_s")
    timeout = 300
    if timeout_raw is not None:
        try:
            timeout = int(timeout_raw)
        except (ValueError, TypeError):
            pass

    # Ensure connect_timeout is set (command timeout is separate)
    if "connect_timeout" not in connect_opts:
        connect_opts["connect_timeout"] = timeout

    slice_data = slice_path.read_bytes()

    try:
        async with asyncssh.connect(hostname, **connect_opts) as conn:
            result = await conn.run(
                receive_cmd, input=slice_data, encoding=None, timeout=timeout,
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
                f"Push to {hostname} timed out after {timeout}s. "
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


def _build_ssh_options(remote: SyncRemote) -> dict[str, Any]:
    """Build asyncssh connect kwargs from config for this remote.

    Parses ``user@host`` from ``remote.host`` and sets ``username`` unless
    an explicit username is already configured in ``[sync.remotes.<name>.ssh]``.

    Returns (hostname, connect_opts) so callers pass the bare hostname to
    ``asyncssh.connect()``.
    """
    from siftd.config import get_ssh_connect_kwargs

    opts = get_ssh_connect_kwargs(remote.name)

    hostname = remote.host or ""
    if remote.host:
        hostname, parsed_user = _parse_ssh_host(remote.host)
        if parsed_user and "username" not in opts:
            opts["username"] = parsed_user

    return hostname, opts


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
    from siftd.config import get_sync_remote

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

    try:
        with httpx.Client(timeout=300) as client:
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
    dry_run: bool = False,
) -> PullResult:
    """Pull conversations from a remote database.

    Args:
        db_path: Path to the local siftd database.
        remote: The remote to pull from.
        since: Only pull conversations started after this date.
        pull_all: Pull all conversations (ignore last_pull).
        workspace: Filter by workspace substring.
        dry_run: If True, query remote but don't merge locally.

    Returns:
        PullResult with stats.

    Raises:
        SyncError: On transport or merge failure.
    """
    effective_since = _resolve_pull_since(since, pull_all, remote)
    should_update_last_pull = since is None

    if _is_http_remote(remote):
        conversations, size_bytes = _pull_http(
            remote, db_path, effective_since, workspace, dry_run,
        )
    elif remote.host:
        conversations, size_bytes = asyncio.run(
            _pull_ssh(remote, db_path, effective_since, workspace, dry_run)
        )
    else:
        conversations, size_bytes = _pull_local(
            remote, db_path, effective_since, workspace, dry_run,
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
        from siftd.config import update_last_pull

        now = datetime.now(UTC).isoformat()
        update_last_pull(remote.name, now)
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
) -> str | None:
    """Determine the effective --since value for pull.

    Priority: explicit flag > pull_all (None) > last_pull > None (all).
    """
    if explicit is not None:
        return explicit
    if pull_all:
        return None
    return remote.last_pull


async def _pull_ssh(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    workspace: str | None,
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
    if workspace is not None:
        send_cmd += f" -w {shlex.quote(workspace)}"

    hostname, connect_opts = _build_ssh_options(remote)

    from siftd.config import get_config

    timeout_raw = get_config("sync.ssh.connect_timeout_s")
    timeout = 300
    if timeout_raw is not None:
        try:
            timeout = int(timeout_raw)
        except (ValueError, TypeError):
            pass

    if "connect_timeout" not in connect_opts:
        connect_opts["connect_timeout"] = timeout

    try:
        async with asyncssh.connect(hostname, **connect_opts) as conn:
            result = await conn.run(send_cmd, encoding=None, timeout=timeout)
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
                f"Pull from {hostname} timed out after {timeout}s. "
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

    # stdout is binary DB data (encoding=None gives us bytes directly)
    raw_bytes = result.stdout or b""

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
        from siftd.api.receive import receive_database

        receive_database(tmp_path, local_db, rebuild_fts=True)
        return conversations, size_bytes
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _pull_local(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    workspace: str | None,
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
            workspace=workspace,
            rebuild_fts=False,
        )

        conversations = result["conversations"]
        size_bytes = result["size_bytes"]

        if conversations == 0:
            return 0, 0

        if dry_run:
            return conversations, size_bytes

        from siftd.api.receive import receive_database

        receive_database(slice_path, local_db, rebuild_fts=True)
        return conversations, size_bytes


def _pull_http(
    remote: SyncRemote,
    local_db: Path,
    since: str | None,
    workspace: str | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Pull via HTTP GET from remote /api/v1/pull endpoint.

    Returns (conversations, size_bytes).
    """
    import httpx

    from siftd.api.auth import AuthError, acquire_token
    from siftd.config import get_sync_remote

    remote_cfg = get_sync_remote(remote.name)
    auth = remote_cfg.get("auth") if remote_cfg else None
    headers: dict[str, str] = {}
    try:
        token = acquire_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    except AuthError:
        pass

    url = remote.path.rstrip("/") + "/api/v1/pull"
    params: dict[str, str] = {}
    if since is not None:
        params["since"] = since
    if workspace is not None:
        params["workspace"] = workspace

    try:
        with httpx.Client(timeout=300) as client:
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
        from siftd.api.receive import receive_database

        receive_database(tmp_path, local_db, rebuild_fts=True)
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
