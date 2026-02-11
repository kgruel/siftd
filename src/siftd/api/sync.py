"""Push local conversations to a remote siftd database.

Wraps slice_database + transport (scp/ssh merge or local copy/merge)
into a single workflow. First push creates the remote DB directly;
subsequent pushes merge the delta.
"""

from __future__ import annotations

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
    """Push via scp + ssh merge. Returns whether the remote DB existed."""
    assert remote.host is not None

    remote_existed = _remote_file_exists(remote.host, remote.path)

    if not remote_existed:
        # First push: scp the slice directly as the DB
        _scp_to(slice_path, remote.host, remote.path)
        return False

    # Subsequent push: scp to temp, ssh merge, clean up
    _require_remote_siftd(remote.host)

    remote_tmp = f"{remote.path}.siftd-push-tmp"
    remote_db = shlex.quote(remote.path)
    remote_tmp_q = shlex.quote(remote_tmp)
    _scp_to(slice_path, remote.host, remote_tmp)

    try:
        _ssh_run(
            remote.host,
            f"siftd --db {remote_db} db merge {remote_tmp_q} --no-fts",
        )
    except SyncError:
        # Clean up temp on failure
        try:
            _ssh_run(remote.host, f"rm -f -- {remote_tmp_q}")
        except SyncError:
            pass
        raise
    else:
        try:
            _ssh_run(remote.host, f"rm -f -- {remote_tmp_q}")
        except SyncError:
            pass

    return True


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


def _require_remote_siftd(host: str) -> None:
    """Check that siftd is available on the remote host."""
    try:
        subprocess.run(
            ["ssh", host, "command", "-v", "siftd"],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode().strip() if e.stderr else ""
        detail = f" SSH error: {stderr}" if stderr else ""
        raise SyncError(
            f"Remote '{host}' does not have siftd installed."
            f"{detail} Install it there, or push to a new path (first push doesn't require it)."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise SyncError(f"SSH connection to '{host}' timed out.") from e
    except OSError as e:
        raise SyncError(f"SSH failed for '{host}': {e}") from e


def _remote_file_exists(host: str, path: str) -> bool:
    """Check whether a file exists on the remote host."""
    try:
        result = subprocess.run(
            ["ssh", host, "test", "-f", path],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise SyncError(f"SSH connection to '{host}' timed out.") from e
    except OSError as e:
        raise SyncError(f"SSH failed for '{host}': {e}") from e
    return result.returncode == 0


def _scp_to(local_path: Path, host: str, remote_path: str) -> None:
    """Copy a local file to a remote host via scp."""
    try:
        result = subprocess.run(
            ["scp", str(local_path), f"{host}:{remote_path}"],
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        raise SyncError(f"scp to {host}:{remote_path} timed out.") from e
    except OSError as e:
        raise SyncError(f"scp to {host}:{remote_path} failed: {e}") from e
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise SyncError(f"scp to {host}:{remote_path} failed: {stderr}")


def _ssh_run(host: str, command: str) -> str:
    """Run a command on a remote host via ssh. Returns stdout."""
    try:
        result = subprocess.run(
            ["ssh", host, command],
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise SyncError(f"SSH command on {host} timed out.") from e
    except OSError as e:
        raise SyncError(f"SSH failed for '{host}': {e}") from e
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise SyncError(f"SSH command failed on {host}: {stderr}")
    return result.stdout.decode()
