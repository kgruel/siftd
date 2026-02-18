"""Copilot CLI adapter for siftd.

Pure parser: reads JSONL event files and yields Conversation domain objects.
No storage coupling.
"""

import json
import sys
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso
from siftd.adapters.sdk import discover_files
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
NAME = "copilot_cli"
DEDUP_STRATEGY = "file"  # one conversation per events.jsonl

# Platform-specific locations
if sys.platform == "win32":
    DEFAULT_LOCATIONS = ["~/AppData/Local/.copilot/session-state"]
else:
    DEFAULT_LOCATIONS = ["~/.local/state/.copilot/session-state"]

# Harness metadata
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Copilot CLI"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {}


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Copilot CLI event files."""
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/events.jsonl"])


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.name != "events.jsonl":
        return False
    path_str = str(path)
    for loc in DEFAULT_LOCATIONS:
        loc_expanded = str(Path(loc).expanduser())
        if loc_expanded in path_str:
            return True
    # Accept paths with .copilot/session-state component (for tests/mock paths)
    if ".copilot" in path.parts and "session-state" in path.parts:
        return True
    return False


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Copilot CLI events.jsonl file and yield Conversation objects."""
    path = Path(source.location)
    records = load_jsonl(path)
    if not records:
        return

    # Extract session metadata
    session_id = None
    session_cwd = None
    branch = None
    model = None
    started_at = None
    ended_at = None

    for record in records:
        event_type = record.get("type")
        ts = record.get("timestamp")
        data = record.get("data", {})

        if event_type == "session.start":
            session_id = data.get("sessionId")
            context = data.get("context", {})
            session_cwd = context.get("cwd")
            branch = context.get("branch")
            if ts:
                started_at = ts

        elif event_type == "session.model_change":
            model = model or data.get("newModel")

        # Track time bounds
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

    external_id = f"{NAME}::{session_id or path.parent.name}"

    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=session_cwd,
        branch=branch,
    )

    # Process events into prompts/responses
    current_prompt: Prompt | None = None
    # pending tool calls: call_id → (response, tool_name, input_data)
    pending_calls: dict[str, tuple[Response, str, dict]] = {}

    for record in records:
        event_type = record.get("type")
        ts = record.get("timestamp", now_iso())
        data = record.get("data", {})

        if event_type == "user.message":
            current_prompt = Prompt(timestamp=ts)
            content_text = data.get("content", "")
            if content_text:
                current_prompt.content.append(
                    ContentBlock(block_type="text", content={"text": content_text})
                )
            conversation.prompts.append(current_prompt)

        elif event_type == "assistant.message":
            response = Response(
                timestamp=ts,
                model=model,
            )

            # Content text
            content_text = data.get("content", "")
            if content_text:
                response.content.append(
                    ContentBlock(block_type="text", content={"text": content_text})
                )

            # Reasoning
            reasoning = data.get("reasoningText")
            if reasoning:
                response.content.append(
                    ContentBlock(block_type="thinking", content={"text": reasoning})
                )

            # Tool requests
            for req in data.get("toolRequests", []):
                call_id = req.get("toolCallId")
                tool_name = req.get("name", "unknown")
                arguments_raw = req.get("arguments", "{}")
                if isinstance(arguments_raw, str):
                    try:
                        arguments = json.loads(arguments_raw)
                    except (json.JSONDecodeError, TypeError):
                        arguments = {"raw": arguments_raw}
                else:
                    arguments = arguments_raw if isinstance(arguments_raw, dict) else {}

                response.content.append(ContentBlock(
                    block_type="tool_use",
                    content={"id": call_id, "name": tool_name, "input": arguments},
                ))

                if call_id:
                    pending_calls[call_id] = (response, tool_name, arguments)

            if current_prompt is not None:
                current_prompt.responses.append(response)

        elif event_type == "tool.execution_complete":
            call_id = data.get("toolCallId")
            success = data.get("success", False)
            result_data = data.get("result", {})

            if call_id and call_id in pending_calls:
                resp, tool_name, input_data = pending_calls.pop(call_id)
                tool_call = ToolCall(
                    tool_name=tool_name,
                    input=input_data,
                    result=result_data if result_data else None,
                    status="success" if success else "error",
                    external_id=call_id,
                    timestamp=ts,
                )
                resp.tool_calls.append(tool_call)

    # Handle pending tool calls that never got results
    for call_id, (resp, tool_name, input_data) in pending_calls.items():
        tool_call = ToolCall(
            tool_name=tool_name,
            input=input_data,
            result=None,
            status="pending",
            external_id=call_id,
            timestamp=None,
        )
        resp.tool_calls.append(tool_call)

    yield conversation
