"""Shared peek types used by adapters and the peek module."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PeekScanResult:
    """Lightweight metadata from scanning a session file."""

    session_id: str
    workspace_path: str | None = None
    model: str | None = None
    exchange_count: int = 0
    started_at: str | None = None
    last_activity_at: str | None = None
    parent_session_id: str | None = None


@dataclass
class PeekExchange:
    """A single user→assistant exchange for detail view."""

    timestamp: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    tool_calls: list[tuple[str, int]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionInfo:
    """Session metadata for list display."""

    session_id: str
    file_path: Path
    workspace_path: str | None = None
    workspace_name: str | None = None
    branch: str | None = None
    model: str | None = None
    last_activity: float = 0.0
    exchange_count: int = 0
    preview_available: bool = True
    adapter_name: str | None = None
    parent_session_id: str | None = None


@dataclass
class SessionDetail:
    """Full session detail for detail view."""

    info: SessionInfo
    started_at: str | None = None
    exchanges: list[PeekExchange] = field(default_factory=list)
