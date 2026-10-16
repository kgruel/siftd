"""Claude Code adapter for siftd.

Pure parser: reads JSONL files and yields Conversation domain objects.
No storage coupling. The adapter only decodes: :class:`ClaudeDecoder` turns
each JSONL line into canonical records, and the shared assembler
(:mod:`siftd.adapters.assembly`) decides where they attach.
"""

import json
import re
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import parse_block
from siftd.adapters.assembly import read_jsonl
from siftd.adapters.records import (
    Ignored,
    LineageRec,
    Meta,
    PatchRec,
    PromptRec,
    Record,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
)
from siftd.adapters.sdk import discover_files
from siftd.domain import ContentBlock, Source, Usage
from siftd.domain.peek import PeekScanResult

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 2
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

# The harness's notice for a backgrounded Bash call is a tool_result that
# *begins* with prose naming its own id, e.g. "Command running in background
# with ID: buxu4lquj. Output is being written to: ...". It is anchored at the
# start because other output may quote the phrase (a Read of this file does);
# the claim is about the result's text alone, so it holds whichever of the
# call and result the file wrote first. A call the harness backgrounded on
# its own (a timeout, no `run_in_background` input) gets the same notice.
# BashOutput's later polls carry the id structurally in input["bash_id"].
_BASH_BG_ID_RE = re.compile(r"Command running in background with ID:\s*(\S+?)\.")

# promptSource values that mean a human authored the prompt. Anything else
# (e.g. "system") is harness-injected content riding a user-role record.
_USER_PROMPT_SOURCES = frozenset({"typed", "queued"})


def _read_subagent_meta(path: Path) -> dict | None:
    """Load the sidecar agent-<name>-<hash>.meta.json beside a subagent JSONL.

    The sidecar carries identity the child transcript itself lacks
    (name/agentType/description/spawnDepth/model). Returns None when it's
    absent (top-level sessions, rotated-off history) or unparseable — the
    single decode point for both the ingest parse and peek paths.
    """
    meta_path = path.with_name(path.stem + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


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


def _is_human_prompt(attachment: dict) -> bool:
    """True for a `queued_command` attachment carrying a message the user typed mid-turn."""
    origin = attachment.get("origin")
    return attachment.get("commandMode") == "prompt" and isinstance(origin, dict) and origin.get("kind") == "human"


def effective_role(record: dict) -> str | None:
    """How every reader of a record classifies it: "user", "assistant", or None.

    The record type gates which records are read (only "user" and "assistant" records
    are messages), and message.role, defaulting to the type, decides prompt versus
    response. The decoder and follow mode both call this one function, so they cannot
    disagree about what a record is. Returns the role verbatim otherwise, so a role
    that is neither user nor assistant is skipped by every caller.
    """
    record_type = record.get("type")
    if record_type not in ("user", "assistant"):
        return None
    message = record.get("message")
    role = message.get("role") if isinstance(message, dict) else None
    return role or record_type


def message_id_of(record: dict) -> str | None:
    """The API message.id a record belongs to, or None when absent."""
    message = record.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    return message_id if isinstance(message_id, str) and message_id else None


def _usage_snapshot(usage_data) -> tuple[Usage | None, dict[str, int]]:
    """Domain Usage plus the usage-detail counters for one message.usage.

    A snapshot of the message's total so far: every streamed record of a
    message repeats it, and the assembler keeps the last one. Counters at zero
    or absent are left out, as they always were.
    """
    if not isinstance(usage_data, dict) or not usage_data:
        return None, {}
    usage = Usage(
        input_tokens=usage_data.get("input_tokens"),
        output_tokens=usage_data.get("output_tokens"),
    )

    details: dict[str, int] = {}

    def keep(name: str, value) -> None:
        if value:
            details[name] = int(value)

    keep("cache_creation_input_tokens", usage_data.get("cache_creation_input_tokens"))
    keep("cache_read_input_tokens", usage_data.get("cache_read_input_tokens"))
    # 5m vs 1h cache writes are priced at different multipliers, so the TTL
    # split is captured under distinct keys (capture-only: the cost model
    # still prices the combined cache_creation total).
    cache_creation = usage_data.get("cache_creation") or {}
    keep("cache_creation_ephemeral_5m_input_tokens", cache_creation.get("ephemeral_5m_input_tokens"))
    keep("cache_creation_ephemeral_1h_input_tokens", cache_creation.get("ephemeral_1h_input_tokens"))
    server_tool_use = usage_data.get("server_tool_use") or {}
    keep("server_tool_use_web_search_requests", server_tool_use.get("web_search_requests"))
    keep("server_tool_use_web_fetch_requests", server_tool_use.get("web_fetch_requests"))
    return usage, details


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


# Record types that are not conversation turns. Each is Ignored by name; the
# docstring of ClaudeDecoder says why, and tests/adapters/test_claude_code.py
# keeps that table and these sets equal.
_IGNORED_TYPES = frozenset({
    "attachment",
    "last-prompt",
    "mode",
    "permission-mode",
    "queue-operation",
    "file-history-snapshot",
    "file-history-delta",
    "atis-latch",
    "bridge-session",
    "api-request",
    "api-request-blob",
    "api-request-shape",
    "cost-state",
    "frame-link",
    "artifact-autoreact-ledger",
    "artifact-comment-monitor",
    "pr-link",
    "agent-name",
    "started",
    "launched",
    "failed",
    "result",
})
_IGNORED_SYSTEM_SUBTYPES = frozenset({
    "stop_hook_summary",
    "local_command",
    "informational",
    "compact_boundary",
    "bridge_status",
})


class ClaudeDecoder:
    """Turns each Claude Code JSONL line into canonical records.

    Every record `type` in the corpus (about 200k lines across 1,175 files) is
    classified here; a type this table does not name is ``Unknown``. A decoder
    holds only decoding state: the first session id, agent id, cwd and branch
    seen, and whether a response has appeared (a turn-duration with none to
    describe is ignored, not carried to the next response).

    | Record | Becomes | Why |
    |---|---|---|
    | `user`, a prompt | PromptRec | A human turn. Injected content (`isMeta`, or a `promptSource` other than typed/queued) stays a prompt with `meta_text` blocks, so it never reaches user-text search. |
    | `user`, with `tool_result` blocks | one ToolResultRec per block | Results pair to their call in either order. A text block beside a result is Ignored (`user:block-beside-tool_result`): four records, the fork directive. |
    | `assistant` | ResponseRec, then one ToolCallRec per `tool_use` block | Chunks of one API message share `message.id` and merge, so a message's usage is counted once. |
    | `system/turn_duration` | PatchRec on the latest response | Wall-clock duration of the turn. |
    | `system/stop_hook_summary` | Ignored | Telemetry of hooks that ran. |
    | `system/local_command` | Ignored | Output of a local slash command, not a model turn. |
    | `system/informational` | Ignored | A notice shown in the UI. |
    | `system/compact_boundary` | Ignored | Marks where compaction happened; the summary that follows is its own user record. |
    | `system/bridge_status` | Ignored | Remote-control banner. |
    | `ai-title` | Meta (`title`) | Re-emitted per turn; the last one wins. |
    | `fork-context-ref` | LineageRec (fork) | The only explicit pointer from a fork to its parent session and the record it forked at. |
    | `attachment` | Ignored (`attachment:<subtype>`) | Context the harness showed the model (reminders, listings, hook output, file snapshots), not a turn. 45 subtypes. One is a known omission, below. |
    | `attachment`, a human `queued_command` | Ignored (`attachment:queued_command:human-prompt`) | A message the user typed while a turn ran: delivered to the model inside the turn, and recorded only here (29 in the corpus, none repeated as a user record). Not stored today, as before the port; named apart so the ledger counts it rather than hiding it among the harness's own context. Storing it would add a prompt in mid-turn, which is its own change. |
    | `last-prompt` | Ignored | Restates the latest prompt, which is its own user record. |
    | `mode` | Ignored | UI mode toggle. |
    | `permission-mode` | Ignored | Permission mode toggle. |
    | `queue-operation` | Ignored | Input-queue bookkeeping; a delivered prompt arrives as its own user record or a `queued_command` attachment. |
    | `file-history-snapshot` | Ignored | File backups for rewind. |
    | `file-history-delta` | Ignored | File backups for rewind. |
    | `atis-latch` | Ignored | Client feature latch; no conversation content. |
    | `bridge-session` | Ignored | Remote-control bookkeeping. |
    | `api-request` | Ignored | Request debugging: parameters of an API call. |
    | `api-request-blob` | Ignored | Request debugging: content-addressed pieces of an API call. |
    | `api-request-shape` | Ignored | Request debugging: the shape behind a request hash. |
    | `cost-state` | Ignored | Client-computed running cost; the cost model prices from tokens. |
    | `frame-link` | Ignored | Link to a published artifact frame. |
    | `artifact-autoreact-ledger` | Ignored | Artifact comment-monitor state. |
    | `artifact-comment-monitor` | Ignored | Artifact comment-monitor state. |
    | `pr-link` | Ignored | The pull request a session is linked to; not stored today. |
    | `agent-name` | Ignored | The session's agent name; not stored today. |
    | `started` | Ignored | Workflow run journal; the child's transcript is its own file. |
    | `launched` | Ignored | Workflow run journal; the child's transcript is its own file. |
    | `failed` | Ignored | Workflow run journal; the child's transcript is its own file. |
    | `result` | Ignored | Workflow run journal; the child's transcript is its own file. |

    Identity. The first ``sessionId`` and the first ``agentId`` on any user or
    assistant record name the conversation: ``claude_code::<sessionId>``, plus
    ``::agent::<agentId>`` for a sub-agent file. Claude Code writes both on every
    record, but a file that states them on different records still gets one
    name, so it is stated once, by ``finish()``, from all of them. A file with no
    ``sessionId`` anywhere is named for its stem. A sub-agent's lineage names
    the session it ran in; a fork's lineage names the parent and the record it
    forked at. The assembler keeps both, as they are different relations (a
    forked sub-agent is both), and peek's sub-agent identity reads the first.

    Time. Only user and assistant records state a time, so conversation bounds
    are the first and last of those. Every other record is given none: a hook
    summary or queue entry is not part of the conversation's span.
    """

    def __init__(self, source: Source):
        self._path = source.as_path
        self._seen_message = False
        self._session_id: str | None = None
        self._agent_id: str | None = None
        self._cwd: str | None = None
        self._branch: str | None = None
        self._responded = False

    def decode(self, unit: dict) -> list[Record]:
        kind = unit.get("type")
        if not isinstance(kind, str):
            return [Unknown(reason="no type")]
        if kind in ("user", "assistant"):
            return self._message(unit)
        if kind == "system":
            return [self._system(unit)]
        if kind == "ai-title":
            title = unit.get("aiTitle")
            if not title:
                return [Ignored(reason="ai-title:empty")]
            return [Meta(attributes={"title": str(title)})]
        if kind == "fork-context-ref":
            parent = unit.get("parentSessionId")
            if not parent:
                return [Unknown(reason="fork-context-ref:no-parent")]
            anchor = unit.get("parentLastUuid")
            return [LineageRec(at=None, kind="fork", parent=f"{NAME}::{parent}", anchor=anchor or None)]
        if kind == "attachment":
            attachment = unit.get("attachment")
            subtype = attachment.get("type") if isinstance(attachment, dict) else None
            if isinstance(attachment, dict) and subtype == "queued_command" and _is_human_prompt(attachment):
                return [Ignored(reason="attachment:queued_command:human-prompt")]
            return [Ignored(reason=f"attachment:{subtype}" if subtype else "attachment")]
        if kind in _IGNORED_TYPES:
            return [Ignored(reason=kind)]
        return [Unknown(reason=f"type:{kind}")]

    def finish(self) -> list[Record]:
        """What only the end of the file settles: the conversation's name, and the branch.

        The name is the whole file's decision (the first sessionId and the first
        agentId, wherever each appears), so it is stated once, here. Content
        that arrived earlier belongs to the conversation this names.
        """
        out: list[Record] = []
        if self._seen_message:
            out.append(Session(external_id=self._external_id()))
            if self._agent_id:
                out.extend(self._sidecar())
                if self._session_id:
                    out.append(LineageRec(at=None, kind="subagent", parent=f"{NAME}::{self._session_id}"))
        # Records carry the branch that was live when each message was written
        # (`gitBranch`). Recomputing from the cwd's *current* git state at ingest
        # time mislabels any session ingested after a branch switch, so the live
        # lookup is only a fallback for old logs that predate the per-record field.
        if self._branch is None and self._cwd:
            from siftd.git import get_worktree_branch

            branch = get_worktree_branch(self._cwd)
            if branch:
                out.append(Meta(branch=branch))
        return out

    # -- system --

    def _system(self, unit: dict) -> Record:
        subtype = unit.get("subtype")
        if subtype == "turn_duration":
            # Emitted after a turn's last assistant message; the wall-clock
            # duration is an attribute of that response (the domain has no
            # latency home).
            duration = unit.get("durationMs")
            if duration is None:
                return Ignored(reason="system:turn_duration:no-duration")
            if not self._responded:
                return Ignored(reason="system:turn_duration:no-response")
            return PatchRec(at=None, attributes={"turn_duration_ms": str(duration)})
        if subtype in _IGNORED_SYSTEM_SUBTYPES:
            return Ignored(reason=f"system:{subtype}")
        return Unknown(reason=f"system:{subtype}")

    # -- user / assistant --

    def _message(self, unit: dict) -> list[Record]:
        out = self._context(unit)
        role = effective_role(unit)
        message = unit.get("message")
        message = message if isinstance(message, dict) else {}
        at = unit.get("timestamp") or None
        blocks = _normalize_content(message.get("content"))
        if role == "user":
            out.extend(self._user(unit, at, blocks))
        elif role == "assistant":
            out.extend(self._assistant(unit, message, at, blocks))
        else:
            out.append(Ignored(reason=f"role:{role}"))
        return out

    def _context(self, unit: dict) -> list[Record]:
        """The workspace and branch this record states, the first time each is stated.

        The session's name is not stated here: ``sessionId`` and ``agentId`` may
        each first appear on a different record, and a name stated early cannot
        be corrected, so ``finish()`` states it from all of them.
        """
        self._seen_message = True
        self._session_id = self._session_id or unit.get("sessionId") or None
        self._agent_id = self._agent_id or unit.get("agentId") or None
        cwd = None if self._cwd else unit.get("cwd") or None
        branch = None if self._branch else unit.get("gitBranch") or None
        self._cwd = self._cwd or cwd
        self._branch = self._branch or branch
        return [Meta(workspace_path=cwd, branch=branch)] if cwd or branch else []

    def _external_id(self) -> str:
        external_id = f"{NAME}::{self._session_id or self._path.stem}"
        return f"{external_id}::agent::{self._agent_id}" if self._agent_id else external_id

    def _sidecar(self) -> list[Record]:
        """Sub-agent type and description from the `agent-<id>.meta.json` beside the transcript.

        The sidecar authoritatively binds this agentId to {agentType, description};
        the child JSONL itself carries no type, and reconstructing it from the
        spawning parent's tool_call (which has no agentId key) is a lossy join.
        Absent for top-level sessions and for historical sub-agents whose sidecar
        rotated off disk before ingest: both degrade silently to no attribute.
        """
        meta = _read_subagent_meta(self._path)
        if meta is None:
            return []
        attributes: dict[str, str] = {}
        if agent_type := meta.get("agentType"):
            attributes["subagent_type"] = str(agent_type)
        if description := meta.get("description"):
            attributes["agent_description"] = str(description)
        return [Meta(attributes=attributes)] if attributes else []

    def _user(self, unit: dict, at: str | None, blocks: list) -> list[Record]:
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            return self._tool_results(unit, at, blocks, results)
        # Injected content (isMeta / non-user promptSource) keeps its place in the
        # conversation but is reclassified so it never pollutes user-prompt FTS
        # search: the text moves off the "text" key (which is what gets indexed)
        # onto "meta_text", and the block type records the injection source.
        injected = _is_injected_user_record(unit)
        content: list[ContentBlock] = []
        for block in blocks:
            content_block = parse_block(block)
            if injected and "text" in content_block.content:
                fields = dict(content_block.content)
                fields["meta_text"] = fields.pop("text")
                fields["meta_source"] = unit.get("promptSource") or "isMeta"
                content_block = ContentBlock(block_type="meta_text", content=fields)
            content.append(content_block)
        uuid = unit.get("uuid")
        return [PromptRec(at=at, content=content, external_id=f"{NAME}::{uuid}" if uuid else None)]

    def _tool_results(self, unit: dict, at: str | None, blocks: list, results: list[dict]) -> list[Record]:
        # The top-level structured toolUseResult belongs to the record's single
        # tool_result block; it is left out in the rare multi-block case where
        # the pairing would be ambiguous.
        structured = _tool_use_result_attributes(unit.get("toolUseResult")) if len(results) == 1 else {}
        out: list[Record] = []
        for block in results:
            call_id = block.get("tool_use_id")
            if not call_id:
                out.append(Unknown(reason="tool_result:no-tool_use_id"))
                continue
            content = block.get("content")
            attributes = dict(structured)
            if isinstance(content, str) and (match := _BASH_BG_ID_RE.match(content)):
                attributes["background_task_id"] = match.group(1)
            out.append(
                ToolResultRec(
                    at=at,
                    call_id=call_id,
                    result={"content": content},
                    status="error" if block.get("is_error") else "success",
                    attributes=attributes,
                )
            )
        if len(results) != len(blocks):
            out.append(Ignored(reason="user:block-beside-tool_result"))
        return out

    def _assistant(self, unit: dict, message: dict, at: str | None, blocks: list) -> list[Record]:
        usage, usage_details = _usage_snapshot(message.get("usage"))
        message_id = message_id_of(unit)
        uuid = unit.get("uuid")
        self._responded = True
        out: list[Record] = [
            ResponseRec(
                at=at,
                content=[parse_block(b) for b in blocks],
                external_id=f"{NAME}::{uuid}" if uuid else None,
                key=message_id,
                model=message.get("model"),
                usage=usage,
                usage_details=usage_details,
            )
        ]
        for block in blocks:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            call_id = block.get("id")
            name = block.get("name", "unknown")
            raw_input = block.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            attributes: dict[str, str] = {}
            if name == "BashOutput" and tool_input.get("bash_id"):
                # A poll names the task it reads structurally: no parsing needed.
                attributes["background_task_id"] = str(tool_input["bash_id"])
            out.append(
                ToolCallRec(
                    at=at,
                    name=name,
                    input=tool_input,
                    call_id=call_id or None,
                    parent_key=message_id,
                    attributes=attributes,
                )
            )
        return out


read = read_jsonl
decoder = ClaudeDecoder


def default_session(source: Source) -> Session:
    """The conversation of a file that names no session of its own."""
    return Session(external_id=f"{NAME}::{source.as_path.stem}")


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


# Subagent .meta.json keys → PeekScanResult.attributes keys. Names match the
# parent-side tool_call attributes vocabulary (_tool_use_result_attributes:
# agent_name/agent_type/agent_model/spawn_depth). Note the ingest-side
# conversation attribute is `subagent_type` — a stored DB contract queried by
# api/conversations.py — so the two surfaces intentionally differ.
_SUBAGENT_META_KEYS: dict[str, str] = {
    "name": "agent_name",
    "agentType": "agent_type",
    "spawnDepth": "spawn_depth",
    "model": "agent_model",
}


def peek_enrich(path: Path, result: PeekScanResult) -> PeekScanResult:
    """The derived scan with subagent .meta.json enrichment.

    A subagent log at .../subagents/agent-<name>-<hash>.jsonl has a sibling
    agent-<name>-<hash>.meta.json carrying identity the JSONL itself lacks
    (name/agentType/spawnDepth/model). Surface it via the scan attributes
    channel; fill result.model from the meta when the log had none.
    """
    if SUBAGENT_PATH_MARKER not in str(path):
        return result
    meta = _read_subagent_meta(path)
    if meta is None:
        return result
    for src, dst in _SUBAGENT_META_KEYS.items():
        value = meta.get(src)
        if value is not None:
            result.attributes[dst] = str(value)
    if result.model is None and meta.get("model"):
        result.model = str(meta["model"])
    return result
