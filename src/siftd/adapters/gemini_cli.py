"""Gemini CLI adapter for siftd.

Pure parser: reads session JSON files and yields Conversation domain objects.
No storage coupling.
"""

import hashlib
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from siftd.adapters._jsonl import now_iso
from siftd.adapters.sdk import (
    AdapterParseError,
    NormalizedRecord,
    build_harness,
    make_peek_hooks,
    yield_conversation,
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
    if path.suffix != ".json":
        return False
    # Check if file is under Gemini CLI's tmp directory or in a chats/ subdirectory
    path_str = str(path)
    # Check actual location
    for loc in DEFAULT_LOCATIONS:
        loc_expanded = str(Path(loc).expanduser())
        if loc_expanded in path_str:
            return True
    # Also accept if in a chats/ directory (for tests/mock paths)
    if path.parent.name == "chats":
        return True
    return False


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Gemini CLI session JSON file and yield Conversation objects."""
    path = Path(source.location)
    data = _load_json_strict(path)
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise AdapterParseError(
            f"Gemini CLI source {path} is missing a messages array"
        )

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
    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

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

    for message in messages:
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

    yield from yield_conversation(conversation)


def _load_json(path: Path) -> dict | None:
    """Load JSON file, returning None on error."""
    from siftd.safecall import load_json

    return load_json(path, context="gemini_cli")


def _load_json_strict(path: Path) -> dict:
    """Load and validate a Gemini CLI session file for ingest."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AdapterParseError(
            f"Gemini CLI source {path} could not be read: {e}"
        ) from e

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise AdapterParseError(
            f"Gemini CLI source {path} contains invalid JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise AdapterParseError(
            f"Gemini CLI source {path} must contain a JSON object"
        )

    return data


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
# Record normalization — enables SDK-derived peek support
# =============================================================================


def iter_gemini_records(path: Path) -> Iterator[dict]:
    """Iterate synthetic records from a Gemini CLI session JSON file.

    Yields a metadata record (session-level fields), then each message
    from the messages array as-is (they already have a "type" field).
    """
    data = _load_json(path)
    if not data or "messages" not in data:
        return

    project_hash = data.get("projectHash")
    yield {
        "_kind": "metadata",
        "sessionId": data.get("sessionId", path.stem),
        "startTime": data.get("startTime"),
        "lastUpdated": data.get("lastUpdated"),
        "projectHash": project_hash,
    }

    yield from data.get("messages", [])


def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map a Gemini CLI record to NormalizedRecord.

    Record types:
        "_kind": "metadata" → metadata (sessionId, workspace from projectHash)
        "type": "user"      → user (prompt text)
        "type": "gemini"    → assistant (response text, thoughts, tool calls, usage)
    """
    # Synthetic metadata record from iter_gemini_records
    if raw.get("_kind") == "metadata":
        workspace = None
        project_hash = raw.get("projectHash")
        if project_hash:
            workspace = _resolve_workspace_from_hash(project_hash)
        return NormalizedRecord(
            kind="metadata",
            timestamp=raw.get("startTime"),
            session_id=raw.get("sessionId"),
            workspace_path=workspace,
            extra={"lastUpdated": raw.get("lastUpdated")},
        )

    msg_type = raw.get("type")
    timestamp = raw.get("timestamp", "")

    if msg_type == "user":
        content_text = raw.get("content", "")
        content_blocks = [{"type": "text", "text": content_text}] if content_text else []
        return NormalizedRecord(
            kind="user",
            timestamp=timestamp,
            content_blocks=content_blocks,
        )

    if msg_type == "gemini":
        content_blocks: list[dict] = []

        # Thinking blocks from thoughts array
        for thought in raw.get("thoughts", []):
            subject = thought.get("subject")
            description = thought.get("description")
            if subject and description:
                text = f"{subject}: {description}"
            else:
                text = description or subject or ""
            if text:
                content_blocks.append({"type": "thinking", "text": text})

        # Main text content
        content_text = raw.get("content", "")
        if content_text:
            content_blocks.append({"type": "text", "text": content_text})

        # Tool calls as tool_use blocks
        for tc in raw.get("toolCalls", []):
            content_blocks.append({
                "type": "tool_use",
                "name": tc.get("name", "unknown"),
                "input": tc.get("args", {}),
            })

        tokens = raw.get("tokens") or {}
        return NormalizedRecord(
            kind="assistant",
            timestamp=timestamp,
            content_blocks=content_blocks,
            model=raw.get("model"),
            input_tokens=tokens.get("input", 0) or 0,
            output_tokens=tokens.get("output", 0) or 0,
        )

    return None


# Peek hooks — derived from normalizer with custom JSON iterator
peek_scan, peek_exchanges, _peek_tail = make_peek_hooks(
    normalize_record,
    tool_aliases=TOOL_ALIASES,
    record_iterator=iter_gemini_records,
)


def peek_tail(path: Path, lines: int = 20) -> Iterator[dict]:
    """Yield last N messages from the session JSON.

    Custom tail since Gemini uses a single JSON file, not JSONL.
    """
    data = _load_json(path)
    if not data or "messages" not in data:
        return

    messages = data.get("messages", [])
    yield from messages[-lines:]
