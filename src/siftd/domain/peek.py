"""Shared peek types used by adapters and the peek module."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PeekToolCall:
    """Tool call detail for peek narrative rendering.

    external_id (when surfaced by the adapter) is the tool_use id from the
    harness JSONL — useful for matching live tool calls before ingest. The
    internal events.id ULID does not exist for live sessions.
    """

    tool_name: str
    count: int = 1
    input: str | None = None
    result: str | None = None
    status: str = "success"
    external_id: str | None = None


@dataclass
class PeekNarrativeBlock:
    """A single block in a peek assistant narrative."""

    block_type: str
    content: str | None = None
    tool_calls: list[PeekToolCall] = field(default_factory=list)


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
    """A single user→assistant exchange for detail view.

    External IDs (prompt_external_id, response_external_ids, tool_use_ids)
    are the harness-level identifiers from the live session log. Internal
    ULIDs do not exist pre-ingest; consumers needing internal ids must
    wait until the session is ingested and use Phase 4's get_event API.
    """

    timestamp: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    tool_calls: list[tuple[str, int]] = field(default_factory=list)
    narrative: list[PeekNarrativeBlock] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_external_id: str | None = None
    response_external_ids: list[str] = field(default_factory=list)
    tool_use_ids: list[str] = field(default_factory=list)


@dataclass
class SessionInfo:
    """Session metadata for list display.

    started_at is the scan's in-file first timestamp (ISO string) — already
    extracted by every peek_scan into PeekScanResult; carried here so list
    consumers can show session age without a second file read.
    """

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
    started_at: str | None = None


@dataclass
class SessionDetail:
    """Full session detail for detail view."""

    info: SessionInfo
    started_at: str | None = None
    exchanges: list[PeekExchange] = field(default_factory=list)
