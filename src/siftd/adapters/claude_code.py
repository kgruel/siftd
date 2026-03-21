"""Claude Code adapter for siftd.

Pure parser: reads JSONL files and yields Conversation domain objects.
No storage coupling.
"""

from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso, parse_block
from siftd.adapters.sdk import (
    NormalizedRecord,
    build_harness,
    discover_files,
    flush_pending_calls,
    make_peek_hooks,
)
from siftd.domain import (
    Conversation,
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
DEDUP_STRATEGY = "file"  # one conversation per file
SUPPORTS_LIVE_REGISTRATION = True  # supports tagging during active sessions
SUBAGENT_PATH_MARKER = "/subagents/"  # path marker for subagent detection

# Harness metadata
HARNESS_SOURCE = "anthropic"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Claude Code"

# Canonical tool name → input keys to try for hint extraction (priority order)
TOOL_HINT_KEYS: dict[str, list[str]] = {
    "shell.execute": ["description", "command"],
    "file.read": ["file_path", "path"],
    "file.write": ["file_path", "path"],
    "file.edit": ["file_path", "path"],
    "file.glob": ["pattern"],
    "search.grep": ["pattern"],
    "search.web": ["query"],
    "web.fetch": ["url"],
    "task.spawn": ["description"],
    "notebook.edit": ["notebook_path"],
    "skill.invoke": ["skill"],
}

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
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/*.jsonl"])


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.suffix != ".jsonl":
        return False
    path_str = str(path)
    # Reject files that are clearly under other adapters' locations
    # This prevents Claude Code from claiming Codex CLI files
    other_adapter_markers = [
        ".codex/sessions", ".codex\\sessions",
        ".pi/agent/sessions", ".pi\\agent\\sessions",
        ".copilot/session-state", ".copilot\\session-state",
    ]
    for marker in other_adapter_markers:
        if marker in path_str:
            return False
    # Accept .jsonl files that aren't under other adapters
    return True


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
    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    # Build external_id (include agentId for subagent files)
    if agent_id:
        external_id = f"{NAME}::{session_id or path.stem}::agent::{agent_id}"
    else:
        external_id = f"{NAME}::{session_id or path.stem}"

    branch = None
    if session_cwd:
        from siftd.git import get_worktree_branch

        branch = get_worktree_branch(session_cwd)

    # Create conversation (will be populated with prompts)
    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=session_cwd,
        branch=branch,
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
    flush_pending_calls(pending_tool_uses)

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


# =============================================================================
# Record normalization — general pattern for SDK integration
# =============================================================================


def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map a Claude Code native record to NormalizedRecord.

    Claude Code record types:
        "user"      → user (or tool_result if content has tool_result blocks)
        "assistant" → assistant (with content blocks, usage, model)
    """
    record_type = raw.get("type")
    ts = raw.get("timestamp")

    if record_type not in ("user", "assistant"):
        return None

    msg = raw.get("message") or {}
    content = msg.get("content")
    # Normalize content to list
    if content is None:
        content_blocks = []
    elif isinstance(content, str):
        content_blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        content_blocks = content
    else:
        content_blocks = []

    if record_type == "user":
        # Check if this is a tool_result message
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content_blocks
        )
        if has_tool_result:
            return NormalizedRecord(kind="tool_result", timestamp=ts)

        extra: dict = {}
        agent_id = raw.get("agentId")
        if agent_id is not None:
            extra["agent_id"] = agent_id

        return NormalizedRecord(
            kind="user",
            timestamp=ts,
            content_blocks=content_blocks,
            session_id=raw.get("sessionId"),
            workspace_path=raw.get("cwd"),
            extra=extra,
        )

    # assistant
    usage = msg.get("usage") or {}
    return NormalizedRecord(
        kind="assistant",
        timestamp=ts,
        content_blocks=content_blocks,
        model=msg.get("model"),
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
    )


# Peek hooks — derived from normalizer
peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    normalize_record,
    tool_aliases=TOOL_ALIASES,
    subagent_path_marker=SUBAGENT_PATH_MARKER,
)
