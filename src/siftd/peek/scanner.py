"""Session scanner: discover and extract metadata from active session files."""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from siftd.adapters.registry import load_all_adapters
from siftd.peek.types import PeekScanResult, SessionInfo

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredFile:
    """A file discovered during scanning, with its owning adapter."""

    path: Path
    mtime: float
    adapter_module: object
    adapter_name: str


def list_active_sessions(
    *,
    workspace: str | None = None,
    threshold_seconds: int = 7200,
    include_inactive: bool = False,
    limit: int | None = None,
) -> list[SessionInfo]:
    """Discover active session files and extract lightweight metadata.

    Args:
        workspace: Filter by workspace name substring.
        threshold_seconds: Only include files modified within this many seconds.
            Default is 7200 (2 hours).
        include_inactive: If True, include all sessions regardless of mtime.
        limit: Maximum number of sessions to return.

    Returns:
        List of SessionInfo sorted by last_activity (most recent first).
    """
    discovered = _discover_files(threshold_seconds, include_inactive)
    sessions: list[SessionInfo] = []

    for file_info in discovered:
        try:
            info = _scan_session_file(file_info)
            if info is None:
                continue

            if workspace and not _matches_workspace(info, workspace):
                continue

            sessions.append(info)
        except Exception as e:
            # Per-file error handling: log and continue
            logger.debug(
                "Error scanning %s: %s", file_info.path, e, exc_info=True
            )
            continue

    sessions.sort(key=lambda s: s.last_activity, reverse=True)

    # Detect workspace name collisions and disambiguate
    _disambiguate_workspace_names(sessions)

    if limit is not None and limit > 0:
        sessions = sessions[:limit]

    return sessions


def _discover_files(
    threshold_seconds: int, include_inactive: bool
) -> list[DiscoveredFile]:
    """Discover session files from all adapters that support peek."""
    plugins = load_all_adapters()
    now = time.time()
    discovered: list[DiscoveredFile] = []

    for plugin in plugins:
        module = plugin.module
        adapter_name = getattr(module, "NAME", plugin.name)

        # Only process adapters that have peek_scan hook
        if not hasattr(module, "peek_scan"):
            continue

        locations = getattr(module, "DEFAULT_LOCATIONS", [])
        glob_patterns = _get_glob_patterns(module)

        for location in locations:
            try:
                base = Path(location).expanduser()
                if not base.exists():
                    continue

                for pattern in glob_patterns:
                    for session_file in base.glob(pattern):
                        try:
                            stat = session_file.stat()
                            age = now - stat.st_mtime

                            if not include_inactive and age > threshold_seconds:
                                continue

                            discovered.append(
                                DiscoveredFile(
                                    path=session_file,
                                    mtime=stat.st_mtime,
                                    adapter_module=module,
                                    adapter_name=adapter_name,
                                )
                            )
                        except OSError as e:
                            logger.debug(
                                "Error stat'ing %s: %s", session_file, e
                            )
                            continue
            except OSError as e:
                logger.debug("Error accessing %s: %s", location, e)
                continue

    return discovered


def _get_glob_patterns(module) -> list[str]:
    """Get glob patterns for an adapter.

    Adapters can define PEEK_GLOB_PATTERNS, otherwise we infer from log format.
    """
    if hasattr(module, "PEEK_GLOB_PATTERNS"):
        return module.PEEK_GLOB_PATTERNS

    log_format = getattr(module, "HARNESS_LOG_FORMAT", "jsonl")
    if log_format == "jsonl":
        return ["**/*.jsonl"]
    elif log_format == "json":
        return ["**/*.json"]
    elif log_format == "markdown":
        return ["**/*.md"]
    else:
        return ["**/*"]


def _scan_session_file(file_info: DiscoveredFile) -> SessionInfo | None:
    """Delegate to adapter's peek_scan to extract metadata."""
    module = file_info.adapter_module

    peek_scan = getattr(module, "peek_scan", None)
    if peek_scan is None:
        # Adapter doesn't support peek — mark as unavailable
        return SessionInfo(
            session_id=file_info.path.stem,
            file_path=file_info.path,
            last_activity=file_info.mtime,
            preview_available=False,
            adapter_name=file_info.adapter_name,
        )

    try:
        result: PeekScanResult | None = peek_scan(file_info.path)
    except Exception as e:
        logger.debug(
            "peek_scan failed for %s: %s", file_info.path, e, exc_info=True
        )
        return None

    if result is None:
        return None

    # Determine last_activity: prefer in-file timestamp over mtime
    last_activity = file_info.mtime
    if result.last_activity_at:
        try:
            # Parse ISO timestamp to epoch
            dt = datetime.fromisoformat(result.last_activity_at.replace("Z", "+00:00"))
            last_activity = dt.timestamp()
        except (ValueError, TypeError):
            pass

    workspace_name = None
    if result.workspace_path:
        workspace_name = Path(result.workspace_path).name

    return SessionInfo(
        session_id=result.session_id,
        file_path=file_info.path,
        workspace_path=result.workspace_path,
        workspace_name=workspace_name,
        model=result.model,
        last_activity=last_activity,
        exchange_count=result.exchange_count,
        preview_available=True,
        adapter_name=file_info.adapter_name,
    )


def _matches_workspace(info: SessionInfo, substr: str) -> bool:
    """Check if session matches workspace filter (case-insensitive substring)."""
    substr_lower = substr.lower()
    if info.workspace_name and substr_lower in info.workspace_name.lower():
        return True
    if info.workspace_path and substr_lower in info.workspace_path.lower():
        return True
    return False


def _disambiguate_workspace_names(sessions: list[SessionInfo]) -> None:
    """Detect and fix workspace name collisions.

    When multiple sessions have the same workspace_name but different
    workspace_path values, update workspace_name to include enough
    parent directories to disambiguate.
    """
    # Group sessions by workspace_name
    by_name: dict[str, list[SessionInfo]] = defaultdict(list)
    for session in sessions:
        if session.workspace_name:
            by_name[session.workspace_name].append(session)

    for name, group in by_name.items():
        if len(group) <= 1:
            continue

        # Check if paths are actually different
        paths = {s.workspace_path for s in group if s.workspace_path}
        if len(paths) <= 1:
            continue

        # Need to disambiguate — add parent directories
        for session in group:
            if session.workspace_path:
                session.workspace_name = _disambiguated_name(
                    session.workspace_path, paths
                )


def _disambiguated_name(path: str, all_paths: set[str]) -> str:
    """Generate a disambiguated workspace name.

    Adds parent directory components until unique among all_paths.
    """
    path_obj = Path(path)
    parts = path_obj.parts

    # Start with just the name
    for i in range(1, len(parts)):
        candidate = "/".join(parts[-i:])
        # Check if this is unique among all paths
        matches = sum(
            1 for p in all_paths if Path(p).parts[-i:] == parts[-i:]
        )
        if matches == 1:
            return candidate

    # If we can't disambiguate, return the full path
    return path
