"""Antigravity CLI adapter for siftd.

Pure parser. The adapter only decodes: :class:`AntigravityDecoder` turns each
transcript step into canonical records, and the shared assembler
(:mod:`siftd.adapters.assembly`) decides where they attach.

Antigravity CLI writes one growing JSONL transcript per conversation at
``brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl``. Each
line is a step: a user turn, a model turn (``PLANNER_RESPONSE``, declaring zero
or more tool calls), the executed result of a declared call, or harness
scaffolding (reminders, checkpoints, error notices, inter-task messages).

Pairing is positional, because the format carries no call id. A result step
consumes the oldest declaration not yet resulted; the decoder names each
declaration ``<step_index>:<n>`` and gives its result that name. Only steps that
run a tool consume a declaration. ``ERROR_MESSAGE``, ``EPHEMERAL_MESSAGE`` and
``SYSTEM_MESSAGE`` never do. A reminder or API error that took a declaration
hands that call the next real result, which is the wrong one. A declaration still
unresolved when the next model or user turn begins stays a
``pending`` call with no result. A result with no declaration at all is an
orphan, named and counted by the ledger rather than stored as a call.

A backgrounded call is a result step with status RUNNING whose content names a
task id. Its output is interim (``final=False``) and the call stays open. Its
completion arrives later as a ``SYSTEM_MESSAGE`` naming the same task id, which
the decoder maps back to the call.

Token usage: a PLANNER_RESPONSE carries ``input_tokens``, ``cache_read_tokens``
and ``output_tokens`` in 253 of 496 corpus transcripts (all from 2026-10 on).
Antigravity reports ``input_tokens`` EXCLUSIVE of cache reads: it is below
``cache_read_tokens`` in 4,105 of 4,934 corpus records, which an inclusive count
cannot produce. The stored input is the inclusive total, because
``uncached_input_sql`` reads a ``google``-source row that way and recovers the
native input as ``total - cache_read``. The cache read is stored as the
``cache_read_input_tokens`` response attribute, which ``cost.sql`` and the usage
rollup read. No cache-creation count is reported.

Workspace: the transcript does not name one. It is recovered from the Antigravity
CLI root in two places, first ``history.jsonl`` (the workspace of a submitted
message, by conversation id), then ``conversation_summaries.db`` (the first entry
of ``workspace_uris``). The two agree wherever both name one.

Model: the transcript does not name one either. It is recovered from the sibling
per-conversation SQLite DB (``conversations/<conversation-id>.db``, table
``gen_metadata``, protobuf blobs). When the DB is missing or unreadable, or its
rows disagree, the model is None.

Known gaps, each named where it is decided:
    - Step media (screenshots and other images on GENERIC steps) is not stored.
    - RUN_COMMAND exit codes are not stored.
    - CHECKPOINT summaries of truncated context are skipped by name.
    - SYSTEM_MESSAGE notices that are not a task's completion are skipped by name,
      including cancellation notices: a canceled background call keeps its
      interim output and stays pending.
"""

import functools
import json
import re
import sqlite3
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse

from siftd.adapters._jsonl import load_jsonl
from siftd.adapters.assembly import read_jsonl
from siftd.adapters.records import (
    Ignored,
    Malformed,
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
from siftd.adapters.sdk import open_external_db
from siftd.domain import ContentBlock, Source, Usage

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 2
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

# Step types that are the executed result of a tool call a PLANNER_RESPONSE declared.
_TOOL_RESULT_STEPS = frozenset({
    "GENERIC",
    "RUN_COMMAND",
    "VIEW_FILE",
    "CODE_ACTION",
    "GREP_SEARCH",
    "LIST_DIRECTORY",
    "SEARCH_WEB",
    "READ_URL_CONTENT",
})

# Step types that are not conversation content, each with the reason it is skipped.
_IGNORED_STEPS: dict[str, str] = {
    "CONVERSATION_HISTORY": "conversation-history",
    "CHECKPOINT": "checkpoint",
    "EPHEMERAL_MESSAGE": "ephemeral-message",
}

_KNOWN_STEPS = _TOOL_RESULT_STEPS | frozenset(_IGNORED_STEPS) | {
    "USER_INPUT",
    "PLANNER_RESPONSE",
    "SYSTEM_MESSAGE",
    "ERROR_MESSAGE",
}

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


class AntigravityDecoder:
    """Turns each transcript step into canonical records.

    Every step ``type`` in the corpus (21,129 steps across 496 transcripts) is
    classified here; a type this table does not name is ``Unknown``. A decoder
    holds only decoding state: the declarations not yet resulted, the background
    tasks still open, whether a model turn has begun since the last user turn, and
    the model the sibling DB names.

    | Step | Becomes | Why |
    |---|---|---|
    | `USER_INPUT` | PromptRec | A human turn. The `<USER_REQUEST>` wrapper is stripped. |
    | `PLANNER_RESPONSE` | ResponseRec, then one ToolCallRec per declared call | A model turn: thinking, text, the declared calls. Its tokens are the response's usage snapshot. |
    | `GENERIC`, `RUN_COMMAND`, `VIEW_FILE`, `CODE_ACTION`, `GREP_SEARCH`, `LIST_DIRECTORY`, `SEARCH_WEB`, `READ_URL_CONTENT` | ToolResultRec | The result of the oldest declaration not yet resulted. DONE is success, ERROR is error. RUNNING is interim: it leaves the call open. A result with no declaration gets a call id no call has, so the assembler counts it as an orphan. |
    | `SYSTEM_MESSAGE`, a task's completion | ToolResultRec on the call that task id names | The completion of a backgrounded call. The log file is read when the notice points at one. |
    | `SYSTEM_MESSAGE`, anything else | Meta (time) + Ignored (`system-message:other`) | Inter-task messages and cancellation notices. A notice for a task this file never declared is Ignored (`system-message:unknown-task`). |
    | `ERROR_MESSAGE` | PatchRec (`error` attribute) on the turn's response | An API or stream error that interrupted the turn. Before the turn's first response there is none to describe: Ignored (`error-message:before-response`). |
    | `EPHEMERAL_MESSAGE` | Meta (time) + Ignored (`ephemeral-message`) | Reminders the harness shows the model. Not a user turn, and not a tool result. |
    | `CHECKPOINT` | Meta (time) + Ignored (`checkpoint`) | A summary of context the harness truncated. |
    | `CONVERSATION_HISTORY` | Meta (time) + Ignored (`conversation-history`) | A marker that earlier history was loaded. |

    Time. Every step that states a time gives it to the conversation's span, skipped,
    unknown or not, as the span has always covered every step. Ignored and Unknown
    records carry no time, so a step's time travels in a Meta beside them.

    Identity. The steps name no session. The conversation is named for its brain
    directory by :func:`default_session`.
    """

    def __init__(self, source: Source):
        path = source.as_path
        self._model = _resolve_model(path)
        # Declarations not yet resulted, as call ids, oldest first.
        self._pending: deque[str] = deque()
        # Background task id -> the call id it was declared under, until it completes.
        self._open_tasks: dict[str, str] = {}
        # Has a model turn begun since the latest user turn?
        self._responded = False

    def decode(self, unit: dict) -> list[Record]:
        at = unit.get("created_at") or None
        kind = unit.get("type")
        if not isinstance(kind, str):
            return _timed(at, Unknown(reason="no type"))
        if kind not in _KNOWN_STEPS:
            return _timed(at, Unknown(reason=f"type:{kind}"))
        if kind == "USER_INPUT":
            return self._user(unit, at)
        if kind == "PLANNER_RESPONSE":
            return self._planner(unit, at)
        if kind == "SYSTEM_MESSAGE":
            return self._system(unit, at)
        if kind == "ERROR_MESSAGE":
            return self._error(unit, at)
        if kind in _TOOL_RESULT_STEPS:
            return self._result(unit, at)
        return _skipped(at, _IGNORED_STEPS[kind])

    # -- user and model turns --

    def _user(self, unit: dict, at: str | None) -> list[Record]:
        # A declaration still unresolved at a user turn will never get a result.
        self._pending.clear()
        self._responded = False
        text = _extract_user_text(unit.get("content", ""))
        return [PromptRec(at=at, content=[ContentBlock(block_type="text", content={"text": text})])]

    def _planner(self, unit: dict, at: str | None) -> list[Record]:
        step = unit.get("step_index")
        if not isinstance(step, int):
            return _timed(at, Unknown(reason="PLANNER_RESPONSE:no-step_index"))
        # Declarations the previous turn never resulted stay unresolved.
        self._pending.clear()
        self._responded = True

        content: list[ContentBlock] = []
        if thinking := unit.get("thinking"):
            content.append(ContentBlock(block_type="thinking", content={"text": thinking}))
        if text := unit.get("content"):
            content.append(ContentBlock(block_type="text", content={"text": text}))
        calls: list[Record] = []
        for n, tc in enumerate(unit.get("tool_calls") or []):
            name = tc.get("name", "unknown")
            tool_input = _parse_tool_args(tc.get("args") or {})
            call_id = f"{step}:{n}"
            # The block carries its call's id, so peek shows the call once, at the block's position.
            content.append(
                ContentBlock(block_type="tool_use", content={"id": call_id, "name": name, "input": tool_input})
            )
            self._pending.append(call_id)
            calls.append(ToolCallRec(at=at, name=name, input=tool_input, call_id=call_id))
        usage, usage_details = _planner_usage(unit)
        return [
            ResponseRec(
                at=at,
                content=content,
                model=self._model,
                usage=usage,
                usage_details=usage_details,
            ),
            *calls,
        ]

    def _error(self, unit: dict, at: str | None) -> list[Record]:
        text = unit.get("error") or unit.get("content") or ""
        if not text:
            return _skipped(at, "error-message:empty")
        if not self._responded:
            return _skipped(at, "error-message:before-response")
        # Every response is a model turn, so the latest one is this turn's.
        return [PatchRec(at=at, attributes={"error": str(text)})]

    # -- tool results and their completions --

    def _result(self, unit: dict, at: str | None) -> list[Record]:
        step = unit.get("step_index")
        if not isinstance(step, int):
            return _timed(at, Unknown(reason="result:no-step_index"))
        call_id = self._pending.popleft() if self._pending else f"{step}:result"
        status = unit.get("status")
        content = unit.get("content") or ""
        attributes: dict[str, str] = {}
        if error := unit.get("error"):
            attributes["error"] = str(error)
        result = {"output": content}
        if status == "DONE":
            return [ToolResultRec(at=at, call_id=call_id, result=result, status="success", attributes=attributes)]
        if status == "ERROR":
            return [ToolResultRec(at=at, call_id=call_id, result=result, status="error", attributes=attributes)]
        # RUNNING, or a status not yet seen: the call is still in flight.
        if status == "RUNNING" and (started := _TASK_STARTED_RE.search(content)):
            attributes["background_task_id"] = started.group(1)
            self._open_tasks[started.group(1)] = call_id
        return [
            ToolResultRec(
                at=at, call_id=call_id, result=result, status="pending", attributes=attributes, final=False
            )
        ]

    def _system(self, unit: dict, at: str | None) -> list[Record]:
        content = unit.get("content") or ""
        finished = _TASK_FINISHED_RE.search(content)
        if finished is None:
            return _skipped(at, "system-message:other")
        task_id, inline_output, log_path = finished.group(1), finished.group(2), finished.group(3)
        call_id = self._open_tasks.pop(task_id, None)
        if call_id is None:
            return _skipped(at, "system-message:unknown-task")
        output = _task_log(log_path) if log_path else None
        if output is None:
            output = inline_output.strip()
        return [ToolResultRec(at=at, call_id=call_id, result={"output": output}, status="success")]


def read(source: Source) -> list:
    """The transcript's units in step order, not file order.

    A transcript is appended as steps complete, so when a model turn calls several
    tools at once their results can be written before the turn itself (29 corpus
    transcripts; 152 out-of-order lines). ``step_index`` is the order the turn
    happened in, and pairing a result to its declaration depends on it. A unit
    stating no index keeps the place it had in the file.
    """
    keyed = []
    key = -1
    for pos, unit in enumerate(read_jsonl(source)):
        if not isinstance(unit, Malformed):
            step = unit.get("step_index")
            if isinstance(step, int):
                key = step
        keyed.append((key, pos, unit))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [unit for _, _, unit in keyed]


def _timed(at: str | None, record: Record) -> list[Record]:
    """A record that carries no time of its own, with its step's time as evidence when it states one.

    Ignored and Unknown steps both lend their time to the span through a Meta beside
    them, so an unfamiliar step at either end of a transcript still bounds it.
    """
    out: list[Record] = [Meta(at=at)] if at is not None else []
    out.append(record)
    return out


def _skipped(at: str | None, reason: str) -> list[Record]:
    """An Ignored step, with its time as evidence when it states one (see the decoder table)."""
    return _timed(at, Ignored(reason=reason))


def _planner_usage(unit: dict) -> tuple[Usage | None, dict[str, int]]:
    """A PLANNER_RESPONSE's usage snapshot, in siftd's stored convention, or nothing when it carries none.

    Antigravity's native ``input_tokens`` EXCLUDES cache reads, so the stored input is
    the sum. ``uncached_input_sql`` (the default branch for a ``google``-source row)
    subtracts ``cache_read`` again, which recovers the native input exactly. Storing
    the native value would make the cost path subtract the cached part a second time
    and under-count uncached input.
    """
    if "input_tokens" not in unit:
        return None, {}
    native_input = unit.get("input_tokens") or 0
    cache_read = unit.get("cache_read_tokens") or 0
    usage = Usage(input_tokens=native_input + cache_read, output_tokens=unit.get("output_tokens") or 0)
    # Zero is stored too: a turn that read nothing from cache says so.
    return usage, {"cache_read_input_tokens": int(cache_read)}


def _task_log(log_path: str) -> str | None:
    """The full output of a finished background task, from the log file its notice names, if still on disk."""
    try:
        return Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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


decoder = AntigravityDecoder


def default_session(source: Source) -> Session:
    """The conversation of a transcript: named for its brain directory, with the workspace it ran in."""
    path = source.as_path
    workspace = source.metadata.get("workspace_path") or _resolve_workspace(path)
    return Session(external_id=f"{NAME}::{_session_id(path)}", workspace_path=workspace)


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


@functools.cache
def _load_summary_workspaces(root: Path) -> dict[str, str]:
    """Map conversation id -> the first workspace in conversation_summaries.db's workspace_uris.

    The list is the conversation's workspace roots, and its first entry is the one
    history.jsonl names wherever both record a conversation. Read once per root and
    cached for the process, as the history is.
    """
    db_path = root / "conversation_summaries.db"
    if not db_path.is_file():
        return {}
    try:
        conn = open_external_db(db_path)
        try:
            rows = conn.execute("SELECT conversation_id, workspace_uris FROM conversation_summaries").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    workspaces: dict[str, str] = {}
    for row in rows:
        try:
            first = json.loads(row["workspace_uris"] or "[]")[0]
        except (ValueError, IndexError, TypeError):
            continue
        if isinstance(first, str) and first.startswith("file://"):
            workspaces.setdefault(row["conversation_id"], unquote(urlparse(first).path))
    return workspaces


def _resolve_workspace(transcript_path: Path) -> str | None:
    """Resolve a transcript's workspace from the Antigravity CLI root: history first, then the summaries."""
    try:
        root = transcript_path.parents[4]
    except IndexError:
        return None
    conv_id = _session_id(transcript_path)
    return _load_history_workspaces(root).get(conv_id) or _load_summary_workspaces(root).get(conv_id)


# =============================================================================
# Model identity — recovered from the sibling conversations/<id>.db
# =============================================================================
#
# Antigravity CLI keeps a per-conversation SQLite DB next to the brain/ tree:
# ``<root>/conversations/<conversation-id>.db``. Its ``gen_metadata`` table
# holds one protobuf blob per generation; field 1.21 of each blob is the
# human-readable model display name (e.g. "Gemini 3.5 Flash (Medium)") and
# field 1.20 is a self-describing string map carrying ``model_enum``. There is
# no published schema -- the field numbers are reverse-engineered -- so the
# parse is a plain wire-format field walk (no proto compiler) and any failure
# degrades to model=None, i.e. the pre-existing JSONL-only behavior.


def _iter_proto_fields(buf: bytes):
    """Walk protobuf wire-format fields: yields (field_no, wire_type, value).

    Wire types: 0 varint (int), 1 fixed64 (bytes), 2 len-delimited (bytes),
    5 fixed32 (bytes). Raises ValueError on malformed input.
    """
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        field_no, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            value, i = _read_varint(buf, i)
        elif wire_type == 1:
            value, i = buf[i : i + 8], i + 8
        elif wire_type == 2:
            length, i = _read_varint(buf, i)
            if i + length > n:
                raise ValueError("truncated len-delimited field")
            value, i = buf[i : i + length], i + length
        elif wire_type == 5:
            value, i = buf[i : i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        yield field_no, wire_type, value


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("truncated or oversized varint")
        byte = buf[i]
        result |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return result, i
        shift += 7


def _model_from_gen_metadata(blob: bytes) -> str | None:
    """Extract the model display name from one gen_metadata protobuf blob.

    Prefers field 1.21 (display name); falls back to the ``model_enum`` entry
    of the 1.20 string map. Returns None if neither is present or the blob is
    malformed.
    """
    try:
        sub = next(
            (v for f, w, v in _iter_proto_fields(blob) if f == 1 and w == 2),
            None,
        )
        if not isinstance(sub, bytes):
            return None
        display_name = None
        map_model = None
        for field_no, wire_type, value in _iter_proto_fields(sub):
            if wire_type != 2 or not isinstance(value, bytes):
                continue
            if field_no == 21:
                display_name = value.decode("utf-8", errors="replace")
            elif field_no == 20:
                key = val = None
                for kf, kw, kv in _iter_proto_fields(value):
                    if kw == 2 and isinstance(kv, bytes):
                        if kf == 1:
                            key = kv.decode("utf-8", errors="replace")
                        elif kf == 2:
                            val = kv.decode("utf-8", errors="replace")
                if key == "model_enum":
                    map_model = val
        return display_name or map_model
    except ValueError:
        return None


def _resolve_model(transcript_path: Path) -> str | None:
    """Recover the conversation's model from the sibling conversations DB.

    Opens ``<root>/conversations/<conversation-id>.db`` read-only and reads
    every ``gen_metadata`` row. Returns the model only when all rows that
    yield one agree -- per-generation attribution to specific transcript
    steps is unverified, so a mixed-model conversation degrades to None
    rather than guessing. Any DB or parse failure also returns None.
    """
    try:
        root = transcript_path.parents[4]
    except IndexError:
        return None
    db_path = root / "conversations" / f"{_session_id(transcript_path)}.db"
    if not db_path.is_file():
        return None
    try:
        conn = open_external_db(db_path)
        try:
            rows = conn.execute("SELECT data FROM gen_metadata").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    models = {
        m
        for (blob,) in rows
        if isinstance(blob, bytes) and (m := _model_from_gen_metadata(blob))
    }
    if len(models) == 1:
        return models.pop()
    return None
