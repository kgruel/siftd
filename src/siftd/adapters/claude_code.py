"""Claude Code adapter for siftd.

Pure parser: reads JSONL files and yields Conversation domain objects.
No storage coupling.
"""

import json
import re
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso, parse_block
from siftd.adapters.sdk import (
    NormalizedRecord,
    build_harness,
    discover_files,
    flush_pending_calls,
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
SUPPORT_TIER = "core"
NAME = "claude_code"
# ~/.config/claude/projects is where modern installs write live logs;
# ~/.claude/projects remains as the legacy/fallback location.
DEFAULT_LOCATIONS = ["~/.config/claude/projects", "~/.claude/projects"]
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
    "tool.search": ["query"],
    "task.message": ["to"],
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
    "Task": "task.spawn",  # historical logs; superseded by "Agent"
    "Agent": "task.spawn",
    "TaskOutput": "task.output",
    "KillShell": "task.kill",
    "AskUserQuestion": "ui.ask",
    "TodoWrite": "ui.todo",  # historical logs; superseded by TaskCreate/TaskUpdate
    "TaskCreate": "ui.todo",
    "TaskUpdate": "ui.todo",
    "NotebookEdit": "notebook.edit",
    "Skill": "skill.invoke",
    "BashOutput": "shell.output",
    "ToolSearch": "tool.search",
    "SendMessage": "task.message",
    "Workflow": "workflow.run",
    "EnterPlanMode": "ui.plan",
    "ExitPlanMode": "ui.plan",
}

# A backgrounded Bash call's tool_result is plain prose naming its own id,
# e.g. "Command running in background with ID: buxu4lquj. Output is being
# written to: ...". BashOutput's later polls carry the same id structurally
# in input["bash_id"] -- no parsing needed on that side.
_BASH_BG_ID_RE = re.compile(r"background with ID:\s*(\S+?)\.")

# promptSource values that mean a human authored the prompt. Anything else
# (e.g. "system") is harness-injected content riding a user-role record.
_USER_PROMPT_SOURCES = frozenset({"typed", "queued"})


def _tool_use_result_attributes(tool_use_result) -> dict[str, str]:
    """Distill a record-level structured `toolUseResult` into attributes.

    Claude Code duplicates tool results in two channels: the in-message
    tool_result block (prose, already stored as the ToolCall result) and a
    top-level structured `toolUseResult` object. The structured form carries
    data the prose lacks — real parent->child agent-spawn linkage
    (agentId/agent_type/model/name/spawnDepth) and the stdout/stderr split
    for Bash. Capture-only: linkage joins are left to downstream consumers.
    String-form toolUseResult (error prose) carries nothing structured.
    """
    attrs: dict[str, str] = {}
    if not isinstance(tool_use_result, dict):
        return attrs

    # Agent/teammate spawn — both shapes carry an agent id:
    #   async Task spawn: {agentId, resolvedModel, status: "async_launched", ...}
    #   teammate spawn:   {agent_id, agent_type, model, name, spawnDepth,
    #                      status: "teammate_spawned", ...} (snake_case)
    agent_id = tool_use_result.get("agentId") or tool_use_result.get("agent_id")
    if agent_id is not None:
        attrs["agent_id"] = str(agent_id)
        if agent_type := tool_use_result.get("agent_type"):
            attrs["agent_type"] = str(agent_type)
        if name := tool_use_result.get("name"):
            attrs["agent_name"] = str(name)
        if model := (tool_use_result.get("model") or tool_use_result.get("resolvedModel")):
            attrs["agent_model"] = str(model)
        if (depth := tool_use_result.get("spawnDepth")) is not None:
            attrs["spawn_depth"] = str(depth)
        if status := tool_use_result.get("status"):
            attrs["spawn_status"] = str(status)

    # Bash — structured stdout/stderr split (the prose result mixes them)
    if "stdout" in tool_use_result or "stderr" in tool_use_result:
        if stdout := tool_use_result.get("stdout"):
            attrs["stdout"] = str(stdout)
        if stderr := tool_use_result.get("stderr"):
            attrs["stderr"] = str(stderr)
        if tool_use_result.get("interrupted"):
            attrs["interrupted"] = "true"

    return attrs


def _is_injected_user_record(record: dict) -> bool:
    """True when a user-role record carries injected (non-human) content.

    Claude Code marks injected content two ways: `isMeta: true` (caveats,
    skill injections) and `promptSource` values other than typed/queued
    (e.g. "system"). Absent both fields, the record is a real user prompt.
    """
    if record.get("isMeta"):
        return True
    prompt_source = record.get("promptSource")
    return prompt_source is not None and prompt_source not in _USER_PROMPT_SOURCES


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
    branch = None
    ai_title = None

    for record in records:
        if record.get("type") == "ai-title":
            # Emitted per turn; the last occurrence is the current title.
            ai_title = record.get("aiTitle") or ai_title
        elif record.get("type") in ("user", "assistant"):
            session_id = session_id or record.get("sessionId")
            agent_id = agent_id or record.get("agentId")
            session_cwd = session_cwd or record.get("cwd")
            branch = branch or record.get("gitBranch")
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

    # Sub-agent type/description live in an `agent-<id>.meta.json` sidecar beside
    # the transcript — the child JSONL itself carries no type. The sidecar
    # authoritatively binds this agentId to {agentType, description}, so we
    # capture it as conversation attributes here rather than reconstructing it
    # downstream from the spawning parent's tool_call (which has no agentId key
    # and only a lossy prompt-equality join back to the child). Absent for
    # top-level sessions and for historical sub-agents whose sidecar rotated off
    # disk before ingest — both degrade silently to no attribute.
    conv_attributes: dict[str, str] = {}
    if ai_title:
        conv_attributes["title"] = str(ai_title)
    if agent_id:
        meta_path = path.parent / f"{path.stem}.meta.json"
        try:
            import json as _json

            meta = _json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = None
        if isinstance(meta, dict):
            if atype := meta.get("agentType"):
                conv_attributes["subagent_type"] = str(atype)
            if desc := meta.get("description"):
                conv_attributes["agent_description"] = str(desc)

    # Records carry the branch that was live when each message was written
    # (`gitBranch`, captured above). Recomputing from the cwd's *current* git
    # state at ingest time mislabels any session ingested after a branch
    # switch, so the live lookup is only a fallback for old logs that predate
    # the per-record field.
    if branch is None and session_cwd:
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
        attributes=conv_attributes,
    )

    # Process messages
    # pending_tool_uses tracks tool_use blocks waiting for tool_result
    # key: tool_use_id, value: (response object, tool_name, input_dict)
    pending_tool_uses: dict[str, tuple[Response, str, dict]] = {}
    current_prompt: Prompt | None = None
    last_response: Response | None = None

    for record in records:
        record_type = record.get("type")
        if record_type == "system" and record.get("subtype") == "turn_duration":
            # Emitted after a turn's last assistant message; attach the
            # wall-clock duration to that response (attributes, not a domain
            # slot — the domain has no latency home).
            duration_ms = record.get("durationMs")
            if duration_ms is not None and last_response is not None:
                last_response.attributes["turn_duration_ms"] = str(duration_ms)
            continue
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
                # The top-level structured toolUseResult belongs to the
                # record's single tool_result block; skip it in the rare
                # multi-block case where the pairing would be ambiguous.
                tool_result_blocks = [
                    b for b in content_blocks
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                structured_attrs: dict[str, str] = {}
                if len(tool_result_blocks) == 1:
                    structured_attrs = _tool_use_result_attributes(record.get("toolUseResult"))

                # Process tool results - attach to pending tool uses
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if tool_use_id and tool_use_id in pending_tool_uses:
                            response, tool_name, input_dict = pending_tool_uses.pop(tool_use_id)
                            is_error = block.get("is_error", False)
                            result_content = block.get("content")
                            status = "error" if is_error else "success"

                            attributes: dict[str, str] = dict(structured_attrs)
                            if (
                                tool_name == "Bash"
                                and input_dict.get("run_in_background")
                                and isinstance(result_content, str)
                            ):
                                bg_match = _BASH_BG_ID_RE.search(result_content)
                                if bg_match:
                                    attributes["background_task_id"] = bg_match.group(1)
                            elif tool_name == "BashOutput":
                                bash_id = input_dict.get("bash_id")
                                if bash_id:
                                    attributes["background_task_id"] = bash_id

                            # Create completed tool call
                            tool_call = ToolCall(
                                tool_name=tool_name,
                                input=input_dict,
                                result={"content": result_content},
                                status=status,
                                external_id=tool_use_id,
                                timestamp=timestamp,
                                attributes=attributes,
                            )
                            response.tool_calls.append(tool_call)
            else:
                # Regular prompt
                current_prompt = Prompt(
                    timestamp=timestamp,
                    external_id=f"{NAME}::{external_msg_id}" if external_msg_id else None,
                )

                # Injected content (isMeta / non-user promptSource) keeps its
                # place in the conversation but is reclassified so it never
                # pollutes user-prompt FTS search: the text moves off the
                # "text" key (which is what gets indexed) onto "meta_text",
                # and the block type records the injection source.
                injected = _is_injected_user_record(record)

                # Parse content blocks
                for block in content_blocks:
                    content_block = parse_block(block)
                    if injected and "text" in content_block.content:
                        content = dict(content_block.content)
                        content["meta_text"] = content.pop("text")
                        content["meta_source"] = record.get("promptSource") or "isMeta"
                        content_block = ContentBlock(block_type="meta_text", content=content)
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

            # 5m vs 1h cache writes are priced at different multipliers, so
            # the TTL split is captured under distinct keys (capture-only —
            # the cost model still prices the combined cache_creation total).
            cache_creation = usage_data.get("cache_creation") or {}
            if cache_creation.get("ephemeral_5m_input_tokens"):
                attributes["cache_creation_ephemeral_5m_input_tokens"] = str(
                    cache_creation["ephemeral_5m_input_tokens"]
                )
            if cache_creation.get("ephemeral_1h_input_tokens"):
                attributes["cache_creation_ephemeral_1h_input_tokens"] = str(
                    cache_creation["ephemeral_1h_input_tokens"]
                )
            server_tool_use = usage_data.get("server_tool_use") or {}
            if server_tool_use.get("web_search_requests"):
                attributes["server_tool_use_web_search_requests"] = str(
                    server_tool_use["web_search_requests"]
                )
            if server_tool_use.get("web_fetch_requests"):
                attributes["server_tool_use_web_fetch_requests"] = str(
                    server_tool_use["web_fetch_requests"]
                )

            response = Response(
                timestamp=timestamp,
                usage=usage,
                model=message_data.get("model"),
                external_id=f"{NAME}::{external_msg_id}" if external_msg_id else None,
                attributes=attributes,
            )
            last_response = response

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

    yield from yield_conversation(conversation)


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

        # Injected content (isMeta / non-user promptSource) is not a real
        # user prompt — surface it as metadata so peek exchange counts and
        # prompt previews only reflect what the human actually typed.
        if _is_injected_user_record(raw):
            return NormalizedRecord(
                kind="metadata",
                timestamp=ts,
                session_id=raw.get("sessionId"),
                workspace_path=raw.get("cwd"),
            )

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
_peek_scan_base, peek_exchanges, peek_tail = make_peek_hooks(
    normalize_record,
    tool_aliases=TOOL_ALIASES,
    subagent_path_marker=SUBAGENT_PATH_MARKER,
)

# Subagent .meta.json keys → PeekScanResult.attributes keys. Names match
# the ingest-side conv_attributes vocabulary (agent_name/agent_type/...).
_SUBAGENT_META_KEYS: dict[str, str] = {
    "name": "agent_name",
    "agentType": "agent_type",
    "spawnDepth": "spawn_depth",
    "model": "agent_model",
}


def peek_scan(path: Path):
    """peek_scan with subagent .meta.json enrichment.

    A subagent log at .../subagents/agent-<name>-<hash>.jsonl has a sibling
    agent-<name>-<hash>.meta.json carrying identity the JSONL itself lacks
    (name/agentType/spawnDepth/model). Surface it via the scan attributes
    channel; fill result.model from the meta when the log had none.
    """
    result = _peek_scan_base(path)
    if result is None or SUBAGENT_PATH_MARKER not in str(path):
        return result
    meta_path = path.with_name(path.stem + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    if not isinstance(meta, dict):
        return result
    for src, dst in _SUBAGENT_META_KEYS.items():
        value = meta.get(src)
        if value is not None:
            result.attributes[dst] = str(value)
    if result.model is None and meta.get("model"):
        result.model = str(meta["model"])
    return result
