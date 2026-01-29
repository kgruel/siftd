"""Session scanner: discover and extract metadata from active session files."""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from siftd.adapters.registry import load_all_adapters


@dataclass
class SessionInfo:
    session_id: str  # UUID from filename
    file_path: Path
    workspace_path: str | None = None  # cwd from first user message
    workspace_name: str | None = None  # derived short name
    model: str | None = None
    last_activity: float = 0.0  # mtime epoch
    exchange_count: int = 0


def list_active_sessions(
    *,
    workspace: str | None = None,
    threshold_seconds: int = 7200,
    include_inactive: bool = False,
) -> list[SessionInfo]:
    """Discover active session files and extract lightweight metadata.

    Args:
        workspace: Filter by workspace name substring.
        threshold_seconds: Only include files modified within this many seconds.
            Default is 7200 (2 hours).
        include_inactive: If True, include all sessions regardless of mtime.

    Returns:
        List of SessionInfo sorted by last_activity (most recent first).
    """
    adapters = load_all_adapters()
    now = time.time()
    sessions: list[SessionInfo] = []

    for adapter in adapters:
        locations = getattr(adapter, "DEFAULT_LOCATIONS", [])
        for location in locations:
            base = Path(location).expanduser()
            if not base.exists():
                continue
            for jsonl_file in base.glob("**/*.jsonl"):
                stat = jsonl_file.stat()
                age = now - stat.st_mtime

                if not include_inactive and age > threshold_seconds:
                    continue

                info = _scan_session_file(jsonl_file, stat.st_mtime)
                if info is None:
                    continue

                if workspace and not _matches_workspace(info, workspace):
                    continue

                sessions.append(info)

    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions


def _matches_workspace(info: SessionInfo, substr: str) -> bool:
    """Check if session matches workspace filter (case-insensitive substring)."""
    substr_lower = substr.lower()
    if info.workspace_name and substr_lower in info.workspace_name.lower():
        return True
    if info.workspace_path and substr_lower in info.workspace_path.lower():
        return True
    return False


def _scan_session_file(path: Path, mtime: float) -> SessionInfo | None:
    """Extract lightweight metadata from a session file.

    Single pass: reads first user record for cwd/sessionId,
    counts user-type records for exchange count,
    tracks last assistant record for model.
    """
    session_id = path.stem  # UUID from filename
    workspace_path = None
    workspace_name = None
    model = None
    exchange_count = 0

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                record_type = record.get("type")
                if record_type == "user":
                    # Skip tool_result messages — they're not new exchanges
                    msg = record.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    ):
                        continue
                    exchange_count += 1
                    if workspace_path is None:
                        workspace_path = record.get("cwd")
                        session_id_from_record = record.get("sessionId")
                        if session_id_from_record:
                            session_id = session_id_from_record
                elif record_type == "assistant":
                    msg = record.get("message") or {}
                    msg_model = msg.get("model")
                    if msg_model:
                        model = msg_model
    except (OSError, UnicodeDecodeError):
        return None

    if exchange_count == 0:
        return None

    # Derive workspace name from cwd
    if workspace_path:
        workspace_name = Path(workspace_path).name

    return SessionInfo(
        session_id=session_id,
        file_path=path,
        workspace_path=workspace_path,
        workspace_name=workspace_name,
        model=model,
        last_activity=mtime,
        exchange_count=exchange_count,
    )
