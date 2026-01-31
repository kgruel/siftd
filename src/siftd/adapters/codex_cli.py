"""Codex CLI adapter for siftd.

Pure parser: reads JSONL session files and yields Conversation domain objects.
No storage coupling.
"""

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.adapters._jsonl import load_jsonl, now_iso
from siftd.adapters.sdk import (
    canonicalize_tool_name,
    peek_jsonl_tail,
)
from siftd.domain import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    Source,
    ToolCall,
)

if TYPE_CHECKING:
    from siftd.peek.types import PeekExchange, PeekScanResult

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
NAME = "codex_cli"
DEFAULT_LOCATIONS = ["~/.codex/sessions"]
DEDUP_STRATEGY = "file"  # one conversation per file

# Harness metadata
HARNESS_SOURCE = "openai"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Codex CLI"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "shell_command": "shell.execute",
    "exec_command": "shell.execute",
    "apply_patch": "file.edit",
    "update_plan": "ui.todo",
    "view_image": "file.read",
    "write_stdin": "shell.stdin",
}


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Codex CLI session files."""
    for location in (locations or DEFAULT_LOCATIONS):
        base = Path(location).expanduser()
        if not base.exists():
            continue
        # Codex stores files as: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
        for jsonl_file in base.glob("**/*.jsonl"):
            yield Source(kind="file", location=jsonl_file)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    # Must be a .jsonl file under a codex sessions directory
    return path.suffix == ".jsonl" and "sessions" in path.parts


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Codex CLI JSONL file and yield Conversation objects."""
    path = Path(source.location)
    records = load_jsonl(path)
    if not records:
        return

    # Extract metadata from session_meta and turn_context
    session_id = None
    session_cwd = None
    model = None
    started_at = None
    ended_at = None

    for record in records:
        record_type = record.get("type")
        ts = record.get("timestamp")

        if record_type == "session_meta":
            payload = record.get("payload", {})
            session_id = payload.get("id")
            session_cwd = payload.get("cwd")
            if not started_at and ts:
                started_at = ts

        elif record_type == "turn_context":
            payload = record.get("payload", {})
            model = model or payload.get("model")

        # Track time bounds from all records
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

    # Build harness
    harness = Harness(
        name=NAME,
        source=HARNESS_SOURCE,
        log_format=HARNESS_LOG_FORMAT,
        display_name=HARNESS_DISPLAY_NAME,
    )

    # Build external_id
    external_id = f"{NAME}::{session_id or path.stem}"

    # Create conversation
    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=session_cwd,
    )

    # Process records into prompts/responses
    # pending_calls tracks function_call/custom_tool_call waiting for output
    # key: call_id, value: (response, tool_name, input_data)
    pending_calls: dict[str, tuple[Response, str, dict | str]] = {}
    current_prompt: Prompt | None = None

    for record in records:
        record_type = record.get("type")
        if record_type != "response_item":
            continue

        payload = record.get("payload", {})
        item_type = payload.get("type")
        timestamp = record.get("timestamp", now_iso())

        if item_type == "message":
            role = payload.get("role")
            content_blocks = payload.get("content", [])

            if role == "user":
                current_prompt = Prompt(timestamp=timestamp)
                for block in content_blocks:
                    current_prompt.content.append(_parse_block(block))
                conversation.prompts.append(current_prompt)

            elif role == "assistant":
                response = Response(
                    timestamp=timestamp,
                    model=model,
                )
                for block in content_blocks:
                    response.content.append(_parse_block(block))
                if current_prompt is not None:
                    current_prompt.responses.append(response)

        elif item_type == "function_call":
            tool_name = payload.get("name", "unknown")
            arguments = payload.get("arguments", "{}")
            call_id = payload.get("call_id")

            # Parse arguments (JSON string)
            input_data = _parse_arguments(arguments)

            # Create a response for the tool call if we don't have one yet
            response = _get_or_create_response(current_prompt, timestamp, model)

            # Add tool_use content block
            response.content.append(ContentBlock(
                block_type="tool_use",
                content={"id": call_id, "name": tool_name, "input": input_data},
            ))

            if call_id:
                pending_calls[call_id] = (response, tool_name, input_data)

        elif item_type == "function_call_output":
            call_id = payload.get("call_id")
            output = payload.get("output", "")

            if call_id and call_id in pending_calls:
                response, tool_name, input_data = pending_calls.pop(call_id)
                tool_call = ToolCall(
                    tool_name=tool_name,
                    input=input_data if isinstance(input_data, dict) else {"raw": input_data},
                    result={"output": output},
                    status="success",
                    external_id=call_id,
                    timestamp=timestamp,
                )
                response.tool_calls.append(tool_call)

        elif item_type == "custom_tool_call":
            tool_name = payload.get("name", "unknown")
            input_data = payload.get("input", "")
            call_id = payload.get("call_id")

            response = _get_or_create_response(current_prompt, timestamp, model)

            response.content.append(ContentBlock(
                block_type="tool_use",
                content={"id": call_id, "name": tool_name, "input": input_data},
            ))

            if call_id:
                pending_calls[call_id] = (response, tool_name, input_data)

        elif item_type == "custom_tool_call_output":
            call_id = payload.get("call_id")
            output = payload.get("output", "")

            if call_id and call_id in pending_calls:
                response, tool_name, input_data = pending_calls.pop(call_id)
                tool_call = ToolCall(
                    tool_name=tool_name,
                    input=input_data if isinstance(input_data, dict) else {"raw": input_data},
                    result={"output": output},
                    status="success",
                    external_id=call_id,
                    timestamp=timestamp,
                )
                response.tool_calls.append(tool_call)

    # Handle pending tool calls that never got output
    for call_id, (response, tool_name, input_data) in pending_calls.items():
        tool_call = ToolCall(
            tool_name=tool_name,
            input=input_data if isinstance(input_data, dict) else {"raw": input_data},
            result=None,
            status="pending",
            external_id=call_id,
            timestamp=None,
        )
        response.tool_calls.append(tool_call)

    yield conversation


def _parse_block(block) -> ContentBlock:
    """Parse content block into a ContentBlock domain object."""
    if isinstance(block, str):
        return ContentBlock(block_type="text", content={"text": block})
    block_type = block.get("type", "unknown")
    # Normalize Codex block types to common types
    if block_type == "input_text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    if block_type == "output_text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    return ContentBlock(block_type=block_type, content=block)


def _parse_arguments(arguments: str) -> dict:
    """Parse function_call arguments (JSON string) into a dict."""
    try:
        return json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {"raw": arguments}


def _get_or_create_response(
    current_prompt: Prompt | None, timestamp: str, model: str | None
) -> Response:
    """Get the latest response on the current prompt, or create one."""
    if current_prompt is not None and current_prompt.responses:
        return current_prompt.responses[-1]
    response = Response(timestamp=timestamp, model=model)
    if current_prompt is not None:
        current_prompt.responses.append(response)
    return response


# =============================================================================
# Peek hooks — optional live session inspection
# =============================================================================


def peek_scan(path: Path) -> "PeekScanResult | None":
    """Extract lightweight metadata for session listing.

    Codex CLI uses a different schema than Claude Code:
    - session_meta record contains id and cwd
    - response_item records with payload.type="message" and role="user" are exchanges
    - turn_context contains model info
    """
    from siftd.peek.types import PeekScanResult

    session_id = path.stem
    workspace_path: str | None = None
    model: str | None = None
    exchange_count = 0
    started_at: str | None = None
    last_activity_at: str | None = None

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
                ts = record.get("timestamp")

                # Track timestamp bounds
                if ts:
                    if started_at is None or ts < started_at:
                        started_at = ts
                    if last_activity_at is None or ts > last_activity_at:
                        last_activity_at = ts

                if record_type == "session_meta":
                    payload = record.get("payload", {})
                    session_id = payload.get("id") or session_id
                    workspace_path = payload.get("cwd")

                elif record_type == "turn_context":
                    payload = record.get("payload", {})
                    model = model or payload.get("model")

                elif record_type == "response_item":
                    payload = record.get("payload", {})
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        exchange_count += 1

    except (OSError, UnicodeDecodeError):
        return None

    if exchange_count == 0:
        return None

    return PeekScanResult(
        session_id=session_id,
        workspace_path=workspace_path,
        model=model,
        exchange_count=exchange_count,
        started_at=started_at,
        last_activity_at=last_activity_at,
    )


def peek_exchanges(path: Path, last_n: int = 5) -> list["PeekExchange"]:
    """Extract recent exchanges for session detail view."""
    from siftd.peek.types import PeekExchange

    if last_n < 1:
        last_n = 1

    exchanges: list[PeekExchange] = []
    current_exchange: PeekExchange | None = None
    model: str | None = None

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
                timestamp = record.get("timestamp")

                if record_type == "turn_context":
                    payload = record.get("payload", {})
                    model = model or payload.get("model")

                elif record_type == "response_item":
                    payload = record.get("payload", {})
                    item_type = payload.get("type")

                    if item_type == "message":
                        role = payload.get("role")
                        content_blocks = payload.get("content", [])

                        if role == "user":
                            current_exchange = PeekExchange(
                                timestamp=timestamp,
                                prompt_text=_extract_codex_text(content_blocks),
                            )
                            exchanges.append(current_exchange)

                        elif role == "assistant" and current_exchange is not None:
                            current_exchange.response_text = _extract_codex_text(
                                content_blocks
                            )

                    elif item_type in ("function_call", "custom_tool_call"):
                        if current_exchange is not None:
                            tool_name = payload.get("name", "unknown")
                            tool_name = canonicalize_tool_name(tool_name, TOOL_ALIASES)
                            # Add to tool_calls counter
                            existing = dict(current_exchange.tool_calls)
                            existing[tool_name] = existing.get(tool_name, 0) + 1
                            current_exchange.tool_calls = list(existing.items())

    except (OSError, UnicodeDecodeError):
        return []

    return exchanges[-last_n:] if len(exchanges) > last_n else exchanges


def peek_tail(path: Path, lines: int = 20) -> Iterator[dict]:
    """Yield last N raw records from the session file."""
    yield from peek_jsonl_tail(path, lines, parse_json=True)


def _extract_codex_text(blocks: list) -> str | None:
    """Extract text from Codex content blocks."""
    parts: list[str] = []

    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type in ("input_text", "output_text", "text"):
                text = block.get("text", "")
                if text:
                    parts.append(text)
            elif block_type == "image":
                parts.append("[image]")
            elif block_type:
                parts.append(f"[{block_type}]")

    return "\n".join(parts) if parts else None
