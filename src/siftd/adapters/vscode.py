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
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from siftd.adapters.sdk import build_harness, discover_files
from siftd.domain import (
    ContentBlock,
    Conversation,
    Prompt,
    Response,
    Source,
    ToolCall,
)

log = logging.getLogger(__name__)

ADAPTER_INTERFACE_VERSION = 1
NAME = "vscode"
DEDUP_STRATEGY = "file"
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "json"
HARNESS_DISPLAY_NAME = "VSCode"

DEFAULT_LOCATIONS = [
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
]


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all VSCode chat session files."""
    yield from discover_files(
        locations,
        DEFAULT_LOCATIONS,
        ["*/chatSessions/*.json", "*/chatSessions/*.jsonl"],
    )


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.suffix not in (".json", ".jsonl"):
        return False
    return "chatSessions" in path.parts


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a VSCode chat session file and yield Conversation objects."""
    path = Path(source.location)

    try:
        if path.suffix == ".jsonl":
            data = _replay_jsonl(path)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("vscode: failed to parse %s: %s", path, e)
        return

    if data is None:
        return

    yield from _session_to_conversation(data, path)


def _session_to_conversation(data: dict, path: Path) -> Iterable[Conversation]:
    """Convert a reconstructed session dict into a Conversation."""
    requests = data.get("requests", [])
    if not requests:
        return

    session_id = data.get("sessionId", path.stem)
    creation_date = data.get("creationDate")
    workspace_path = _resolve_workspace(path)

    started_at = _ms_to_iso(creation_date) if creation_date else None
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

    if not conversation.prompts:
        return

    yield conversation


def _replay_jsonl(path: Path) -> dict | None:
    """Reconstruct a session from JSONL patch operations.

    VSCode's JSONL format uses three patch kinds:
    - kind=0: Initial state (full session object in 'v')
    - kind=1: Set value at key path 'k' (replace)
    - kind=2: Append items to array at key path 'k' (extend)
    """
    state = None

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            patch = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = patch.get("kind")
        value = patch.get("v")

        if kind == 0:
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

    return state


def _set_at_path(obj: dict | list, path: list, value) -> None:
    """Set a value at a nested key path."""
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
    timestamp = _ms_to_iso(timestamp_ms) if timestamp_ms else None

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


def _ms_to_iso(ms: int | float) -> str:
    """Convert Unix milliseconds to ISO 8601 timestamp."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _last_timestamp(requests: list[dict]) -> str | None:
    """Extract the latest timestamp from requests."""
    latest = None
    for req in requests:
        ts = req.get("timestamp")
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return _ms_to_iso(latest) if latest is not None else None
