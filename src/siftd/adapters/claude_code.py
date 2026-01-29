"""Claude Code adapter for siftd.

Pure parser: reads JSONL files and yields Conversation domain objects.
No storage coupling.
"""

from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso, parse_block
from siftd.domain import (
    Conversation,
    Harness,
    Prompt,
    Response,
    Source,
    ToolCall,
    Usage,
)

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
NAME = "claude_code"
DEFAULT_LOCATIONS = ["~/.claude/projects", "~/.config/claude/projects"]
SOURCE_KINDS = ["file"]
DEDUP_STRATEGY = "file"  # one conversation per file

# Harness metadata
HARNESS_SOURCE = "anthropic"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Claude Code"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "Read": "file.read",
    "Write": "file.write",
    "Edit": "file.edit",
    "Glob": "file.glob",
    "Bash": "shell.execute",
    "Grep": "search.grep",
    "WebSearch": "search.web",
    "WebFetch": "web.fetch",
    "Task": "task.spawn",
    "TaskOutput": "task.output",
    "KillShell": "task.kill",
    "AskUserQuestion": "ui.ask",
    "TodoWrite": "ui.todo",
    "NotebookEdit": "notebook.edit",
    "Skill": "skill.invoke",
}


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Claude Code session files."""
    for location in (locations or DEFAULT_LOCATIONS):
        base = Path(location).expanduser()
        if not base.exists():
            continue
        # Claude Code stores files as: ~/.claude/projects/{project}/*.jsonl
        for jsonl_file in base.glob("**/*.jsonl"):
            yield Source(kind="file", location=jsonl_file)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    return path.suffix == ".jsonl"


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Claude Code JSONL file and yield Conversation objects.

    Typically yields a single conversation per file, but the interface
    supports multiple for generality.
    """
    path = Path(source.location)
    records = load_jsonl(path)
    if not records:
        return

    # Extract session metadata
    session_id = None
    agent_id = None
    session_cwd = None
    started_at = None
    ended_at = None

    for record in records:
        if record.get("type") in ("user", "assistant"):
            session_id = session_id or record.get("sessionId")
            agent_id = agent_id or record.get("agentId")
            session_cwd = session_cwd or record.get("cwd")
            ts = record.get("timestamp")
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

    # Build external_id (include agentId for subagent files)
    if agent_id:
        external_id = f"{NAME}::{session_id or path.stem}::agent::{agent_id}"
    else:
        external_id = f"{NAME}::{session_id or path.stem}"

    # Create conversation (will be populated with prompts)
    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=session_cwd,
    )

    # Process messages
    # pending_tool_uses tracks tool_use blocks waiting for tool_result
    # key: tool_use_id, value: (response object, tool_name, input_dict)
    pending_tool_uses: dict[str, tuple[Response, str, dict]] = {}
    current_prompt: Prompt | None = None

    for record in records:
        record_type = record.get("type")
        if record_type not in ("user", "assistant"):
            continue

        message_data = record.get("message") or {}
        role = message_data.get("role") or record_type
        timestamp = record.get("timestamp", now_iso())
        external_msg_id = record.get("uuid")
        content_blocks = _normalize_content(message_data.get("content"))

        if role == "user":
            # Check if this is a tool_result message
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content_blocks
            )

            if has_tool_result:
                # Process tool results - attach to pending tool uses
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if tool_use_id and tool_use_id in pending_tool_uses:
                            response, tool_name, input_dict = pending_tool_uses.pop(tool_use_id)
                            is_error = block.get("is_error", False)
                            result_content = block.get("content")
                            status = "error" if is_error else "success"

                            # Create completed tool call
                            tool_call = ToolCall(
                                tool_name=tool_name,
                                input=input_dict,
                                result={"content": result_content},
                                status=status,
                                external_id=tool_use_id,
                                timestamp=timestamp,
                            )
                            response.tool_calls.append(tool_call)
            else:
                # Regular prompt
                current_prompt = Prompt(
                    timestamp=timestamp,
                    external_id=f"{NAME}::{external_msg_id}" if external_msg_id else None,
                )

                # Parse content blocks
                for block in content_blocks:
                    content_block = parse_block(block)
                    current_prompt.content.append(content_block)

                conversation.prompts.append(current_prompt)

        elif role == "assistant":
            # Response
            usage_data = message_data.get("usage") or {}
            usage = None
            if usage_data:
                usage = Usage(
                    input_tokens=usage_data.get("input_tokens"),
                    output_tokens=usage_data.get("output_tokens"),
                )

            # Extract cache token attributes
            attributes: dict[str, str] = {}
            if usage_data.get("cache_creation_input_tokens"):
                attributes["cache_creation_input_tokens"] = str(usage_data["cache_creation_input_tokens"])
            if usage_data.get("cache_read_input_tokens"):
                attributes["cache_read_input_tokens"] = str(usage_data["cache_read_input_tokens"])

            response = Response(
                timestamp=timestamp,
                usage=usage,
                model=message_data.get("model"),
                external_id=f"{NAME}::{external_msg_id}" if external_msg_id else None,
                attributes=attributes,
            )

            # Parse content blocks and track tool uses
            for block in content_blocks:
                content_block = parse_block(block)
                response.content.append(content_block)

                # Track tool_use for later matching with tool_result
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    tool_name = block.get("name", "unknown")
                    input_dict = block.get("input", {})
                    if tool_id:
                        pending_tool_uses[tool_id] = (response, tool_name, input_dict)

            # Attach response to current prompt
            if current_prompt is not None:
                current_prompt.responses.append(response)

    # Handle any pending tool calls that never got results
    for tool_use_id, (response, tool_name, input_dict) in pending_tool_uses.items():
        tool_call = ToolCall(
            tool_name=tool_name,
            input=input_dict,
            result=None,
            status="pending",
            external_id=tool_use_id,
            timestamp=None,
        )
        response.tool_calls.append(tool_call)

    # Skip sessions with no messages (opened and immediately canceled)
    if not conversation.prompts:
        return

    yield conversation


def _normalize_content(content) -> list:
    """Normalize content to a list of blocks.

    Content can be:
    - None -> []
    - A string -> [{"type": "text", "text": string}]
    - A list of blocks -> as-is
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []
