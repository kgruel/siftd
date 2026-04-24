"""VSCode chat adapter for siftd.

Parses chat session files from VSCode variants (Code, Insiders, Cursor, Windsurf).
Covers Ask, Edit, and Agent modes -- all share the same version 3 session schema.

Supports two storage formats:
- JSON: single file with complete session (legacy default)
- JSONL: patch-based format (chat.useLogSessionStorage); each line is a patch
  operation that builds up the session incrementally
"""

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from siftd.adapters.sdk import (
    AdapterParseError,
    NormalizedRecord,
    build_harness,
    discover_files,
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
from siftd.safecall import epoch_ms_to_iso

log = logging.getLogger(__name__)

ADAPTER_INTERFACE_VERSION = 1
NAME = "vscode"
DEDUP_STRATEGY = "file"
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "json"
HARNESS_DISPLAY_NAME = "VSCode"

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


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a VSCode chat session file and yield Conversation objects."""
    path = Path(source.location)

    if path.suffix == ".jsonl":
        data = _replay_jsonl_strict(path)
    else:
        data = _load_json_strict(path)

    yield from _session_to_conversation(data, path)


def _session_to_conversation(data: dict, path: Path) -> Iterable[Conversation]:
    """Convert a reconstructed session dict into a Conversation."""
    requests = data.get("requests")
    if not isinstance(requests, list):
        raise AdapterParseError(
            f"VSCode source {path} is missing a requests array"
        )
    if not requests:
        return

    session_id = data.get("sessionId", path.stem)
    creation_date = data.get("creationDate")
    workspace_path = _resolve_workspace(path)

    started_at = epoch_ms_to_iso(creation_date)
    ended_at = _last_timestamp(requests)

    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    conversation = Conversation(
        external_id=f"{NAME}::{session_id}",
        harness=harness,
        started_at=started_at or ended_at or datetime.now(UTC).isoformat(),
        ended_at=ended_at,
        workspace_path=workspace_path,
    )

    for request in requests:
        prompt = _parse_request(request)
        if prompt:
            conversation.prompts.append(prompt)

    yield from yield_conversation(conversation)


def _replay_jsonl(path: Path, *, strict: bool = False) -> dict | None:
    """Reconstruct a session from JSONL patch operations.

    VSCode's JSONL format uses three patch kinds:
    - kind=0: Initial state (full session object in 'v')
    - kind=1: Set value at key path 'k' (replace)
    - kind=2: Append items to array at key path 'k' (extend)
    """
    # Lenient path preserved for peek; ingest uses strict=True.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        if strict:
            raise AdapterParseError(
                f"VSCode source {path} could not be read: {e}"
            ) from e
        return None

    state = None
    saw_content = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        saw_content = True

        try:
            patch = json.loads(line)
        except json.JSONDecodeError as e:
            if strict:
                raise AdapterParseError(
                    f"VSCode source {path} contains invalid JSONL: {e}"
                ) from e
            continue

        kind = patch.get("kind")
        value = patch.get("v")

        if kind == 0:
            if strict and not isinstance(value, dict):
                raise AdapterParseError(
                    f"VSCode source {path} has an invalid initial session payload"
                )
            state = value
            continue

        if state is None:
            continue

        key_path = patch.get("k", [])
        if not key_path:
            continue

        if kind == 1:
            _set_at_path(state, key_path, value)
        elif kind == 2:
            _append_at_path(state, key_path, value)

    if strict and state is None and saw_content:
        raise AdapterParseError(
            f"VSCode source {path} did not reconstruct a session"
        )

    return state


def _replay_jsonl_strict(path: Path) -> dict:
    """Strict JSONL replay for ingest paths that require a session object."""
    data = _replay_jsonl(path, strict=True)
    if not isinstance(data, dict):
        raise AdapterParseError(
            f"VSCode source {path} did not reconstruct a session object"
        )
    return data


def _set_at_path(obj: dict | list, path: list, value) -> None:
    """Set a value at a nested key path."""
    if not path:
        return
    for key in path[:-1]:
        if isinstance(obj, list) and isinstance(key, int):
            if 0 <= key < len(obj):
                obj = obj[key]
            else:
                return
        elif isinstance(obj, dict):
            child = obj.get(key)
            if child is None:
                return
            obj = child
        else:
            return

    final = path[-1]
    if isinstance(obj, list) and isinstance(final, int):
        if 0 <= final < len(obj):
            obj[final] = value
    elif isinstance(obj, dict):
        obj[final] = value


def _append_at_path(obj: dict | list, path: list, value) -> None:
    """Append items to an array at a nested key path."""
    for key in path:
        if isinstance(obj, list) and isinstance(key, int):
            if 0 <= key < len(obj):
                obj = obj[key]
            else:
                return
        elif isinstance(obj, dict):
            child = obj.get(key)
            if child is None:
                return
            obj = child
        else:
            return

    if isinstance(obj, list) and isinstance(value, list):
        obj.extend(value)


def _parse_request(request: dict) -> Prompt | None:
    """Parse a single request object into a Prompt with Response."""
    message = request.get("message", "")
    if isinstance(message, dict):
        message = message.get("text", str(message))

    timestamp_ms = request.get("timestamp")
    timestamp = epoch_ms_to_iso(timestamp_ms)

    prompt = Prompt(
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        external_id=request.get("requestId"),
        content=[ContentBlock(block_type="text", content={"text": message})],
    )

    response_parts = request.get("response") or []
    content_blocks: list[ContentBlock] = []
    tool_calls: list[ToolCall] = []

    for part in response_parts:
        kind = part.get("kind", "")

        if kind == "markdownContent":
            value = part.get("content", {}).get("value", "")
            content_blocks.append(ContentBlock(block_type="text", content={"text": value}))

        elif kind == "toolInvocationSerialized":
            tool_calls.append(ToolCall(
                tool_name=part.get("toolName") or part.get("name", "unknown"),
                input=part.get("input", {}),
                result=part.get("result"),
                status="success" if part.get("result") is not None else "pending",
                external_id=part.get("toolCallId"),
            ))

        elif kind == "textEditGroup":
            content_blocks.append(ContentBlock(block_type="text_edit", content=part))

        elif kind:
            content_blocks.append(ContentBlock(block_type=kind, content=part))

    response = Response(
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        model=request.get("modelId"),
        external_id=request.get("responseId"),
        content=content_blocks,
        tool_calls=tool_calls,
    )
    prompt.responses.append(response)

    return prompt


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



def _last_timestamp(requests: list[dict]) -> str | None:
    """Extract the latest timestamp from requests."""
    latest = None
    for req in requests:
        ts = req.get("timestamp")
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return epoch_ms_to_iso(latest)


# =============================================================================
# Record normalization — enables SDK-derived peek support
# =============================================================================


def _load_session(path: Path) -> dict | None:
    """Load a VSCode session from JSON or JSONL format."""
    from siftd.safecall import load_json

    if path.suffix == ".jsonl":
        return _replay_jsonl(path)
    return load_json(path, context="vscode")


def _load_json_strict(path: Path) -> dict:
    """Load and validate a VSCode JSON session file for ingest."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise AdapterParseError(
            f"VSCode source {path} could not be read: {e}"
        ) from e
    except json.JSONDecodeError as e:
        raise AdapterParseError(
            f"VSCode source {path} contains invalid JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise AdapterParseError(
            f"VSCode source {path} must contain a JSON object"
        )

    return data


def iter_vscode_records(path: Path) -> Iterator[dict]:
    """Iterate synthetic records from a VSCode session file.

    VSCode stores each session as a single JSON object with a requests array.
    This iterator yields synthetic records: one metadata record, then for each
    request, a user record followed by an assistant record.
    """
    data = _load_session(path)
    if not data:
        return

    creation_date = data.get("creationDate")
    yield {
        "_kind": "metadata",
        "sessionId": data.get("sessionId", path.stem),
        "creationDate": creation_date,
        "_path": str(path),
    }

    for request in data.get("requests", []):
        ts = request.get("timestamp")
        yield {"_kind": "user", **request, "_ts": epoch_ms_to_iso(ts)}
        yield {"_kind": "assistant", **request, "_ts": epoch_ms_to_iso(ts)}


def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map a VSCode synthetic record to NormalizedRecord.

    Synthetic record kinds (produced by iter_vscode_records):
        "_kind": "metadata"   → metadata (sessionId, workspace via path)
        "_kind": "user"       → user (prompt text from request.message)
        "_kind": "assistant"  → assistant (response parts from request.response)
    """
    kind = raw.get("_kind")
    ts = raw.get("_ts")

    if kind == "metadata":
        creation_date = raw.get("creationDate")
        return NormalizedRecord(
            kind="metadata",
            timestamp=epoch_ms_to_iso(creation_date),
            session_id=raw.get("sessionId"),
            workspace_path=_resolve_workspace(Path(raw["_path"])) if raw.get("_path") else None,
        )

    if kind == "user":
        message = raw.get("message", "")
        if isinstance(message, dict):
            message = message.get("text", str(message))
        content_blocks = [{"type": "text", "text": message}] if message else []
        return NormalizedRecord(
            kind="user",
            timestamp=ts,
            content_blocks=content_blocks,
        )

    if kind == "assistant":
        content_blocks: list[dict] = []
        for part in raw.get("response") or []:
            part_kind = part.get("kind", "")
            if part_kind == "markdownContent":
                value = part.get("content", {}).get("value", "")
                if value:
                    content_blocks.append({"type": "text", "text": value})
            elif part_kind == "toolInvocationSerialized":
                content_blocks.append({
                    "type": "tool_use",
                    "name": part.get("toolName") or part.get("name", "unknown"),
                    "input": part.get("input", {}),
                })
        return NormalizedRecord(
            kind="assistant",
            timestamp=ts,
            model=raw.get("modelId"),
            content_blocks=content_blocks,
        )

    return None


PEEK_GLOB_PATTERNS = _GLOB_PATTERNS

# Peek hooks — derived from normalizer with custom JSON iterator
peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    normalize_record,
    record_iterator=iter_vscode_records,
)
