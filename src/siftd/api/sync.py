"""Push local conversations to a remote siftd database.

Wraps slice_database + transport (ssh pipe or local copy/merge)
into a single workflow. SSH pushes stream the slice DB over stdin
to ``siftd db receive`` on the remote — a single connection instead
of 5 separate SSH/SCP invocations.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from siftd.domain.sync import PushResult, SyncRemote


class SyncError(Exception):
    """Raised when a sync operation fails."""


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

        if remote.host:
            remote_existed = _push_ssh(remote, slice_path)
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


def _push_ssh(remote: SyncRemote, slice_path: Path) -> bool:
    """Push via single SSH stdin pipe to ``siftd db receive``.

    Streams the slice DB over stdin in a single SSH connection:
        ssh [opts] host "siftd --db <path> db receive --no-fts" < slice.db

    Returns whether the remote DB already existed (status != "created").
    """
    assert remote.host is not None

    remote_db = shlex.quote(remote.path)
    receive_cmd = f"siftd --db {remote_db} db receive --no-fts"

    ssh_opts = _build_ssh_options(remote)
    cmd = ["ssh"] + ssh_opts + [remote.host, receive_cmd]

    from siftd.config import get_config

    timeout_raw = get_config("sync.ssh.connect_timeout_s")
    timeout = 300
    if timeout_raw is not None:
        try:
            timeout = int(timeout_raw)
        except (ValueError, TypeError):
            pass

    try:
        with open(slice_path, "rb") as f:
            result = subprocess.run(
                cmd,
                stdin=f,
                capture_output=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as e:
        raise SyncError(
            f"Push to {remote.host} timed out after {timeout}s. "
            "The remote may be slow or unreachable."
        ) from e
    except OSError as e:
        raise SyncError(_friendly_os_error(remote.host, str(e))) from e

    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise SyncError(_friendly_remote_error(remote.host, remote.path, stderr))

    stdout = result.stdout.decode().strip()
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise SyncError(
            f"Unexpected response from remote: {stdout!r}"
        ) from e

    return response.get("status") != "created"


def _build_ssh_options(remote: SyncRemote) -> list[str]:
    """Build SSH CLI options from config for this remote."""
    from siftd.config import get_ssh_options

    return get_ssh_options(remote.name)


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
        return f"siftd is not installed on {host}. Install with: uv tool install siftd"

    # Try parsing structured JSON error from cmd_db_receive
    try:
        err = json.loads(stderr)
        error_type = err.get("error_type", "")
        error_msg = err.get("error", stderr)
        if error_type == "database_locked":
            return (
                f"Remote database is locked. Another process may be using "
                f"{path} on {host}. Wait and retry."
            )
        return f"Remote error: {error_msg}"
    except (json.JSONDecodeError, ValueError):
        pass

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
