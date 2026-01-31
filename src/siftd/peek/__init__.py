"""Peek module: live session inspection bypassing SQLite."""

from siftd.peek.reader import (
    AmbiguousSessionError,
    find_session_file,
    read_session_detail,
    tail_session,
)
from siftd.peek.scanner import (
    list_active_sessions,
)
from siftd.peek.types import (
    PeekExchange,
    PeekScanResult,
    SessionDetail,
    SessionInfo,
)

__all__ = [
    "AmbiguousSessionError",
    "PeekExchange",
    "PeekScanResult",
    "SessionDetail",
    "SessionInfo",
    "find_session_file",
    "list_active_sessions",
    "read_session_detail",
    "tail_session",
]
