"""OpenCode adapter for siftd.

Pure parser: reads OpenCode's SQLite database and yields Conversation domain objects.
No storage coupling.
"""

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import now_iso
from siftd.adapters.sdk import AdapterParseError, build_harness, open_external_db
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
from siftd.safecall import epoch_ms_to_iso

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
SUPPORT_TIER = "contrib"
NAME = "opencode"
DEFAULT_LOCATIONS = ["~/.local/share/opencode"]
DEDUP_STRATEGY = "session"  # multiple conversations from one DB

# Harness metadata
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "sqlite"
HARNESS_DISPLAY_NAME = "OpenCode"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {}


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for OpenCode databases."""
    for location in locations or DEFAULT_LOCATIONS:
        base = Path(location).expanduser()
        if not base.exists():
            continue
        db_path = base / "opencode.db"
        if db_path.is_file():
            yield Source(kind="sqlite", location=db_path)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "sqlite":
        return False
    path = Path(source.location)
    return path.name == "opencode.db"


def parse(source: Source) -> Iterable[Conversation]:
    """Parse an OpenCode SQLite database and yield Conversation objects."""
    path = Path(source.location)
    if not path.exists():
        raise AdapterParseError(f"OpenCode source {path} does not exist")
    if not path.is_file():
        raise AdapterParseError(f"OpenCode source {path} is not a file")

    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    try:
        conn = open_external_db(path)
    except sqlite3.Error as e:
        raise AdapterParseError(
            f"OpenCode source {path} could not be opened as SQLite: {e}"
        ) from e

    try:
        yield from _parse_sessions(conn, harness, path=path, strict=True)
    finally:
        conn.close()


def _parse_sessions(
    conn: sqlite3.Connection,
    harness: Harness,
    *,
    path: Path | None = None,
    strict: bool = False,
) -> Iterable[Conversation]:
    """Query sessions and yield Conversation objects."""
    # Lenient path preserved for peek; ingest uses strict=True.
    try:
        sessions = conn.execute(
            "SELECT id, directory, title, time_created, time_updated FROM session ORDER BY time_created"
        ).fetchall()
    except sqlite3.OperationalError as e:
        if strict:
            target = path or "<sqlite>"
            raise AdapterParseError(
                f"OpenCode source {target} is missing the session table: {e}"
            ) from e
        return

    for session in sessions:
        session_id = session["id"]
        directory = session["directory"]
        time_created = session["time_created"]
        time_updated = session["time_updated"]

        started_at = epoch_ms_to_iso(time_created)
        ended_at = epoch_ms_to_iso(time_updated)

        external_id = f"{NAME}::{session_id}"

        conversation = Conversation(
            external_id=external_id,
            harness=harness,
            started_at=started_at or now_iso(),
            ended_at=ended_at,
            workspace_path=directory,
        )

        # Query messages for this session, ordered by time_created
        try:
            messages = conn.execute(
                "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            if strict:
                target = path or "<sqlite>"
                raise AdapterParseError(
                    f"OpenCode source {target} is missing the message table: {e}"
                ) from e
            continue

        current_prompt: Prompt | None = None

        for message in messages:
            msg_data = _parse_json(message["data"])
            if not msg_data:
                continue

            role = msg_data.get("role")
            msg_time = epoch_ms_to_iso(message["time_created"])

            if role == "user":
                current_prompt = Prompt(timestamp=msg_time or now_iso())

                # Get parts for this message
                parts = _get_parts(conn, message["id"], session_id)
                for part in parts:
                    part_data = _parse_json(part["data"])
                    if not part_data:
                        continue
                    block = _part_to_content_block(part_data)
                    if block:
                        current_prompt.content.append(block)

                # Fallback: extract summary as content if no parts
                if not current_prompt.content:
                    summary = msg_data.get("summary", {})
                    title = summary.get("title", "") if isinstance(summary, dict) else ""
                    if title:
                        current_prompt.content.append(
                            ContentBlock(block_type="text", content={"text": title})
                        )

                conversation.prompts.append(current_prompt)

            elif role == "assistant":
                response = Response(timestamp=msg_time or now_iso())

                # Extract model
                model_id = msg_data.get("modelID")
                if model_id:
                    response.model = model_id

                # Extract usage
                tokens = msg_data.get("tokens")
                if tokens:
                    response.usage = Usage(
                        input_tokens=tokens.get("input"),
                        output_tokens=tokens.get("output"),
                    )
                    reasoning = tokens.get("reasoning")
                    if reasoning is not None:
                        response.attributes["reasoning_output_tokens"] = str(reasoning)
                    cache = tokens.get("cache", {})
                    if isinstance(cache, dict):
                        cache_read = cache.get("read")
                        if cache_read is not None:
                            response.attributes["cache_read_input_tokens"] = str(cache_read)
                        cache_write = cache.get("write")
                        if cache_write is not None:
                            response.attributes["cache_creation_input_tokens"] = str(cache_write)

                # Extract cost
                cost = msg_data.get("cost")
                if cost is not None:
                    response.attributes["cost"] = str(cost)

                # Get parts for this message
                parts = _get_parts(conn, message["id"], session_id)
                for part in parts:
                    part_data = _parse_json(part["data"])
                    if not part_data:
                        continue
                    part_type = part_data.get("type")

                    if part_type == "tool":
                        tool_call = _part_to_tool_call(part_data)
                        if tool_call:
                            response.tool_calls.append(tool_call)
                        # Also add as content block
                        response.content.append(ContentBlock(
                            block_type="tool_use",
                            content={
                                "id": part_data.get("callID"),
                                "name": part_data.get("tool", "unknown"),
                            },
                        ))
                    else:
                        block = _part_to_content_block(part_data)
                        if block:
                            response.content.append(block)

                if current_prompt is not None:
                    current_prompt.responses.append(response)

        # Only yield conversations that have prompts
        if conversation.prompts:
            yield conversation


def _get_parts(conn: sqlite3.Connection, message_id: str, session_id: str) -> list:
    """Query parts for a message, ordered by time_created."""
    try:
        return conn.execute(
            "SELECT id, data, time_created FROM part WHERE message_id = ? AND session_id = ? ORDER BY time_created",
            (message_id, session_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _part_to_content_block(part_data: dict) -> ContentBlock | None:
    """Convert a part data dict to a ContentBlock."""
    part_type = part_data.get("type")

    if part_type == "text":
        text = part_data.get("text", "")
        if text:
            return ContentBlock(block_type="text", content={"text": text})
    elif part_type == "reasoning":
        text = part_data.get("text", "")
        if text:
            return ContentBlock(block_type="thinking", content={"text": text})
    elif part_type in ("step-start", "step-finish"):
        return None  # skip step markers
    elif part_type == "tool":
        return None  # handled separately in parse
    return None


def _part_to_tool_call(part_data: dict) -> ToolCall | None:
    """Convert a tool part data dict to a ToolCall."""
    state = part_data.get("state", {})
    if not isinstance(state, dict):
        return None

    tool_name = part_data.get("tool", "unknown")
    call_id = part_data.get("callID")
    status_raw = state.get("status", "")

    input_data = state.get("input", {})
    if not isinstance(input_data, dict):
        input_data = {"raw": str(input_data)}

    output = state.get("output")
    result = {"output": output} if output else None

    # Map status
    if status_raw == "completed":
        status = "success"
    elif status_raw == "error":
        status = "error"
    else:
        status = "pending"

    # Extract timestamp from state.time
    time_data = state.get("time", {})
    timestamp = None
    if isinstance(time_data, dict):
        end_time = time_data.get("end")
        if end_time:
            timestamp = epoch_ms_to_iso(end_time)

    return ToolCall(
        tool_name=tool_name,
        input=input_data,
        result=result,
        status=status,
        external_id=call_id,
        timestamp=timestamp,
    )


def _parse_json(data: str | None) -> dict | None:
    """Parse a JSON string, returning None on failure."""
    if not data:
        return None
    try:
        result = json.loads(data)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
