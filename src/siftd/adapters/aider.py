"""Aider adapter for siftd.

Pure parser: reads .aider.chat.history.md files and yields Conversation
domain objects. Each session (delimited by ``# aider chat started at``)
becomes a separate Conversation.

Discovery is opt-in via ``--path``. DEFAULT_LOCATIONS covers ``~/.aider``.
Chat history files are scattered across project directories; the user
supplies scan roots explicitly.

Aider is the one supported log format that timestamps in local wall time
with no offset, so this is the one adapter that has to resolve a zone
rather than pass a timestamp through.
"""

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters.sdk import build_harness, yield_conversation
from siftd.dateparse import local_to_utc
from siftd.domain import (
    ContentBlock,
    Conversation,
    Prompt,
    Response,
    Source,
)

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
SUPPORT_TIER = "frozen"
NAME = "aider"
DEFAULT_LOCATIONS = ["~/.aider"]
DEDUP_STRATEGY = "file"  # each history file is a distinct source

# Harness metadata
HARNESS_SOURCE = "multi"  # aider supports multiple LLM providers
HARNESS_LOG_FORMAT = "markdown"
HARNESS_DISPLAY_NAME = "Aider"

# No structured tool names in aider's markdown format
TOOL_ALIASES: dict[str, str] = {}

# Regex for session headers: # aider chat started at 2025-07-15 14:32:01
_SESSION_RE = re.compile(r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Regex for cost lines: > Tokens: 4.5k sent, 1.2k received. Cost: $0.02
_COST_RE = re.compile(
    r"Tokens:\s*([\d.,]+[kKmM]?)\s*sent,\s*([\d.,]+[kKmM]?)\s*received\."
    r"(?:\s*Cost:\s*\$([\d.,]+))?"
)


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for aider files.

    Args:
        locations: Directories to scan. Falls back to DEFAULT_LOCATIONS.
                   When provided via ``--path``, scans for chat history files.
    """
    for location in (locations or DEFAULT_LOCATIONS):
        base = Path(location).expanduser()
        if not base.exists():
            continue
        # analytics.jsonl skipped — ingestion deferred until schema is documented upstream
        for md_file in base.glob("**/.aider.chat.history.md"):
            yield Source(kind="file", location=md_file)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    return Path(source.location).name == ".aider.chat.history.md"


def parse(source: Source) -> Iterable[Conversation]:
    """Parse an aider chat history file and yield one Conversation per session."""
    path = Path(source.location)

    if path.name == ".aider.chat.history.md":
        yield from _parse_chat_history(path)


def _parse_chat_history(path: Path) -> Iterable[Conversation]:
    """Parse a .aider.chat.history.md file into Conversations.

    Each ``# aider chat started at`` header starts a new session/conversation.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    workspace_path = str(path.parent)
    path_hash = hashlib.sha256(str(path).encode()).hexdigest()[:12]

    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    # Split into sessions by header line
    sessions = _split_sessions(text)

    for timestamp, body in sessions:
        # The header records local wall time with no offset, but `started_at`
        # is a UTC column — resolve it against the host zone at parse time
        # (see `local_to_utc`). `external_id` deliberately keeps the raw
        # header string: it is the dedup key, and re-keying it would
        # duplicate every already-ingested aider conversation.
        external_id = f"{NAME}::{path_hash}::{timestamp}"
        started_at = local_to_utc(timestamp)

        conversation = Conversation(
            external_id=external_id,
            harness=harness,
            started_at=started_at,
            workspace_path=workspace_path,
        )

        _parse_session_body(body, conversation)

        yield from yield_conversation(conversation)


def _split_sessions(text: str) -> list[tuple[str, str]]:
    """Split file text into (timestamp, body) tuples per session."""
    sessions: list[tuple[str, str]] = []
    lines = text.split("\n")

    current_ts: str | None = None
    current_lines: list[str] = []

    for line in lines:
        match = _SESSION_RE.match(line)
        if match:
            # Flush previous session
            if current_ts is not None:
                sessions.append((current_ts, "\n".join(current_lines)))
            current_ts = match.group(1)
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last session
    if current_ts is not None:
        sessions.append((current_ts, "\n".join(current_lines)))

    return sessions


def _parse_session_body(body: str, conversation: Conversation) -> None:
    """Parse session body into Prompts and Responses on the conversation.

    Message types by line prefix:
    - ``#### `` — user message
    - ``> `` — tool/system output
    - everything else — assistant response
    """
    lines = body.split("\n")

    current_prompt: Prompt | None = None
    current_role: str | None = None  # "user", "assistant", "tool"
    buffer: list[str] = []

    def flush():
        nonlocal current_prompt, current_role, buffer
        if not buffer or current_role is None:
            buffer = []
            current_role = None
            return

        text = "\n".join(buffer).strip()
        if not text:
            buffer = []
            current_role = None
            return

        if current_role == "user":
            current_prompt = Prompt(
                timestamp=conversation.started_at,
                content=[ContentBlock(block_type="text", content={"text": text})],
            )
            conversation.prompts.append(current_prompt)

        elif current_role == "assistant":
            if current_prompt is not None:
                response = Response(timestamp=conversation.started_at)
                response.content.append(
                    ContentBlock(block_type="text", content={"text": text})
                )
                current_prompt.responses.append(response)

        elif current_role == "tool":
            if current_prompt is not None:
                # Try to extract cost info from tool output
                attributes = _extract_cost_attributes(text)
                if current_prompt.responses:
                    # Attach cost attributes to the last response
                    last_resp = current_prompt.responses[-1]
                    last_resp.attributes.update(attributes)
                    # Also store tool output as a content block
                    last_resp.content.append(
                        ContentBlock(
                            block_type="tool_output",
                            content={"text": text},
                        )
                    )
                else:
                    # Tool output before any assistant response — create one
                    response = Response(
                        timestamp=conversation.started_at,
                        attributes=attributes,
                    )
                    response.content.append(
                        ContentBlock(
                            block_type="tool_output",
                            content={"text": text},
                        )
                    )
                    current_prompt.responses.append(response)

        buffer = []
        current_role = None

    for line in lines:
        if line.startswith("#### "):
            # User message line
            stripped = line[5:]  # remove "#### " prefix
            if current_role == "user":
                # Continuation of multi-line user message
                buffer.append(stripped)
            else:
                flush()
                current_role = "user"
                buffer = [stripped]

        elif line.startswith("> "):
            # Tool/system output
            stripped = line[2:]  # remove "> " prefix
            if current_role == "tool":
                buffer.append(stripped)
            else:
                flush()
                current_role = "tool"
                buffer = [stripped]

        else:
            # Assistant response (or blank line)
            if current_role == "assistant":
                buffer.append(line)
            else:
                flush()
                current_role = "assistant"
                buffer = [line]

    flush()


def _extract_cost_attributes(tool_text: str) -> dict[str, str]:
    """Extract approximate cost/token info from tool output text.

    Parses lines like: Tokens: 4.5k sent, 1.2k received. Cost: $0.02
    Returns attributes dict (may be empty).
    """
    attributes: dict[str, str] = {}
    match = _COST_RE.search(tool_text)
    if match:
        sent_str, recv_str, cost_str = match.groups()
        sent = _parse_token_count(sent_str)
        recv = _parse_token_count(recv_str)
        if sent is not None:
            attributes["approx_input_tokens"] = str(sent)
        if recv is not None:
            attributes["approx_output_tokens"] = str(recv)
        if cost_str:
            attributes["approx_cost"] = cost_str
    return attributes


def _parse_token_count(s: str) -> int | None:
    """Parse a token count string like '4.5k', '1.2k', '500' into an int."""
    s = s.strip().replace(",", "")
    multiplier = 1
    if s.lower().endswith("k"):
        multiplier = 1000
        s = s[:-1]
    elif s.lower().endswith("m"):
        multiplier = 1_000_000
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return None
