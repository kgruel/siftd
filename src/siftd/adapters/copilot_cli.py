"""Copilot CLI adapter for siftd.

Pure parser: reads JSONL event files and yields Conversation domain objects.
No storage coupling. The adapter only decodes: :class:`CopilotDecoder` turns
each event line into canonical records, and the shared assembler
(:mod:`siftd.adapters.assembly`) decides where they attach.
"""

import sys
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters.assembly import assemble, read_jsonl
from siftd.adapters.peek_projection import make_peek_hooks_from_decoder
from siftd.adapters.records import (
    Ignored,
    Meta,
    PromptRec,
    Record,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
)
from siftd.adapters.sdk import build_harness, discover_files
from siftd.domain import ContentBlock, Conversation, Source
from siftd.safecall import parse_json_args

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
SUPPORT_TIER = "contrib"
NAME = "copilot_cli"
DEDUP_STRATEGY = "file"  # one conversation per events.jsonl

# Platform-specific locations
if sys.platform == "win32":
    DEFAULT_LOCATIONS = ["~/AppData/Local/.copilot/session-state"]
else:
    DEFAULT_LOCATIONS = ["~/.local/state/.copilot/session-state"]

# Harness metadata
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Copilot CLI"

# Raw tool name → canonical tool name. Empty: ingest stores raw names, and
# peek canonicalizes through this table.
TOOL_ALIASES: dict[str, str] = {}

# Event types that are not conversation content. Each is Ignored by name; the
# docstring of CopilotDecoder says why, and tests/adapters/test_copilot_cli.py
# keeps that table and this set equal.
_IGNORED_TYPES = frozenset({
    "session.info",
    "assistant.turn_start",
    "assistant.turn_end",
    "tool.execution_start",
    "subagent.started",
    "subagent.completed",
})


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Copilot CLI event files."""
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/events.jsonl"])


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.name != "events.jsonl":
        return False
    path_str = str(path)
    for loc in DEFAULT_LOCATIONS:
        loc_expanded = str(Path(loc).expanduser())
        if loc_expanded in path_str:
            return True
    # Accept paths with .copilot/session-state component (for tests/mock paths)
    if ".copilot" in path.parts and "session-state" in path.parts:
        return True
    return False


HARNESS = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)


class CopilotDecoder:
    """Turns each Copilot CLI event line into canonical records.

    Every event `type` in the corpus is classified here; a type this table does
    not name is ``Unknown``. A decoder holds only decoding state: the last
    ``sessionId``, working directory and branch that a ``session.start`` stated,
    which name the conversation (the file's last header wins, as it always did).

    | Record | Becomes | Why |
    |---|---|---|
    | `session.start` | Meta (`at`) | Opens the session's clock. Its `cwd` and `branch` are the conversation's workspace and branch, stated by `finish()`. |
    | `session.model_change` | Meta (`model`) | The model from this event on. Responses after it carry it; a later change replaces it. |
    | `user.message` | PromptRec | A human turn. Its `content` is the text; `transformedContent` (harness-injected datetime and reminders) and `attachments` are not stored. |
    | `assistant.message` | ResponseRec, then one ToolCallRec per `toolRequests` entry | One model message, keyed by `messageId`. `reasoningText` is a thinking block. A message with `parentToolCallId` is a sub-agent's turn: its response carries `parent_tool_call_id` naming the spawning call. |
    | `tool.execution_complete` | ToolResultRec | Pairs to its call by `toolCallId`. `success` gives the status; `result` is kept whole; `toolTelemetry` is not stored. |
    | `session.info` | Ignored | Notices: MCP connection, sign-in, model change. Not conversation content. |
    | `assistant.turn_start` | Ignored | Turn boundary. The messages inside the turn carry the content. |
    | `assistant.turn_end` | Ignored | Turn boundary. |
    | `tool.execution_start` | Ignored | Announces a call that its `assistant.message` already requested; the request is the call. |
    | `subagent.started` | Ignored | Sub-agent lifecycle. Its `agentName` and description are not stored today; its turns are the messages with `parentToolCallId`. |
    | `subagent.completed` | Ignored | Sub-agent lifecycle. |

    Identity. The conversation is ``copilot_cli::<sessionId>``, or
    ``copilot_cli::<directory name>`` when no header states a session id. It is
    stated once, by ``finish()``.

    Time. Every event's timestamp is evidence. Prompts, responses and results
    carry their own; an event whose records are all Ignored or Unknown (a
    notice, a turn marker) is stated as a Meta at its time, so the span runs
    from the earliest event to the latest, as it did before the port.

    Usage. The format records no token counts (no event type carries one), so
    responses carry no usage.
    """

    def __init__(self, source: Source):
        self._path = source.as_path
        self._session_id: str | None = None
        self._cwd: str | None = None
        self._branch: str | None = None

    def decode(self, unit: dict) -> list[Record]:
        at = unit.get("timestamp") or None
        records = self._decode(unit, at)
        if at is not None and all(isinstance(r, (Ignored, Unknown)) for r in records):
            return [Meta(at=at), *records]
        return records

    def _decode(self, unit: dict, at: str | None) -> list[Record]:
        kind = unit.get("type")
        if not isinstance(kind, str):
            return [Unknown(reason="no type")]
        data = unit.get("data")
        if not isinstance(data, dict):
            data = {}
        if kind == "user.message":
            return [self._user(data, at)]
        if kind == "assistant.message":
            return self._assistant(data, at)
        if kind == "tool.execution_complete":
            return [self._tool_result(data, at)]
        if kind == "session.start":
            return [self._session_start(data, at)]
        if kind == "session.model_change":
            model = data.get("newModel")
            return [Meta(at=at, model=str(model) if model else None)]
        if kind in _IGNORED_TYPES:
            return [Ignored(reason=kind)]
        return [Unknown(reason=f"type:{kind}")]

    def finish(self) -> list[Record]:
        """The conversation's name and workspace: the file's last header, stated once."""
        return [Session(external_id=self._external_id(), workspace_path=self._cwd or None, branch=self._branch or None)]

    # -- events --

    def _session_start(self, data: dict, at: str | None) -> Record:
        # Last header wins, None included: the name and workspace have always
        # come from the last session.start in the file.
        self._session_id = data.get("sessionId")
        context = data.get("context")
        context = context if isinstance(context, dict) else {}
        self._cwd = context.get("cwd")
        self._branch = context.get("branch")
        return Meta(at=at)

    def _user(self, data: dict, at: str | None) -> PromptRec:
        content = data.get("content")
        blocks = [ContentBlock(block_type="text", content={"text": content})] if content else []
        return PromptRec(at=at, content=blocks)

    def _assistant(self, data: dict, at: str | None) -> list[Record]:
        message_id = data.get("messageId") or None
        requests = data.get("toolRequests") or []
        extras: list[Record] = []
        if not isinstance(requests, list):
            extras.append(Unknown(reason="assistant.message:toolRequests-not-a-list"))
            requests = []

        blocks: list[ContentBlock] = []
        if text := data.get("content"):
            blocks.append(ContentBlock(block_type="text", content={"text": text}))
        if reasoning := data.get("reasoningText"):
            blocks.append(ContentBlock(block_type="thinking", content={"text": reasoning}))

        calls: list[Record] = []
        for request in requests:
            if not isinstance(request, dict):
                extras.append(Unknown(reason="toolRequest:not-an-object"))
                continue
            call_id = request.get("toolCallId") or None
            name = request.get("name") or "unknown"
            arguments = parse_json_args(request.get("arguments", "{}"))
            blocks.append(ContentBlock(
                block_type="tool_use",
                content={"id": call_id, "name": name, "input": arguments},
            ))
            calls.append(ToolCallRec(at=at, name=name, input=arguments, call_id=call_id, parent_key=message_id))

        attributes: dict[str, str] = {}
        if parent := data.get("parentToolCallId"):
            attributes["parent_tool_call_id"] = str(parent)
        response = ResponseRec(at=at, content=blocks, key=message_id, attributes=attributes)
        return [response, *calls, *extras]

    def _tool_result(self, data: dict, at: str | None) -> Record:
        call_id = data.get("toolCallId")
        if not call_id:
            return Unknown(reason="tool.execution_complete:no-toolCallId")
        result = data.get("result")
        if not isinstance(result, dict):
            # Storage keeps an empty result as none, so an empty one is {}.
            result = {"content": result} if result else {}
        return ToolResultRec(
            at=at,
            call_id=str(call_id),
            result=result,
            status="success" if data.get("success", False) else "error",
        )

    def _external_id(self) -> str:
        return f"{NAME}::{self._session_id or self._path.parent.name}"


def _default_session(path: Path) -> Session:
    """The conversation of a file whose header names no session: its directory."""
    return Session(external_id=f"{NAME}::{path.parent.name}")


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Copilot CLI events.jsonl file and yield Conversation objects."""
    return assemble(
        source,
        read=read_jsonl,
        decoder=CopilotDecoder,
        harness=HARNESS,
        default_session=_default_session(source.as_path),
    ).conversations


# Peek hooks: the same decoder as ingest, assembled and projected, so peek and
# ingest classify each event once.
peek_scan, peek_exchanges, peek_tail = make_peek_hooks_from_decoder(
    read_jsonl,
    CopilotDecoder,
    HARNESS,
    default_session_for=_default_session,
    tool_aliases=TOOL_ALIASES,
)
