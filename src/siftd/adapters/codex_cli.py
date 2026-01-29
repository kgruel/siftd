"""Codex CLI adapter for siftd.

Pure parser: reads JSONL session files and yields Conversation domain objects.
No storage coupling.
"""

import json
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso
from siftd.domain import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    Source,
    ToolCall,
)

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
NAME = "codex_cli"
DEFAULT_LOCATIONS = ["~/.codex/sessions"]
SOURCE_KINDS = ["file"]
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
