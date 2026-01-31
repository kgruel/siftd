"""Template adapter — copy and customize for new log formats.

This is a minimal starting point for writing a siftd adapter.
Copy this file to ~/.config/siftd/adapters/my_harness.py and customize.

Adapters are pure parsers: they read log files and yield Conversation
domain objects. No storage coupling.

Required exports:
    ADAPTER_INTERFACE_VERSION (int): Must be 1.
    NAME (str): Unique adapter identifier (e.g., "my_harness").
    DEFAULT_LOCATIONS (list[str]): Paths to scan when no --path given.
    DEDUP_STRATEGY (str): "file" or "session".
    HARNESS_SOURCE (str): Provider name (e.g., "anthropic", "openai").

    discover(locations=None) -> Iterable[Source]: Find log sources.
    can_handle(source: Source) -> bool: Check if this adapter handles source.
    parse(source: Source) -> Iterable[Conversation]: Parse source into conversations.

Optional exports:
    HARNESS_LOG_FORMAT (str): Log format (e.g., "jsonl", "json").
    HARNESS_DISPLAY_NAME (str): Human-readable name.
    TOOL_ALIASES (dict[str, str]): Map raw tool names to canonical names.

Optional peek hooks (for `siftd peek` support):
    peek_scan(path: Path) -> PeekScanResult | None: Extract lightweight metadata.
    peek_exchanges(path: Path, last_n: int) -> list[PeekExchange]: Get recent exchanges.
    peek_tail(path: Path, lines: int) -> Iterator[dict]: Tail raw records.

    Adapters without peek hooks will still work for ingest, but sessions will
    show "preview unavailable" in peek listings. See docs/writing-adapters.md.
"""

import sys
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters.sdk import (
    ToolCallLinker,
    build_harness,
    discover_files,
    load_jsonl,
    timestamp_bounds,
)
from siftd.domain import (
    ContentBlock,
    Conversation,
    Prompt,
    Response,
    Source,
    ToolCall,
    Usage,
)

# ============================================================================
# Adapter metadata — customize these
# ============================================================================

ADAPTER_INTERFACE_VERSION = 1

# Unique identifier for this adapter
NAME = "my_harness"

# Default paths to scan for log files (~ is expanded)
DEFAULT_LOCATIONS = ["~/.my_harness/logs"]

# Deduplication strategy:
#   "file"    — one conversation per file (most common)
#   "session" — multiple files may update the same conversation (use external_id)
DEDUP_STRATEGY = "file"

# Provider/source name (e.g., "anthropic", "openai", "google", "multi")
HARNESS_SOURCE = "openai"

# Log format (e.g., "jsonl", "json", "markdown")
HARNESS_LOG_FORMAT = "jsonl"

# Human-readable display name (optional)
HARNESS_DISPLAY_NAME = "My Harness"

# Map raw tool names to canonical names (optional)
# Canonical names use dot notation: "file.read", "shell.execute", etc.
TOOL_ALIASES: dict[str, str] = {
    # "raw_tool_name": "canonical.name",
}


# ============================================================================
# Required functions
# ============================================================================


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all log files.

    Called during `siftd ingest` to find files to parse.
    """
    # Use SDK helper for standard file discovery
    yield from discover_files(
        locations,
        DEFAULT_LOCATIONS,
        glob_patterns=["**/*.jsonl"],  # adjust for your format
    )


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source.

    Called to determine which adapter handles a discovered source.
    Be specific to avoid conflicts with other adapters.
    """
    if source.kind != "file":
        return False
    path = Path(source.location)
    # Customize: check file extension, parent directory, or file contents
    return path.suffix == ".jsonl"


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a log file and yield Conversation objects.

    This is the main parsing logic. Customize based on your log format.
    """
    path = Path(source.location)

    # Load JSONL with error handling
    records, errors = load_jsonl(path)
    if errors:
        # Log or handle parse errors
        for e in errors:
            print(f"Warning: {path}:{e.line_number}: {e.error}", file=sys.stderr)
    if not records:
        return

    # Extract timestamp bounds
    started_at, ended_at = timestamp_bounds(records)

    # Build harness metadata
    harness = build_harness(
        NAME,
        HARNESS_SOURCE,
        HARNESS_LOG_FORMAT,
        HARNESS_DISPLAY_NAME,
    )

    # Build external_id — must be stable and unique per conversation
    # Common patterns:
    #   - Session ID from log: f"{NAME}::{session_id}"
    #   - File-based: f"{NAME}::{path.stem}"
    session_id = records[0].get("session_id") or path.stem
    external_id = f"{NAME}::{session_id}"

    # Extract workspace path if available
    workspace_path = records[0].get("cwd")

    # Create conversation shell
    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or _now_iso(),
        ended_at=ended_at,
        workspace_path=workspace_path,
    )

    # Process records into prompts/responses
    # This example assumes records have "role" and "content" fields
    _parse_records(records, conversation)

    # Skip empty conversations
    if not conversation.prompts:
        return

    yield conversation


# ============================================================================
# Parsing helpers — customize for your log format
# ============================================================================


def _parse_records(records: list[dict], conversation: Conversation) -> None:
    """Parse records into Prompt and Response objects on the conversation."""
    current_prompt: Prompt | None = None
    linker = ToolCallLinker()

    for record in records:
        role = record.get("role")
        timestamp = record.get("timestamp", conversation.started_at)
        content = record.get("content", "")

        if role == "user":
            # Check for tool results (Claude-style)
            if _has_tool_result(content):
                _process_tool_results(content, linker)
            else:
                # New user prompt
                current_prompt = Prompt(
                    timestamp=timestamp,
                    external_id=record.get("id"),
                )
                if isinstance(content, str):
                    current_prompt.content.append(
                        ContentBlock(block_type="text", content={"text": content})
                    )
                conversation.prompts.append(current_prompt)

        elif role == "assistant":
            if current_prompt is None:
                continue

            # Extract usage if available
            usage = None
            usage_data = record.get("usage")
            if usage_data:
                usage = Usage(
                    input_tokens=usage_data.get("input_tokens"),
                    output_tokens=usage_data.get("output_tokens"),
                )

            response = Response(
                timestamp=timestamp,
                usage=usage,
                model=record.get("model"),
                external_id=record.get("id"),
            )

            # Parse content blocks
            if isinstance(content, str):
                response.content.append(
                    ContentBlock(block_type="text", content={"text": content})
                )
            elif isinstance(content, list):
                for block in content:
                    response.content.append(
                        ContentBlock(
                            block_type=block.get("type", "unknown"),
                            content=block,
                        )
                    )
                    # Track tool uses for later matching
                    if block.get("type") == "tool_use":
                        linker.add_use(
                            block.get("id"),
                            name=block.get("name"),
                            input=block.get("input", {}),
                            response=response,
                        )

            current_prompt.responses.append(response)

    # Finalize tool calls from linker
    for tool_id, use_data, result_data in linker.get_pairs():
        response = use_data.get("response")
        if response is None:
            continue
        status = "success" if result_data else "pending"
        if result_data and result_data.get("is_error"):
            status = "error"
        tool_call = ToolCall(
            tool_name=use_data.get("name", "unknown"),
            input=use_data.get("input", {}),
            result=result_data.get("content") if result_data else None,
            status=status,
            external_id=tool_id,
        )
        response.tool_calls.append(tool_call)


def _has_tool_result(content) -> bool:
    """Check if content contains tool_result blocks."""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _process_tool_results(content: list, linker: ToolCallLinker) -> None:
    """Process tool_result blocks, adding them to the linker."""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            linker.add_result(
                block.get("tool_use_id"),
                content=block.get("content"),
                is_error=block.get("is_error", False),
            )


def _now_iso() -> str:
    """ISO timestamp for now (UTC)."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# ============================================================================
# Optional: Peek hooks for `siftd peek` support
# ============================================================================
#
# Uncomment and customize if you want `siftd peek` to work with your adapter.
# Without these, the adapter still works for ingest, but sessions show
# "preview unavailable" in peek listings.
#
# from typing import Iterator
# from siftd.peek.types import PeekExchange, PeekScanResult
# from siftd.adapters.sdk import peek_jsonl_scan, peek_jsonl_exchanges, peek_jsonl_tail
#
# def peek_scan(path: Path) -> PeekScanResult | None:
#     """Extract lightweight metadata for session listing."""
#     return peek_jsonl_scan(
#         path,
#         user_type="user",
#         assistant_type="assistant",
#         cwd_key="cwd",
#         session_id_key="session_id",
#         is_tool_result=lambda r: _has_tool_result(r.get("content", [])),
#     )
#
#
# def peek_exchanges(path: Path, last_n: int = 5) -> list[PeekExchange]:
#     """Extract recent exchanges for session detail view."""
#     return peek_jsonl_exchanges(
#         path,
#         last_n,
#         user_type="user",
#         assistant_type="assistant",
#         get_content_blocks=lambda r: r.get("content", []),
#         get_usage=lambda r: (
#             r.get("usage", {}).get("input_tokens", 0),
#             r.get("usage", {}).get("output_tokens", 0),
#         ),
#         is_tool_result=lambda r: _has_tool_result(r.get("content", [])),
#         tool_aliases=TOOL_ALIASES,
#     )
#
#
# def peek_tail(path: Path, lines: int = 20) -> Iterator[dict]:
#     """Yield last N raw records from the session file."""
#     yield from peek_jsonl_tail(path, lines, parse_json=True)
