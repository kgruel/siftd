"""Goose adapter for tbd-v2.

Pure parser: reads SQLite sessions database and yields Conversation domain objects.
No storage coupling.
"""

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from domain import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    ToolCall,
    Usage,
)
from domain.source import Source

# Adapter self-description
NAME = "goose"
DEFAULT_LOCATIONS = ["~/.local/share/goose/sessions/sessions.db"]
SOURCE_KINDS = ["session"]
DEDUP_STRATEGY = "session"  # one conversation per session, dedup by session ID

# Harness metadata
HARNESS_SOURCE = "block"
HARNESS_LOG_FORMAT = "sqlite"
HARNESS_DISPLAY_NAME = "Goose"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "developer__text_editor": "file.edit",
    "developer__shell": "shell.execute",
    "developer__read_file": "file.read",
    "developer__write_file": "file.write",
    "developer__list_directory": "file.glob",
    "developer__search_files": "search.grep",
    "developer__browser": "web.fetch",
}


def discover() -> Iterable[Source]:
    """Yield one Source per session in the Goose SQLite database."""
    for location in DEFAULT_LOCATIONS:
        db_path = Path(location).expanduser()
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT id FROM sessions ORDER BY created_at")
            for row in cursor:
                yield Source(
                    kind="session",
                    location=db_path,
                    metadata={"session_id": row["id"]},
                )
            conn.close()
        except sqlite3.Error:
            continue


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "session":
        return False
    path = Path(source.location)
    return path.name == "sessions.db" and "goose" in str(path)


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Goose session from the SQLite database and yield a Conversation."""
    db_path = Path(source.location)
    session_id = source.metadata.get("session_id")
    if not session_id:
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return

    try:
        # Load session metadata
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session_row:
            return

        # Load messages ordered by timestamp
        messages = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_timestamp",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return
    finally:
        conn.close()

    # Extract session metadata
    working_dir = session_row["working_dir"]
    created_at = session_row["created_at"] or ""
    updated_at = session_row["updated_at"]
    provider_name = session_row["provider_name"]

    # Extract model from model_config_json
    default_model = None
    model_config_raw = session_row["model_config_json"]
    if model_config_raw:
        try:
            model_config = json.loads(model_config_raw)
            default_model = model_config.get("model") or model_config.get("model_name")
        except (json.JSONDecodeError, TypeError):
            pass

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
        started_at=created_at,
        ended_at=updated_at,
        workspace_path=working_dir,
        default_model=default_model,
    )

    # Process messages
    current_prompt: Prompt | None = None
    # Track toolRequest IDs from assistant messages for matching with toolResponse
    pending_tool_requests: dict[str, tuple[Response, str, dict]] = {}

    for msg in messages:
        role = msg["role"]
        timestamp = str(msg["created_timestamp"])
        content_blocks = _parse_content_json(msg["content_json"])
        msg_tokens = msg["tokens"]

        if role == "user":
            # Check if this message contains only toolResponse blocks
            has_tool_response = any(
                b.get("type") == "toolResponse" for b in content_blocks
            )
            has_text = any(b.get("type") == "text" for b in content_blocks)

            if has_tool_response:
                # Match tool responses to pending requests
                for block in content_blocks:
                    if block.get("type") == "toolResponse":
                        tool_id = block.get("id")
                        tool_result = block.get("toolResult", {})
                        if tool_id and tool_id in pending_tool_requests:
                            response, tool_name, input_dict = pending_tool_requests.pop(tool_id)
                            status = tool_result.get("status", "success")
                            result_value = tool_result.get("value")
                            tool_call = ToolCall(
                                tool_name=tool_name,
                                input=input_dict,
                                result={"value": result_value} if result_value else None,
                                status=status,
                                external_id=tool_id,
                                timestamp=timestamp,
                            )
                            response.tool_calls.append(tool_call)

                # If there's also text content, create a prompt for it
                if has_text:
                    current_prompt = Prompt(
                        timestamp=timestamp,
                        external_id=f"{NAME}::{session_id}::msg::{msg['id']}",
                    )
                    for block in content_blocks:
                        if block.get("type") == "text":
                            current_prompt.content.append(
                                ContentBlock(block_type="text", content={"text": block.get("text", "")})
                            )
                    conversation.prompts.append(current_prompt)
            else:
                # Regular user prompt
                current_prompt = Prompt(
                    timestamp=timestamp,
                    external_id=f"{NAME}::{session_id}::msg::{msg['id']}",
                )
                for block in content_blocks:
                    content_block = _block_to_content_block(block)
                    current_prompt.content.append(content_block)
                conversation.prompts.append(current_prompt)

        elif role == "assistant":
            # Goose provides a single `tokens` integer per message, not a full
            # input/output breakdown. Check metadata_json for a richer split.
            usage = _extract_usage(msg["metadata_json"], msg_tokens)

            response = Response(
                timestamp=timestamp,
                usage=usage,
                model=default_model,
                provider=provider_name,
                external_id=f"{NAME}::{session_id}::msg::{msg['id']}",
            )

            for block in content_blocks:
                if block.get("type") == "toolRequest":
                    # Track for matching with toolResponse
                    tool_id = block.get("id")
                    tool_call_data = block.get("toolCall", {})
                    tool_value = tool_call_data.get("value", {})
                    tool_name = tool_value.get("name", "unknown")
                    tool_args = tool_value.get("arguments", {})

                    if tool_id:
                        pending_tool_requests[tool_id] = (response, tool_name, tool_args)

                    # Add as content block
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
                else:
                    content_block = _block_to_content_block(block)
                    response.content.append(content_block)

            # Attach response to current prompt
            if current_prompt is not None:
                current_prompt.responses.append(response)

    # Handle any pending tool requests that never got responses
    for tool_id, (response, tool_name, input_dict) in pending_tool_requests.items():
        tool_call = ToolCall(
            tool_name=tool_name,
            input=input_dict,
            result=None,
            status="pending",
            external_id=tool_id,
            timestamp=None,
        )
        response.tool_calls.append(tool_call)

    yield conversation


def _extract_usage(metadata_json: str | None, msg_tokens: int | None) -> Usage | None:
    """Extract token usage from message metadata, falling back to the raw tokens field.

    Goose stores a single integer `tokens` per message. If metadata_json contains
    a finer-grained breakdown (input_tokens/output_tokens), prefer that.
    """
    if metadata_json:
        try:
            meta = json.loads(metadata_json)
            if isinstance(meta, dict):
                input_tokens = meta.get("input_tokens") or meta.get("tokens_in")
                output_tokens = meta.get("output_tokens") or meta.get("tokens_out")
                if input_tokens is not None or output_tokens is not None:
                    return Usage(
                        input_tokens=int(input_tokens) if input_tokens else None,
                        output_tokens=int(output_tokens) if output_tokens else None,
                    )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Fallback: single integer is output tokens for assistant messages
    if msg_tokens:
        return Usage(output_tokens=msg_tokens)

    return None


def _parse_content_json(content_json: str | None) -> list[dict]:
    """Parse content_json field into a list of content blocks."""
    if not content_json:
        return []
    try:
        parsed = json.loads(content_json)
        if isinstance(parsed, list):
            return parsed
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def _block_to_content_block(block: dict) -> ContentBlock:
    """Convert a raw Goose content block to a ContentBlock domain object."""
    block_type = block.get("type", "unknown")
    if block_type == "text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    return ContentBlock(block_type=block_type, content=block)
