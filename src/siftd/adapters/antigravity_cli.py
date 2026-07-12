"""Antigravity CLI adapter for siftd.

Pure parser: reads transcript JSONL files and yields Conversation domain
objects. No storage coupling.

Antigravity CLI writes one growing JSONL transcript per conversation at
``brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl``.
Each line is a "step": a user turn, a model turn (declaring zero or more
tool calls), or the executed result of a declared tool call. The format
carries no call id to correlate a declaration with its result -- pairing is
purely positional (a step type generally follows the PLANNER_RESPONSE step
that declared it). Session identity and workspace are not in the transcript
itself; workspace is recovered from a sibling ``history.jsonl`` at the
Antigravity CLI root, keyed by conversation id.

Known gaps (nothing in the source data to build these from):
    - No token usage or model id anywhere in the transcript.
    - Tool calls declared but never resolved before the transcript ends
      (e.g. a backgrounded run_command the user interrupted) surface as
      status="pending" ToolCalls with no result, same fallback used by
      codex_cli's flush_pending_calls.

Backgrounded tool calls (a RUN_COMMAND step whose status is RUNNING rather
than DONE) are a further wrinkle: the transcript records them as started,
then their actual completion arrives later as a free-text SYSTEM_MESSAGE
step, correlated by a task id embedded in both steps' prose -- not a
structured field. This is genuinely new ground: no other adapter in this
codebase does cross-event-type deferred correlation (ToolCallLinker and
flush_pending_calls in sdk.py both assume a clean structured id shared by a
declare/resolve pair of the *same* event shape, e.g. codex_cli's call_id).
Solved locally here (_open_background_tasks / _resolve_background_task)
rather than generalized into the SDK -- there's exactly one data point.
"""

import functools
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso
from siftd.adapters.sdk import (
    NormalizedRecord,
    build_harness,
    iter_jsonl,
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
)

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
SUPPORT_TIER = "core"
NAME = "antigravity_cli"
DEFAULT_LOCATIONS = ["~/.gemini/antigravity-cli"]
DEDUP_STRATEGY = "session"  # one conversation per brain/<id>; transcript grows over the session

# Without this, peek falls back to "**/*.jsonl" under DEFAULT_LOCATIONS, which
# would also match the top-level history.jsonl (not a transcript) and, when a
# conversation has both transcript.jsonl and transcript_full.jsonl, surface it
# twice. Full is a strict superset when present (verified against a real
# session), so peek -- unlike discover(), which needs the compact fallback for
# completeness -- only looks for it.
PEEK_GLOB_PATTERNS = ["brain/*/.system_generated/logs/transcript_full.jsonl"]

# Harness metadata
HARNESS_SOURCE = "google"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Antigravity CLI"

# Raw tool name -> canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "view_file": "file.read",
    "write_to_file": "file.write",
    "run_command": "shell.execute",
    "grep_search": "search.grep",
    "list_dir": "file.glob",
}

# Result-step type -> the raw tool name it executes, for the rare orphan case
# (a result step with no preceding declared tool_calls entry to pair with,
# e.g. a log truncated mid-turn). Keeps orphaned tool calls eligible for
# TOOL_ALIASES canonicalization instead of falling through as e.g.
# "list_directory" (from LIST_DIRECTORY.lower()), which TOOL_ALIASES doesn't
# recognize. GENERIC has no single underlying tool (list_permissions and
# others all surface as GENERIC) so it's deliberately left unmapped.
_RESULT_TYPE_TOOL_NAMES = {
    "VIEW_FILE": "view_file",
    "RUN_COMMAND": "run_command",
    "GREP_SEARCH": "grep_search",
    "LIST_DIRECTORY": "list_dir",
    "CODE_ACTION": "write_to_file",
}

# Step types that are scaffolding, not conversation turns: context-truncation
# summaries and the session-open marker. Every other step type is either
# USER_INPUT, PLANNER_RESPONSE, SYSTEM_MESSAGE (handled separately -- it may
# carry a background task's result), or the executed result of a tool call
# PLANNER_RESPONSE just declared.
_SKIP_TYPES = {"CHECKPOINT", "CONVERSATION_HISTORY"}

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\n(.*?)\n</USER_REQUEST>", re.DOTALL)

# A RUNNING RUN_COMMAND step's content names its own task id, e.g.:
#   "Tool is running as a background task with task id: <conv-id>/task-48"
_TASK_STARTED_RE = re.compile(r"background task with task id:\s*(\S+)")

# The eventual completion arrives as a SYSTEM_MESSAGE step, keyed by the same
# task id, with the (possibly truncated) output inline and -- when the task
# produced one -- a pointer to the untruncated log file:
#   'Task id "<conv-id>/task-48" finished with result:\n...\nLog: file:///path\n</SYSTEM_MESSAGE>'
_TASK_FINISHED_RE = re.compile(
    r'Task id "([^"]+)" finished with result:\s*\n*(.*?)(?:\n*Log: file://(\S+)\s*)?</SYSTEM_MESSAGE>',
    re.DOTALL,
)


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Antigravity CLI transcript files.

    Each conversation directory under brain/ may have both transcript.jsonl
    (compacted, checkpoint-truncated) and transcript_full.jsonl (untruncated);
    the full variant is preferred when present since it's a strict superset.
    """
    for location in locations or DEFAULT_LOCATIONS:
        base = Path(location).expanduser()
        brain = base / "brain"
        if not brain.is_dir():
            continue
        for conv_dir in sorted(brain.iterdir()):
            if not conv_dir.is_dir():
                continue
            logs_dir = conv_dir / ".system_generated" / "logs"
            full = logs_dir / "transcript_full.jsonl"
            compact = logs_dir / "transcript.jsonl"
            if full.is_file():
                yield Source(kind="file", location=full)
            elif compact.is_file():
                yield Source(kind="file", location=compact)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.name not in ("transcript_full.jsonl", "transcript.jsonl"):
        return False
    parts = path.parts
    return "brain" in parts and ".system_generated" in parts and "logs" in parts


def parse(source: Source) -> Iterable[Conversation]:
    """Parse an Antigravity CLI transcript JSONL file into a Conversation."""
    path = Path(source.location)
    records = load_jsonl(path)
    if not records:
        return

    session_id = _session_id(path)
    workspace_path = source.metadata.get("workspace_path") or _resolve_workspace(path)

    started_at: str | None = None
    ended_at: str | None = None
    for record in records:
        ts = record.get("created_at")
        if ts is None:
            continue
        if started_at is None or ts < started_at:
            started_at = ts
        if ended_at is None or ts > ended_at:
            ended_at = ts

    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    conversation = Conversation(
        external_id=f"{NAME}::{session_id}",
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=workspace_path,
    )

    current_prompt: Prompt | None = None
    current_response: Response | None = None
    # Tool calls declared by the most recent PLANNER_RESPONSE, matched
    # positionally against the next step -- the raw format has no call id.
    pending_tool_calls: list[tuple[str, dict]] = []
    # Backgrounded tool calls (RUNNING, not DONE), keyed by the task id
    # embedded in their content, waiting for a later SYSTEM_MESSAGE step to
    # report completion under that same id. Unlike pending_tool_calls, these
    # are already appended to a response (as status="pending") by the time
    # they're registered here -- resolution mutates them in place.
    open_background_tasks: dict[str, ToolCall] = {}

    def flush_pending() -> None:
        if not pending_tool_calls or current_response is None:
            pending_tool_calls.clear()
            return
        for tool_name, tool_input in pending_tool_calls:
            current_response.tool_calls.append(
                ToolCall(tool_name=tool_name, input=tool_input, result=None, status="pending")
            )
        pending_tool_calls.clear()

    for record in records:
        step_type = record.get("type")
        timestamp = record.get("created_at") or conversation.started_at

        if step_type in _SKIP_TYPES:
            continue

        if step_type == "USER_INPUT":
            flush_pending()
            current_prompt = Prompt(
                timestamp=timestamp,
                content=[
                    ContentBlock(
                        block_type="text",
                        content={"text": _extract_user_text(record.get("content", ""))},
                    )
                ],
            )
            conversation.prompts.append(current_prompt)
            current_response = None
            continue

        if step_type == "PLANNER_RESPONSE":
            flush_pending()
            if current_prompt is None:
                current_prompt = Prompt(timestamp=timestamp)
                conversation.prompts.append(current_prompt)
            current_response = Response(timestamp=timestamp)
            current_prompt.responses.append(current_response)

            thinking = record.get("thinking")
            if thinking:
                current_response.content.append(
                    ContentBlock(block_type="thinking", content={"text": thinking})
                )
            text = record.get("content")
            if text:
                current_response.content.append(ContentBlock(block_type="text", content={"text": text}))

            for tc in record.get("tool_calls") or []:
                tool_name = tc.get("name", "unknown")
                tool_input = _parse_tool_args(tc.get("args") or {})
                current_response.content.append(
                    ContentBlock(
                        block_type="tool_use",
                        content={"name": tool_name, "input": tool_input},
                    )
                )
                pending_tool_calls.append((tool_name, tool_input))
            continue

        if step_type == "SYSTEM_MESSAGE":
            _resolve_background_task(record.get("content", ""), open_background_tasks)
            continue

        # Any other step type is the executed result of a tool call the
        # preceding PLANNER_RESPONSE declared.
        if current_response is None:
            if current_prompt is None:
                current_prompt = Prompt(timestamp=timestamp)
                conversation.prompts.append(current_prompt)
            current_response = Response(timestamp=timestamp)
            current_prompt.responses.append(current_response)

        if pending_tool_calls:
            tool_name, tool_input = pending_tool_calls.pop(0)
        else:
            # Result step with no declared tool call to pair with (e.g. a
            # truncated log). Keep the content rather than dropping it.
            fallback_name = step_type.lower() if isinstance(step_type, str) else "unknown"
            tool_name = _RESULT_TYPE_TOOL_NAMES.get(step_type, fallback_name) if isinstance(step_type, str) else fallback_name
            tool_input = {}

        content = record.get("content", "")
        status = "success" if record.get("status") == "DONE" else "pending"
        tool_call = ToolCall(
            tool_name=tool_name,
            input=tool_input,
            result={"output": content},
            status=status,
            timestamp=timestamp,
        )
        current_response.tool_calls.append(tool_call)

        if record.get("status") == "RUNNING":
            started = _TASK_STARTED_RE.search(content)
            if started:
                tool_call.attributes["background_task_id"] = started.group(1)
                open_background_tasks[started.group(1)] = tool_call

    flush_pending()

    yield from yield_conversation(conversation)


def _resolve_background_task(content: str, open_tasks: dict[str, ToolCall]) -> None:
    """Resolve a backgrounded tool call from its completion SYSTEM_MESSAGE.

    Reads the full untruncated log file when the message points at one and
    it's still on disk; falls back to the (possibly truncated) inline text
    otherwise. No-op if the message isn't a task-completion notice, or names
    a task id we never saw declared (e.g. from a run that predates this
    transcript slice).
    """
    match = _TASK_FINISHED_RE.search(content)
    if not match:
        return
    task_id, inline_output, log_path = match.group(1), match.group(2), match.group(3)
    tool_call = open_tasks.pop(task_id, None)
    if tool_call is None:
        return

    output = inline_output.strip()
    if log_path:
        try:
            log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = None
        if log_text is not None:
            output = log_text

    tool_call.result = {"output": output}
    tool_call.status = "success"


def _session_id(transcript_path: Path) -> str:
    """The conversation id is the brain/<id> directory name."""
    try:
        return transcript_path.parents[2].name
    except IndexError:
        return transcript_path.stem


def _extract_user_text(content) -> str:
    """Strip the <USER_REQUEST>/<ADDITIONAL_METADATA> wrapper Antigravity adds to every turn."""
    if not isinstance(content, str):
        return str(content)
    match = _USER_REQUEST_RE.search(content)
    return match.group(1) if match else content


def _parse_tool_args(raw_args: dict) -> dict:
    """Unwrap Antigravity's per-value JSON encoding in tool_calls[].args.

    Each arg value is individually JSON-encoded (e.g. a string arg arrives
    as the 3-character-longer '"literal value"', a bool arg as the string
    "true"). Values that aren't valid JSON are kept as-is.
    """
    parsed = {}
    for key, value in raw_args.items():
        if isinstance(value, str):
            try:
                parsed[key] = json.loads(value)
                continue
            except (json.JSONDecodeError, ValueError):
                pass
        parsed[key] = value
    return parsed


@functools.cache
def _load_history_workspaces(root: Path) -> dict[str, str]:
    """Map conversationId -> workspace from the Antigravity CLI history.jsonl.

    history.jsonl is the only place the transcript's workspace lives; it is
    a flat log of every submitted message across all conversations at this
    root, so this is read once per root and cached for the process.
    """
    history_path = root / "history.jsonl"
    workspaces: dict[str, str] = {}
    if not history_path.is_file():
        return workspaces
    for record in load_jsonl(history_path):
        conv_id = record.get("conversationId")
        workspace = record.get("workspace")
        if conv_id and workspace and conv_id not in workspaces:
            workspaces[conv_id] = workspace
    return workspaces


def _resolve_workspace(transcript_path: Path) -> str | None:
    """Resolve a transcript's workspace via the sibling history.jsonl."""
    try:
        root = transcript_path.parents[4]
    except IndexError:
        return None
    return _load_history_workspaces(root).get(_session_id(transcript_path))


# =============================================================================
# Record normalization — enables SDK-derived peek support
# =============================================================================


def iter_antigravity_records(path: Path) -> Iterator[dict]:
    """Iterate transcript records, prefixed with a synthetic metadata record.

    The transcript itself carries no session id or workspace path -- those
    are recovered here (session id from the directory layout, workspace from
    the sibling history.jsonl) so normalize_record can surface them.
    """
    yield {
        "_kind": "metadata",
        "session_id": _session_id(path),
        "workspace_path": _resolve_workspace(path),
    }
    yield from iter_jsonl(path)


def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map an Antigravity CLI transcript record to NormalizedRecord.

    Step types:
        "_kind": "metadata"  → metadata (session id, workspace)
        "USER_INPUT"         → user (text, unwrapped from <USER_REQUEST>)
        "PLANNER_RESPONSE"   → assistant (thinking, text, declared tool_calls)
        anything else        → None (executed tool-call results aren't
                                surfaced separately in peek, same as the
                                declaration-only treatment codex_cli uses)
    """
    if raw.get("_kind") == "metadata":
        return NormalizedRecord(
            kind="metadata",
            session_id=raw.get("session_id"),
            workspace_path=raw.get("workspace_path"),
        )

    step_type = raw.get("type")
    timestamp = raw.get("created_at")

    if step_type == "USER_INPUT":
        text = _extract_user_text(raw.get("content", ""))
        return NormalizedRecord(
            kind="user",
            timestamp=timestamp,
            content_blocks=[{"type": "text", "text": text}] if text else [],
        )

    if step_type == "PLANNER_RESPONSE":
        content_blocks: list[dict] = []
        thinking = raw.get("thinking")
        if thinking:
            content_blocks.append({"type": "thinking", "text": thinking})
        text = raw.get("content")
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in raw.get("tool_calls") or []:
            content_blocks.append({
                "type": "tool_use",
                "name": tc.get("name", "unknown"),
                "input": _parse_tool_args(tc.get("args") or {}),
            })
        return NormalizedRecord(kind="assistant", timestamp=timestamp, content_blocks=content_blocks)

    return None


# Peek hooks — derived from normalizer with a custom record iterator that
# injects session/workspace identity (the transcript itself has neither).
peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    normalize_record,
    tool_aliases=TOOL_ALIASES,
    record_iterator=iter_antigravity_records,
)
