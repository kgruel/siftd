"""Gemini CLI adapter for siftd.

Pure parser: reads session JSON files and yields Conversation domain objects.
No storage coupling.
"""

import hashlib
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.adapters._jsonl import now_iso
from siftd.adapters.sdk import canonicalize_tool_name
from siftd.domain import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    Source,
    ToolCall,
    Usage,
)

if TYPE_CHECKING:
    from siftd.peek.types import PeekExchange, PeekScanResult

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
NAME = "gemini_cli"
DEFAULT_LOCATIONS = ["~/.gemini/tmp"]
DEDUP_STRATEGY = "session"  # one conversation per session, latest wins

# Glob pattern for peek discovery (JSON files in chats/ subdirectory)
PEEK_GLOB_PATTERNS = ["*/chats/*.json"]

# Harness metadata
HARNESS_SOURCE = "google"
HARNESS_LOG_FORMAT = "json"
HARNESS_DISPLAY_NAME = "Gemini CLI"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "read_file": "file.read",
    "write_file": "file.write",
    "edit_file": "file.edit",
    "run_shell_command": "shell.execute",
    "search_files": "search.grep",
    "list_files": "file.glob",
}


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Gemini CLI session files."""
    for location in (locations or DEFAULT_LOCATIONS):
        base = Path(location).expanduser()
        if not base.exists():
            continue
        # Gemini stores files as: ~/.gemini/tmp/{hash}/chats/*.json
        for json_file in base.glob("*/chats/*.json"):
            yield Source(kind="file", location=json_file)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    # Gemini session files are JSON in a chats/ subdirectory
    return path.suffix == ".json" and path.parent.name == "chats"


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Gemini CLI session JSON file and yield Conversation objects."""
    path = Path(source.location)
    data = _load_json(path)
    if not data or "messages" not in data:
        return

    # Extract session metadata
    session_id = data.get("sessionId", path.stem)
    project_hash = data.get("projectHash")
    start_time = data.get("startTime")
    last_updated = data.get("lastUpdated")

    # Try to resolve workspace path from project hash
    workspace_path = source.metadata.get("workspace_path")
    if not workspace_path and project_hash:
        # The project hash is in the path: ~/.gemini/tmp/{hash}/chats/...
        # We can also try to reverse-lookup from known paths
        workspace_path = _resolve_workspace_from_hash(project_hash)

    # Build harness
    harness = Harness(
        name=NAME,
        source=HARNESS_SOURCE,
        log_format=HARNESS_LOG_FORMAT,
        display_name=HARNESS_DISPLAY_NAME,
    )

    # Build external_id
    external_id = f"{NAME}::{session_id}"

    # Create conversation
    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=start_time or now_iso(),
        ended_at=last_updated,
        workspace_path=workspace_path,
    )

    # Process messages
    current_prompt: Prompt | None = None

    for message in data.get("messages", []):
        msg_type = message.get("type")
        msg_id = message.get("id")
        timestamp = message.get("timestamp", "")
        content_text = message.get("content", "")

        if msg_type == "user":
            # User prompt
            current_prompt = Prompt(
                timestamp=timestamp,
                external_id=f"{NAME}::{msg_id}" if msg_id else None,
            )

            # Add text content block
            if content_text:
                current_prompt.content.append(
                    ContentBlock(block_type="text", content={"text": content_text})
                )

            conversation.prompts.append(current_prompt)

        elif msg_type == "gemini":
            # Model response
            tokens_data = message.get("tokens", {})
            usage = None
            if tokens_data:
                usage = Usage(
                    input_tokens=tokens_data.get("input"),
                    output_tokens=tokens_data.get("output"),
                )

            model = message.get("model")

            response = Response(
                timestamp=timestamp,
                usage=usage,
                model=model,
                external_id=f"{NAME}::{msg_id}" if msg_id else None,
            )

            # Add thinking blocks from thoughts array
            for thought in message.get("thoughts", []):
                response.content.append(
                    ContentBlock(
                        block_type="thinking",
                        content={
                            "subject": thought.get("subject"),
                            "description": thought.get("description"),
                            "timestamp": thought.get("timestamp"),
                        },
                    )
                )

            # Add main text content
            if content_text:
                response.content.append(
                    ContentBlock(block_type="text", content={"text": content_text})
                )

            # Process tool calls - Gemini embeds results in the same message
            for tool_call_data in message.get("toolCalls", []):
                tool_id = tool_call_data.get("id")
                tool_name = tool_call_data.get("name", "unknown")
                tool_args = tool_call_data.get("args", {})
                tool_status = tool_call_data.get("status", "pending")
                tool_timestamp = tool_call_data.get("timestamp")

                # Extract result from the result array
                result_data = None
                results = tool_call_data.get("result", [])
                if results:
                    # Take the first result's functionResponse
                    func_response = results[0].get("functionResponse", {})
                    response_content = func_response.get("response", {})
                    result_data = response_content

                # Map Gemini status to our status
                status = "success" if tool_status == "success" else tool_status

                tool_call = ToolCall(
                    tool_name=tool_name,
                    input=tool_args,
                    result=result_data,
                    status=status,
                    external_id=tool_id,
                    timestamp=tool_timestamp,
                )
                response.tool_calls.append(tool_call)

                # Also add tool_use content block for completeness
                response.content.append(
                    ContentBlock(
                        block_type="tool_use",
                        content={
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_args,
                        },
                    )
                )

            # Attach response to current prompt
            if current_prompt is not None:
                current_prompt.responses.append(response)

    yield conversation


def _load_json(path: Path) -> dict:
    """Load JSON file."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_workspace_from_hash(project_hash: str) -> str | None:
    """Try to resolve workspace path from project hash.

    The hash is SHA-256 of the absolute path. We can't reverse it directly,
    but we can check common locations.
    """
    # Check common workspace locations
    common_roots = [
        Path.home() / "Code",
        Path.home() / "Projects",
        Path.home() / "code",
        Path.home() / "projects",
        Path.home(),
    ]

    for root in common_roots:
        if not root.exists():
            continue
        # Check immediate children and one level deep
        for path in root.iterdir():
            if path.is_dir():
                if hash_path(str(path)) == project_hash:
                    return str(path)
                # Check one level deeper
                try:
                    for subpath in path.iterdir():
                        if subpath.is_dir() and hash_path(str(subpath)) == project_hash:
                            return str(subpath)
                except PermissionError:
                    continue

    return None


def hash_path(path: str) -> str:
    """Compute the project hash for a given path (SHA-256)."""
    return hashlib.sha256(path.encode()).hexdigest()


# =============================================================================
# Peek hooks — optional live session inspection
# =============================================================================


def peek_scan(path: Path) -> "PeekScanResult | None":
    """Extract lightweight metadata for session listing.

    Gemini CLI stores sessions as single JSON files with a messages array.
    """
    from siftd.peek.types import PeekScanResult

    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not data or "messages" not in data:
        return None

    session_id = data.get("sessionId", path.stem)
    project_hash = data.get("projectHash")
    start_time = data.get("startTime")
    last_updated = data.get("lastUpdated")

    # Try to resolve workspace path from project hash
    workspace_path = None
    if project_hash:
        workspace_path = _resolve_workspace_from_hash(project_hash)

    # Count user messages as exchanges
    exchange_count = 0
    model: str | None = None

    for message in data.get("messages", []):
        if message.get("type") == "user":
            exchange_count += 1
        elif message.get("type") == "gemini":
            model = model or message.get("model")

    if exchange_count == 0:
        return None

    return PeekScanResult(
        session_id=session_id,
        workspace_path=workspace_path,
        model=model,
        exchange_count=exchange_count,
        started_at=start_time,
        last_activity_at=last_updated,
    )


def peek_exchanges(path: Path, last_n: int = 5) -> list["PeekExchange"]:
    """Extract recent exchanges for session detail view."""
    from siftd.peek.types import PeekExchange

    if last_n < 1:
        last_n = 1

    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []

    if not data or "messages" not in data:
        return []

    exchanges: list[PeekExchange] = []
    current_exchange: PeekExchange | None = None

    for message in data.get("messages", []):
        msg_type = message.get("type")
        timestamp = message.get("timestamp", "")
        content_text = message.get("content", "")

        if msg_type == "user":
            current_exchange = PeekExchange(
                timestamp=timestamp,
                prompt_text=content_text if content_text else None,
            )
            exchanges.append(current_exchange)

        elif msg_type == "gemini" and current_exchange is not None:
            tokens_data = message.get("tokens", {})
            current_exchange.input_tokens += tokens_data.get("input", 0)
            current_exchange.output_tokens += tokens_data.get("output", 0)
            current_exchange.response_text = content_text if content_text else None

            # Collect tool calls
            tool_calls: dict[str, int] = {}
            for tool_call_data in message.get("toolCalls", []):
                tool_name = tool_call_data.get("name", "unknown")
                tool_name = canonicalize_tool_name(tool_name, TOOL_ALIASES)
                tool_calls[tool_name] = tool_calls.get(tool_name, 0) + 1

            if tool_calls:
                current_exchange.tool_calls = list(tool_calls.items())

    return exchanges[-last_n:] if len(exchanges) > last_n else exchanges


def peek_tail(path: Path, lines: int = 20) -> Iterator[dict]:
    """Yield last N messages from the session file.

    Gemini CLI uses a single JSON file, so we return the last N messages
    from the messages array.
    """
    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return

    if not data or "messages" not in data:
        return

    messages = data.get("messages", [])
    yield from messages[-lines:]
