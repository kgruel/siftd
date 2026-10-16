"""VSCode chat adapter for siftd.

Parses chat session files from VSCode variants (Code, Insiders, Cursor, Windsurf).
Covers Ask, Edit, and Agent modes -- all share the same version 3 session schema.

Supports two storage formats:
- JSON: single file with complete session (legacy default)
- JSONL: patch-based format (chat.useLogSessionStorage); each line is a patch
  operation that builds up the session incrementally

Pipeline: :func:`read_strict` replays a session file into its units (one
session header, then one unit per request); :class:`VSCodeDecoder` turns each
unit into canonical records; the shared assembler
(:mod:`siftd.adapters.assembly`) decides where they attach.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siftd.adapters.assembly import assemble
from siftd.adapters.peek_projection import make_peek_hooks_from_decoder
from siftd.adapters.records import (
    Ignored,
    Malformed,
    PromptRec,
    Record,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
)
from siftd.adapters.sdk import AdapterParseError, build_harness, discover_files
from siftd.domain import ContentBlock, Conversation, Source, Usage
from siftd.safecall import epoch_ms_to_iso

ADAPTER_INTERFACE_VERSION = 1
SUPPORT_TIER = "contrib"
NAME = "vscode"
DEDUP_STRATEGY = "file"
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "json"
HARNESS_DISPLAY_NAME = "VSCode"

HARNESS = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

DEFAULT_LOCATIONS = [
    # workspaceStorage: per-workspace chat sessions.
    # macOS
    "~/Library/Application Support/Code/User/workspaceStorage",
    "~/Library/Application Support/Code - Insiders/User/workspaceStorage",
    "~/Library/Application Support/Cursor/User/workspaceStorage",
    "~/Library/Application Support/Windsurf/User/workspaceStorage",
    # Linux
    "~/.config/Code/User/workspaceStorage",
    "~/.config/Code - Insiders/User/workspaceStorage",
    "~/.config/Cursor/User/workspaceStorage",
    "~/.config/Windsurf/User/workspaceStorage",
    # Windows
    "~/AppData/Roaming/Code/User/workspaceStorage",
    "~/AppData/Roaming/Code - Insiders/User/workspaceStorage",
    "~/AppData/Roaming/Cursor/User/workspaceStorage",
    "~/AppData/Roaming/Windsurf/User/workspaceStorage",
    # globalStorage: no-workspace ("empty window") chat sessions.
    # macOS
    "~/Library/Application Support/Code/User/globalStorage",
    "~/Library/Application Support/Code - Insiders/User/globalStorage",
    "~/Library/Application Support/Cursor/User/globalStorage",
    "~/Library/Application Support/Windsurf/User/globalStorage",
    # Linux
    "~/.config/Code/User/globalStorage",
    "~/.config/Code - Insiders/User/globalStorage",
    "~/.config/Cursor/User/globalStorage",
    "~/.config/Windsurf/User/globalStorage",
    # Windows
    "~/AppData/Roaming/Code/User/globalStorage",
    "~/AppData/Roaming/Code - Insiders/User/globalStorage",
    "~/AppData/Roaming/Cursor/User/globalStorage",
    "~/AppData/Roaming/Windsurf/User/globalStorage",
]

# Shared glob patterns cover both workspaceStorage (per-workspace) and
# globalStorage/emptyWindowChatSessions (no-workspace) layouts.
_GLOB_PATTERNS = [
    "*/chatSessions/*.json",
    "*/chatSessions/*.jsonl",
    "emptyWindowChatSessions/*.json",
    "emptyWindowChatSessions/*.jsonl",
]

# Raw tool name -> canonical tool name, for the names the corpus holds. A name
# not listed here is shown as it was written.
TOOL_ALIASES: dict[str, str] = {
    "copilot_readFile": "file.read",
    "copilot_findTextInFiles": "search.grep",
    "run_in_terminal": "shell.execute",
}

# Response parts whose kind is content kept as the raw part, with no Unknown.
_RAW_CONTENT_KINDS = frozenset({"inlineReference", "mcpServersStarting", "progressMessage", "warning", "command"})

# JSONL patch kinds the replay folds into the session state (see _replay).
_FOLDED_PATCH_KINDS = frozenset({0, 1, 2})

_IGNORED_THINKING_MARKER = "thinking:marker"
_IGNORED_RESTATED_PENDING = "tool-invocation:pending-restated"


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all VSCode chat session files."""
    yield from discover_files(locations, DEFAULT_LOCATIONS, _GLOB_PATTERNS)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.suffix not in (".json", ".jsonl"):
        return False
    parts = path.parts
    return "chatSessions" in parts or "emptyWindowChatSessions" in parts


# --- Units: what the reader yields ---------------------------------------------


@dataclass(frozen=True)
class SessionUnit:
    """The session's header: identity, creation time. Its requests follow as RequestUnits."""

    session: dict


@dataclass(frozen=True)
class RequestUnit:
    """One request of the session: a user turn and the response it drew. ``request`` may be any JSON value."""

    request: Any


@dataclass(frozen=True)
class PatchUnit:
    """A JSONL patch the replay could not apply. ``reason`` names why."""

    reason: str


def read_strict(source: Source) -> Iterator[object]:
    """Yield the units of a session file. Ingest reads with this: a file it cannot read fails."""
    return _read(source, strict=True)


def read_lenient(source: Source) -> Iterator[object]:
    """Yield the units of a session file that may still be being written, for peek.

    A line or file that does not parse is yielded as ``Malformed`` instead of
    failing the read, so a live session can still be shown.
    """
    return _read(source, strict=False)


def _read(source: Source, *, strict: bool) -> Iterator[object]:
    path = source.as_path
    if path.suffix == ".jsonl":
        state, unapplied, malformed = _replay(path, strict=strict)
    else:
        state, malformed = _load_whole(path, strict=strict)
        unapplied = []
    if not isinstance(state, dict):
        if strict:
            raise AdapterParseError(f"VSCode source {path} did not reconstruct a session object")
        yield from malformed
        return
    requests = state.get("requests")
    if not isinstance(requests, list):
        if strict:
            raise AdapterParseError(f"VSCode source {path} is missing a requests array")
        yield from malformed
        return
    yield SessionUnit(session=state)
    for request in requests:
        yield RequestUnit(request=request)
    yield from malformed
    for reason in unapplied:
        yield PatchUnit(reason=reason)


def _load_whole(path: Path, *, strict: bool) -> tuple[dict[str, Any] | None, list[Malformed]]:
    """A whole-file session: the parsed object, or what a lenient read yields instead."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        if strict:
            raise AdapterParseError(f"VSCode source {path} could not be read: {e}") from e
        return None, [Malformed(line=0, error=str(e))]
    except json.JSONDecodeError as e:
        if strict:
            raise AdapterParseError(f"VSCode source {path} contains invalid JSON: {e}") from e
        return None, [Malformed(line=0, error=str(e))]
    if not isinstance(data, dict):
        if strict:
            raise AdapterParseError(f"VSCode source {path} must contain a JSON object")
        return None, [Malformed(line=0, error="not an object")]
    return data, []


def _replay(path: Path, *, strict: bool) -> tuple[dict[str, Any] | None, list[str], list[Malformed]]:
    """Replay a JSONL patch log into the session it builds.

    kind 0 sets the root (the session object), kind 1 sets the value at a key
    path, kind 2 appends a list at a key path. Returns the state, the reasons
    for patches that could not apply, and the lines that did not parse.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        if strict:
            raise AdapterParseError(f"VSCode source {path} could not be read: {e}") from e
        return None, [], [Malformed(line=0, error=str(e))]

    state: dict[str, Any] | None = None
    saw_content = False
    unapplied: list[str] = []
    malformed: list[Malformed] = []
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        saw_content = True
        try:
            patch = json.loads(text)
        except json.JSONDecodeError as e:
            if strict:
                raise AdapterParseError(f"VSCode source {path} contains invalid JSONL: {e}") from e
            malformed.append(Malformed(line=line_no, error=str(e)))
            continue
        if not isinstance(patch, dict):
            if strict:
                raise AdapterParseError(f"VSCode source {path} contains invalid JSONL: line {line_no} is not an object")
            malformed.append(Malformed(line=line_no, error="not an object"))
            continue

        kind = patch.get("kind")
        value = patch.get("v")
        if kind == 0:
            if strict and not isinstance(value, dict):
                raise AdapterParseError(f"VSCode source {path} has an invalid initial session payload")
            state = value if isinstance(value, dict) else None
            continue
        if kind not in (1, 2):
            unapplied.append(f"patch:kind-{kind}")
            continue
        if state is None:
            unapplied.append("patch:before-root")
            continue
        key_path = patch.get("k")
        if not isinstance(key_path, list) or not key_path:
            unapplied.append("patch:no-path")
            continue
        applied = _set_at_path(state, key_path, value) if kind == 1 else _append_at_path(state, key_path, value)
        if not applied:
            unapplied.append("patch:path-missing")

    if strict and state is None and saw_content:
        raise AdapterParseError(f"VSCode source {path} did not reconstruct a session")
    return state, unapplied, malformed


def _set_at_path(obj: dict | list, path: list, value) -> bool:
    """Set a value at a nested key path. False when the path does not exist."""
    if not path:
        return False
    for key in path[:-1]:
        if isinstance(obj, list) and isinstance(key, int):
            if 0 <= key < len(obj):
                obj = obj[key]
            else:
                return False
        elif isinstance(obj, dict):
            child = obj.get(key)
            if child is None:
                return False
            obj = child
        else:
            return False

    final = path[-1]
    if isinstance(obj, list) and isinstance(final, int):
        if 0 <= final < len(obj):
            obj[final] = value
            return True
        return False
    if isinstance(obj, dict):
        obj[final] = value
        return True
    return False


def _append_at_path(obj: dict | list, path: list, value) -> bool:
    """Append items to an array at a nested key path. False when the path or value does not fit."""
    for key in path:
        if isinstance(obj, list) and isinstance(key, int):
            if 0 <= key < len(obj):
                obj = obj[key]
            else:
                return False
        elif isinstance(obj, dict):
            child = obj.get(key)
            if child is None:
                return False
            obj = child
        else:
            return False

    if isinstance(obj, list) and isinstance(value, list):
        obj.extend(value)
        return True
    return False


# --- Decoding ------------------------------------------------------------------


class VSCodeDecoder:
    """Turns each unit of a VSCode session into canonical records.

    A decoder holds only decoding state: the tool call ids it has already given
    a :class:`ToolCallRec`. Every unit kind and every response part kind in the
    corpus is named in the table below; ``tests/adapters/test_vscode.py`` keeps
    the table and the code equal. The reader does the patch-log replay, so
    JSON and JSONL sessions of the same state decode to the same records.

    | Unit or part | Becomes | Why |
    |---|---|---|
    | `session` | Session | The header. `sessionId` names the conversation (`vscode::<sessionId>`, the file stem when absent), `creationDate` starts it (without one, the latest request's timestamp), and the workspace comes from the sibling `workspace.json`. The header is the whole session, so it is also where each tool call's final emission is learned. |
    | `request` | PromptRec, ResponseRec, then the tool records of its parts | One request is one user turn and the answer it drew. The prompt is keyed by `requestId`, the response by `responseId`, and `result.usage` is the response's usage. A request with no answer still gets an empty response. |
    | `part:text` | content (text block) | Answer prose: a part with no `kind` and a string `value`. All 29 answer parts in the corpus. |
    | `part:thinking` | content (thinking block) | Model reasoning with text. |
    | `part:thinking:marker` | Ignored (`thinking:marker`) | A thinking part with an empty value: a metadata marker carrying an id and a title, or an encrypted-content id. 47 in the corpus, none with text. |
    | `part:toolInvocationSerialized` | ToolCallRec, then ToolResultRec per emission that changes its state | A tool call. VS Code re-emits one invocation under its `toolCallId` as it progresses, possibly in a later request. The call is made at its first emission and takes the name and input of the session's last emission. Each emission that settles it is a result; only the session's last emission is final, so a later emission can still settle a call. A pending emission after a settled one reverts the call to pending (a non-final result with status `pending`). A pending restatement of a pending call is Ignored (`tool-invocation:pending-restated`). |
    | `part:inlineReference` | content (raw block) | A file or symbol the answer cites. Kept as the raw part. |
    | `part:mcpServersStarting` | content (raw block) | An MCP server starting for the request. |
    | `part:progressMessage` | content (raw block) | Progress shown while the chat was getting ready. |
    | `part:warning` | content (raw block) | A warning shown in the chat, such as a slow start. |
    | `part:command` | content (raw block) | A command the chat offered as a button. |
    | `part:other` | content (raw block), Unknown (`part:<kind>`) | Any other kind, such as `textEditGroup`: kept raw, and counted, because the corpus does not hold it. |
    | `part:no-kind` | content (unkeyed block), Unknown (`part:no-kind`) | A part with no `kind` and no string `value`. Kept raw, counted. |
    | `part:not-an-object` | Unknown (`part:not-an-object`) | A response entry that is not an object. |
    | `tool-invocation:no-toolCallId` | Unknown (`tool-invocation:no-toolCallId`) | An invocation with no id to pair its result by. |
    | `request:not-an-object` | Unknown (`request:not-an-object`) | A request entry that is not an object. |
    | `patch:0` | folded by the reader | JSONL root: the session object the later patches build on. Not a record. |
    | `patch:1` | folded by the reader | JSONL set at a key path. Not a record. |
    | `patch:2` | folded by the reader | JSONL append to an array at a key path. Not a record. |
    | `patch:unapplied` | Unknown (`patch:<reason>`) | A patch the replay could not apply: an unknown kind, no key path, a path that does not exist, or no root yet. The state it would have changed is missing, so the record is counted. |

    Not stored, and not records: session fields such as `customTitle`,
    `selectedModel` and `inputState`; request fields such as `agent`,
    `modelState`, `followups`, `timeSpentWaiting` and `variableData`; and the
    `result` fields other than `usage.promptTokens` and `usage.completionTokens`
    (`timings`, `metadata`, `details`, `promptTokenDetails`).

    Time. A request's timestamp dates its prompt, its response and the calls
    it made. A completed call's result carries no time of its own, so the
    call keeps the time of the response that made it. Undated events take
    their neighbours' time.
    """

    def __init__(self, source: Source):
        self._path = source.as_path
        # Learned from the header, for the whole session (see _session): each
        # call's final emission, the calls already given a ToolCallRec, and the
        # calls whose latest emission so far settled them.
        self._final: dict[str, dict] = {}
        self._calls: set[str] = set()
        self._settled: set[str] = set()

    def decode(self, unit: object) -> list[Record]:
        if isinstance(unit, SessionUnit):
            return self._session(unit.session)
        if isinstance(unit, RequestUnit):
            if not isinstance(unit.request, dict):
                return [Unknown(reason="request:not-an-object")]
            return self._request(unit.request)
        if isinstance(unit, PatchUnit):
            return [Unknown(reason=unit.reason)]
        return [Unknown(reason="unit:unrecognized")]

    def _session(self, session: dict) -> list[Record]:
        session_id = _ident(session.get("sessionId")) or self._path.stem
        requests = session.get("requests")
        requests = requests if isinstance(requests, list) else []
        # The reader replays the whole session before it yields this header, so
        # the header can see every request. A call's last emission may sit in a
        # later request than the one that made it, so its final state and input
        # are a fact of the whole session.
        self._final = {}
        for request in requests:
            for part in _parts(request):
                call_id = _invocation_id(part)
                if call_id is not None:
                    self._final[call_id] = part
        # Without a creation date the session starts at its latest request, as
        # it always has. The assembler would take the earliest instead.
        started = epoch_ms_to_iso(session.get("creationDate")) or epoch_ms_to_iso(_latest_timestamp(requests))
        return [
            Session(
                external_id=f"{NAME}::{session_id}",
                started_at=started,
                workspace_path=_resolve_workspace(self._path),
            )
        ]

    def _request(self, request: dict) -> list[Record]:
        at = epoch_ms_to_iso(request.get("timestamp"))
        request_id = _ident(request.get("requestId"))
        response_id = _ident(request.get("responseId"))

        message = request.get("message", "")
        if isinstance(message, dict):
            message = message.get("text", str(message))

        out: list[Record] = [
            PromptRec(at=at, content=[ContentBlock(block_type="text", content={"text": message})],
                      external_id=request_id, key=request_id)
        ]

        content: list[ContentBlock] = []
        tool_parts: list[dict] = []
        for part in _parts(request):
            if not isinstance(part, dict):
                out.append(Unknown(reason="part:not-an-object"))
                continue
            kind = part.get("kind") if isinstance(part.get("kind"), str) else ""
            if _is_text_part(part):
                content.append(ContentBlock(block_type="text", content={"text": part["value"]}))
            elif kind == "toolInvocationSerialized":
                tool_parts.append(part)
            elif kind == "thinking":
                value = part.get("value")
                if isinstance(value, str) and value:
                    content.append(ContentBlock(block_type="thinking", content={"text": value}))
                elif value in ("", []):
                    out.append(Ignored(reason=_IGNORED_THINKING_MARKER))
                else:
                    out.append(Unknown(reason="thinking:value-type"))
            elif kind in _RAW_CONTENT_KINDS:
                content.append(ContentBlock(block_type=kind, content=part))
            elif kind:
                content.append(ContentBlock(block_type=kind, content=part))
                out.append(Unknown(reason=f"part:{kind}"))
            else:
                content.append(ContentBlock(block_type="unkeyed", content=part))
                out.append(Unknown(reason="part:no-kind"))

        out.append(
            ResponseRec(
                at=at,
                content=content,
                external_id=response_id,
                key=response_id,
                model=request.get("modelId"),
                usage=_usage(request.get("result")),
            )
        )
        out.extend(self._tool_records(tool_parts, at=at, parent_key=response_id))
        return out

    def _tool_records(self, parts: list[dict], *, at: str | None, parent_key: str | None) -> list[Record]:
        """The records of one request's tool invocations.

        An invocation is emitted again as it progresses, under one toolCallId. The
        call is made at its first emission, as a ToolCallRec naming the session's
        last emission (see _session). Each emission that settles the call is a
        ToolResultRec, final only when it is the session's last emission, so a
        call stays open for a settling emission in a later request. A pending
        emission after a settled one reverts the call to pending, as a non-final
        result with status ``pending``. The ledger counts each emission.
        """
        out: list[Record] = []
        for part in parts:
            call_id = _invocation_id(part)
            if call_id is None:
                out.append(Unknown(reason="tool-invocation:no-toolCallId"))
                continue
            # A request decoded without its session header (no SessionUnit before it)
            # has no later emissions to wait for, so each one counts as final.
            final = self._final.get(call_id, part)
            new_call = call_id not in self._calls
            if new_call:
                self._calls.add(call_id)
                out.append(
                    ToolCallRec(at=at, name=_tool_name(final), input=_tool_input(final),
                                call_id=call_id, parent_key=parent_key)
                )
            state = _settled_state(part)
            if state is not None:
                status, result = state
                self._settled.add(call_id)
                out.append(ToolResultRec(at=None, call_id=call_id, result=result, status=status,
                                         final=final is part))
            elif call_id in self._settled:
                # The call was settled and this emission reverts it. An empty result
                # is what a pending call stores: storage keeps neither {} nor None.
                self._settled.discard(call_id)
                out.append(ToolResultRec(at=None, call_id=call_id, result={}, status="pending", final=False))
            elif not new_call:
                out.append(Ignored(reason=_IGNORED_RESTATED_PENDING))
        return out


def _ident(value: object) -> str | None:
    """A source identifier, or None when absent or empty."""
    return value if isinstance(value, str) and value else None


def _parts(request: Any) -> list:
    response = request.get("response") if isinstance(request, dict) else None
    return response if isinstance(response, list) else []


def _invocation_id(part: Any) -> str | None:
    """The toolCallId of a tool invocation part, or None for any other part or a missing id."""
    if isinstance(part, dict) and part.get("kind") == "toolInvocationSerialized":
        return _ident(part.get("toolCallId"))
    return None


def _latest_timestamp(requests: list) -> float | None:
    """The latest request timestamp, in epoch milliseconds."""
    stamps = []
    for request in requests:
        stamp = request.get("timestamp") if isinstance(request, dict) else None
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            stamps.append(stamp)
    return max(stamps, default=None)


def _is_text_part(part: dict) -> bool:
    """Answer prose: a response part with no kind and a string value."""
    return "kind" not in part and isinstance(part.get("value"), str)


def _tool_name(part: dict) -> str:
    return part.get("toolId") or "unknown"


def _invocation_text(part: dict) -> str:
    message = part.get("invocationMessage")
    if isinstance(message, dict):
        message = message.get("value")
    return message if isinstance(message, str) else ""


def _tool_input(part: dict) -> dict:
    """Recover the call's input from what VS Code stores for it.

    VS Code keeps no argument object. The invocation text is usually present; it
    is stored as "description", the key the SDK's peek hint reads first. Without
    that key the peek view shows no input at all. run_in_terminal also stores the
    command under toolSpecificData, and some tools echo their input in
    resultDetails.
    """
    tool_input: dict = {}
    text = _invocation_text(part)
    if text:
        tool_input["description"] = text
    specific = part.get("toolSpecificData")
    if isinstance(specific, dict) and isinstance(specific.get("commandLine"), dict):
        original = specific["commandLine"].get("original")
        if isinstance(original, str) and original:
            tool_input["command"] = original
    details = part.get("resultDetails")
    if isinstance(details, dict) and "input" in details:
        tool_input["input"] = details["input"]
    return tool_input


def _settled_state(part: dict) -> tuple[str, dict] | None:
    """An emission's status and result, or None while the invocation is still pending.

    A settled invocation with no result details has an empty result: the
    completion is the fact, not a payload.
    """
    details = part.get("resultDetails")
    result = {"resultDetails": details} if details is not None else {}
    if isinstance(details, dict) and details.get("isError") is True:
        return "error", result
    if part.get("isComplete") is True or details is not None:
        return "success", result
    return None


def _usage(result: Any) -> Usage | None:
    """The request's token usage: a snapshot of the whole request, from ``result.usage``."""
    if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
        return None
    usage = result["usage"]
    prompt = usage.get("promptTokens")
    completion = usage.get("completionTokens")
    if prompt is None and completion is None:
        return None
    return Usage(input_tokens=_count(prompt), output_tokens=_count(completion))


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _resolve_workspace(session_path: Path) -> str | None:
    """Read workspace.json from the hash directory to get workspace path.

    Session files live at: workspaceStorage/<hash>/chatSessions/<session>.json
    workspace.json lives at: workspaceStorage/<hash>/workspace.json
    """
    hash_dir = session_path.parent.parent
    workspace_json = hash_dir / "workspace.json"

    try:
        data = json.loads(workspace_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    folder = data.get("folder", "")
    if folder.startswith("file://"):
        return folder[len("file://"):]
    return folder or None


def _default_session(path: Path) -> Session:
    """The conversation of a file that names no session of its own."""
    return Session(external_id=f"{NAME}::{path.stem}")


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a VSCode chat session file and yield Conversation objects."""
    return assemble(
        source,
        read=read_strict,
        decoder=VSCodeDecoder,
        harness=HARNESS,
        default_session=_default_session(source.as_path),
    ).conversations


PEEK_GLOB_PATTERNS = _GLOB_PATTERNS

# Peek hooks: the same reader and decoder as ingest, assembled and projected, so
# peek and ingest classify each record once. Peek reads leniently: a session
# being written may end in a partial line.
peek_scan, peek_exchanges, peek_tail = make_peek_hooks_from_decoder(
    read_lenient,
    VSCodeDecoder,
    HARNESS,
    default_session_for=_default_session,
    tool_aliases=TOOL_ALIASES,
)
