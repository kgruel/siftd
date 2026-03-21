"""Pi Coding Agent adapter for siftd.

Pure parser: reads JSONL session files and yields Conversation domain objects.
No storage coupling.
"""

import json
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso
from siftd.adapters.sdk import NormalizedRecord, build_harness, discover_files, flush_pending_calls, make_peek_hooks
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
NAME = "pi_agent"
DEFAULT_LOCATIONS = ["~/.pi/agent/sessions"]
DEDUP_STRATEGY = "file"  # one conversation per file

# Harness metadata
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Pi Coding Agent"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {}


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Pi Agent session files."""
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/*.jsonl"])


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.suffix != ".jsonl":
        return False
    path_str = str(path)
    for loc in DEFAULT_LOCATIONS:
        loc_expanded = str(Path(loc).expanduser())
        if loc_expanded in path_str:
            return True
    # Accept paths with .pi/agent/sessions component (for tests/mock paths)
    if ".pi" in path.parts:
        parent_parts = path.parts
        for i, part in enumerate(parent_parts):
            if part == ".pi" and i + 2 < len(parent_parts):
                if parent_parts[i + 1] == "agent" and parent_parts[i + 2] == "sessions":
                    return True
    return False


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Pi Agent JSONL file and yield Conversation objects."""
    path = Path(source.location)
    records = load_jsonl(path)
    if not records:
        return

    # Extract session metadata from the session header
    session_id = None
    session_cwd = None
    model = None
    started_at = None
    ended_at = None

    for record in records:
        record_type = record.get("type")
        ts = record.get("timestamp")

        if record_type == "session":
            session_id = record.get("id")
            session_cwd = record.get("cwd")
            if ts:
                started_at = ts

        elif record_type == "model_change":
            model = model or record.get("modelId")

        # Track time bounds
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

    # Build harness
    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    external_id = f"{NAME}::{session_id or path.stem}"

    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=session_cwd,
    )

    # Process message records into prompts/responses
    current_prompt: Prompt | None = None
    # pending tool calls: call_id → (response, tool_name, input_data)
    pending_calls: dict[str, tuple[Response, str, dict]] = {}

    for record in records:
        record_type = record.get("type")
        if record_type != "message":
            continue

        msg = record.get("message", {})
        role = msg.get("role")
        timestamp = record.get("timestamp", now_iso())

        if role == "user":
            current_prompt = Prompt(timestamp=timestamp)
            for block in msg.get("content", []):
                current_prompt.content.append(_parse_block(block))
            conversation.prompts.append(current_prompt)

        elif role == "assistant":
            response = Response(
                timestamp=timestamp,
                model=msg.get("model") or model,
            )

            # Extract usage
            usage_data = msg.get("usage")
            if usage_data:
                response.usage = Usage(
                    input_tokens=usage_data.get("input"),
                    output_tokens=usage_data.get("output"),
                )
                # Cache tokens as attributes
                cache_read = usage_data.get("cacheRead")
                if cache_read is not None:
                    response.attributes["cache_read_input_tokens"] = str(cache_read)
                cache_write = usage_data.get("cacheWrite")
                if cache_write is not None:
                    response.attributes["cache_creation_input_tokens"] = str(cache_write)

            # Cost as attributes
            cost_data = (usage_data or {}).get("cost")
            if cost_data:
                total_cost = cost_data.get("total")
                if total_cost is not None:
                    response.attributes["cost"] = str(total_cost)

            # Parse content blocks
            for block in msg.get("content", []):
                block_type = block.get("type", "")
                if block_type == "toolCall":
                    call_id = block.get("id")
                    tool_name = block.get("name", "unknown")
                    arguments = block.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except (json.JSONDecodeError, TypeError):
                            arguments = {"raw": arguments}

                    response.content.append(ContentBlock(
                        block_type="tool_use",
                        content={"id": call_id, "name": tool_name, "input": arguments},
                    ))
                    if call_id:
                        pending_calls[call_id] = (response, tool_name, arguments)
                elif block_type == "thinking":
                    response.content.append(ContentBlock(
                        block_type="thinking",
                        content={"text": block.get("thinking", "")},
                    ))
                else:
                    response.content.append(_parse_block(block))

            if current_prompt is not None:
                current_prompt.responses.append(response)

        elif role == "toolResult":
            call_id = msg.get("toolCallId")
            tool_name = msg.get("toolName", "unknown")
            is_error = msg.get("isError", False)
            result_content = msg.get("content", [])
            result_text = _extract_text(result_content)

            if call_id and call_id in pending_calls:
                resp, t_name, input_data = pending_calls.pop(call_id)
                tool_call = ToolCall(
                    tool_name=t_name,
                    input=input_data,
                    result={"output": result_text} if result_text else None,
                    status="error" if is_error else "success",
                    external_id=call_id,
                    timestamp=timestamp,
                )
                resp.tool_calls.append(tool_call)

    # Handle pending tool calls that never got results
    flush_pending_calls(pending_calls)

    yield conversation


def _parse_block(block) -> ContentBlock:
    """Parse content block into a ContentBlock domain object."""
    if isinstance(block, str):
        return ContentBlock(block_type="text", content={"text": block})
    block_type = block.get("type", "unknown")
    if block_type == "text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    return ContentBlock(block_type=block_type, content=block)


def _extract_text(content_blocks: list) -> str | None:
    """Extract text from content blocks."""
    parts: list[str] = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts) if parts else None


# =============================================================================
# Record normalization — enables SDK-derived peek support
# =============================================================================


def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map a Pi Agent native record to NormalizedRecord.

    Pi Agent record types:
        "session"      → metadata (id, cwd)
        "model_change" → metadata (modelId)
        "message" with role "user"       → user
        "message" with role "assistant"  → assistant
        "message" with role "toolResult" → tool_result (skip for exchange counting)
    """
    record_type = raw.get("type")
    ts = raw.get("timestamp")

    if record_type == "session":
        return NormalizedRecord(
            kind="metadata",
            timestamp=ts,
            session_id=raw.get("id"),
            workspace_path=raw.get("cwd"),
        )

    if record_type == "model_change":
        return NormalizedRecord(
            kind="metadata",
            timestamp=ts,
            model=raw.get("modelId"),
        )

    if record_type != "message":
        return None

    msg = raw.get("message", {})
    role = msg.get("role")
    content_blocks = msg.get("content", [])

    if role == "user":
        return NormalizedRecord(
            kind="user",
            timestamp=ts,
            content_blocks=content_blocks,
        )

    if role == "assistant":
        usage = msg.get("usage") or {}
        return NormalizedRecord(
            kind="assistant",
            timestamp=ts,
            content_blocks=content_blocks,
            model=msg.get("model"),
            input_tokens=usage.get("input", 0) or 0,
            output_tokens=usage.get("output", 0) or 0,
        )

    if role == "toolResult":
        return NormalizedRecord(kind="tool_result", timestamp=ts)

    return None


# Peek hooks — derived from normalizer
peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    normalize_record,
    tool_aliases=TOOL_ALIASES,
)
