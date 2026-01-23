"""Cline (VS Code extension) adapter for tbd-v2.

Pure parser: reads JSON task directories and yields Conversation domain objects.
No storage coupling.
"""

import json
from datetime import datetime
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
NAME = "cline"
DEFAULT_LOCATIONS = [
    "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks",
    "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks",
]
SOURCE_KINDS = ["file"]
DEDUP_STRATEGY = "file"  # one conversation per task directory

# Harness metadata
HARNESS_SOURCE = "anthropic"
HARNESS_LOG_FORMAT = "json"
HARNESS_DISPLAY_NAME = "Cline"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "read_file": "file.read",
    "write_to_file": "file.write",
    "replace_in_file": "file.edit",
    "list_files": "file.list",
    "search_files": "file.search",
    "execute_command": "shell.execute",
    "browser_action": "browser.action",
    "ask_followup_question": "ui.ask",
    "attempt_completion": "ui.complete",
    "use_mcp_tool": "mcp.tool",
    "access_mcp_resource": "mcp.resource",
    "list_code_definition_names": "code.definitions",
}


def discover() -> Iterable[Source]:
    """Yield Source objects for each Cline task directory containing api_conversation_history.json."""
    for location in DEFAULT_LOCATIONS:
        base = Path(location).expanduser()
        if not base.exists():
            continue
        for task_dir in sorted(base.iterdir()):
            if not task_dir.is_dir():
                continue
            api_file = task_dir / "api_conversation_history.json"
            if api_file.exists():
                yield Source(kind="file", location=api_file)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    return (
        path.name == "api_conversation_history.json"
        and "saoudrizwan.claude-dev" in str(path)
    )


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Cline task's api_conversation_history.json and yield a Conversation."""
    path = Path(source.location)
    messages = _load_json_array(path)
    if not messages:
        return

    task_dir = path.parent
    task_id = task_dir.name

    # Load optional metadata for model info and timestamps
    metadata = _load_task_metadata(task_dir)
    model = metadata.get("model")
    cwd = metadata.get("cwd")

    # Derive timestamps from metadata or fallback to now
    started_at = metadata.get("started_at") or _now()
    ended_at = metadata.get("ended_at")

    harness = Harness(
        name=NAME,
        source=HARNESS_SOURCE,
        log_format=HARNESS_LOG_FORMAT,
        display_name=HARNESS_DISPLAY_NAME,
    )

    external_id = f"{NAME}::{task_id}"

    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at,
        ended_at=ended_at,
        workspace_path=cwd,
        default_model=model,
    )

    # Process messages: alternating user/assistant in raw Anthropic format
    pending_tool_uses: dict[str, tuple[Response, str, dict]] = {}
    current_prompt: Prompt | None = None

    for msg in messages:
        role = msg.get("role")
        content_blocks = _normalize_content(msg.get("content"))

        if role == "user":
            # Check if this is a tool_result message
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content_blocks
            )

            if has_tool_result:
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if tool_use_id and tool_use_id in pending_tool_uses:
                            response, tool_name, input_dict = pending_tool_uses.pop(tool_use_id)
                            is_error = block.get("is_error", False)
                            result_content = block.get("content")
                            status = "error" if is_error else "success"

                            tool_call = ToolCall(
                                tool_name=tool_name,
                                input=input_dict,
                                result={"content": result_content},
                                status=status,
                                external_id=tool_use_id,
                            )
                            response.tool_calls.append(tool_call)
            else:
                current_prompt = Prompt(timestamp=started_at)
                for block in content_blocks:
                    current_prompt.content.append(_parse_block(block))
                conversation.prompts.append(current_prompt)

        elif role == "assistant":
            # Extract usage from the message if present
            usage_data = msg.get("usage") or {}
            usage = None
            if usage_data:
                usage = Usage(
                    input_tokens=usage_data.get("input_tokens"),
                    output_tokens=usage_data.get("output_tokens"),
                )

            attributes: dict[str, str] = {}
            if usage_data.get("cache_creation_input_tokens"):
                attributes["cache_creation_input_tokens"] = str(
                    usage_data["cache_creation_input_tokens"]
                )
            if usage_data.get("cache_read_input_tokens"):
                attributes["cache_read_input_tokens"] = str(
                    usage_data["cache_read_input_tokens"]
                )

            response = Response(
                timestamp=started_at,
                usage=usage,
                model=model,
                attributes=attributes,
            )

            for block in content_blocks:
                response.content.append(_parse_block(block))

                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    tool_name = block.get("name", "unknown")
                    input_dict = block.get("input", {})
                    if tool_id:
                        pending_tool_uses[tool_id] = (response, tool_name, input_dict)

            if current_prompt is not None:
                current_prompt.responses.append(response)

    # Handle pending tool calls that never got results
    for tool_use_id, (response, tool_name, input_dict) in pending_tool_uses.items():
        tool_call = ToolCall(
            tool_name=tool_name,
            input=input_dict,
            result=None,
            status="pending",
            external_id=tool_use_id,
        )
        response.tool_calls.append(tool_call)

    yield conversation


def _load_json_array(path: Path) -> list[dict]:
    """Load a JSON array file."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _load_task_metadata(task_dir: Path) -> dict:
    """Load metadata from taskHistory.json and ui_messages.json.

    Returns dict with: model, cwd, started_at, ended_at.
    """
    result: dict = {}

    # Try taskHistory.json: .../saoudrizwan.claude-dev/state/taskHistory.json
    state_dir = task_dir.parent.parent / "state"
    history_file = state_dir / "taskHistory.json"
    task_id = task_dir.name

    if history_file.exists():
        try:
            with history_file.open("r", encoding="utf-8") as f:
                history = json.load(f)
            if isinstance(history, list):
                for item in history:
                    if item.get("id") == task_id:
                        result["model"] = item.get("modelId")
                        result["cwd"] = item.get("cwdOnTaskInitialization")
                        ts = item.get("ts")
                        if ts:
                            # ts is Unix milliseconds
                            result["started_at"] = datetime.fromtimestamp(
                                ts / 1000
                            ).isoformat()
                        break
        except (json.JSONDecodeError, OSError):
            pass

    # Try ui_messages.json for end timestamp (last message ts)
    ui_file = task_dir / "ui_messages.json"
    if ui_file.exists():
        try:
            with ui_file.open("r", encoding="utf-8") as f:
                ui_messages = json.load(f)
            if isinstance(ui_messages, list) and ui_messages:
                last_ts = ui_messages[-1].get("ts")
                if last_ts:
                    result["ended_at"] = datetime.fromtimestamp(
                        last_ts / 1000
                    ).isoformat()
        except (json.JSONDecodeError, OSError):
            pass

    return result


def _normalize_content(content) -> list:
    """Normalize content to a list of blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _parse_block(block) -> ContentBlock:
    """Parse content block into a ContentBlock domain object."""
    if isinstance(block, str):
        return ContentBlock(block_type="text", content={"text": block})
    block_type = block.get("type", "unknown")
    return ContentBlock(block_type=block_type, content=block)


def _now() -> str:
    """ISO timestamp for now."""
    return datetime.now().isoformat()
