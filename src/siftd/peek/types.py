"""Shared types for peek module.

These types are used by both the peek module and adapter peek hooks.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PeekScanResult:
    """Lightweight metadata from scanning a session file.

    Returned by adapter's peek_scan() hook. Contains only the fields
    needed for session listing — no exchange content.
    """

    session_id: str  # Canonical ID — adapter decides (file stem or in-record)
    workspace_path: str | None = None  # cwd/project path
    model: str | None = None  # Last model used
    exchange_count: int = 0  # Number of user turns (real prompts, not tool_results)
    started_at: str | None = None  # Earliest timestamp
    last_activity_at: str | None = None  # Latest timestamp (prefer over mtime)


@dataclass
class PeekExchange:
    """A single user→assistant exchange for detail view.

    Returned by adapter's peek_exchanges() hook.
    """

    timestamp: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    tool_calls: list[tuple[str, int]] = field(default_factory=list)  # (tool_name, count)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionInfo:
    """Session metadata for list display.

    Built from PeekScanResult plus file-level info.
    """

    session_id: str  # Canonical ID from adapter
    file_path: Path
    workspace_path: str | None = None  # Full cwd path
    workspace_name: str | None = None  # Display name (may be disambiguated)
    model: str | None = None
    last_activity: float = 0.0  # Epoch seconds (from in-file timestamp or mtime)
    exchange_count: int = 0
    preview_available: bool = True  # False if adapter doesn't support peek
    adapter_name: str | None = None  # Which adapter owns this session


@dataclass
class SessionDetail:
    """Full session detail for detail view."""

    info: SessionInfo
    started_at: str | None = None
    exchanges: list[PeekExchange] = field(default_factory=list)
