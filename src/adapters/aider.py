"""Aider adapter for tbd-v2.

Pure parser: reads Aider analytics JSONL logs and chat history markdown files,
yields Conversation domain objects. No storage coupling.

Aider produces two relevant file types:
- Analytics JSONL (opt-in via --analytics-log): structured token/cost data per message
- Chat history markdown (.aider.chat.history.md): conversation content, always written

The analytics log is the primary structured source. Chat history provides content
when analytics data is unavailable.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from domain import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    Usage,
)
from domain.source import Source

# Adapter self-description
NAME = "aider"
DEFAULT_LOCATIONS = [
    "~/.aider/analytics.jsonl",  # common analytics log location
]
CHAT_HISTORY_GLOBS = [
    "**/.aider.chat.history.md",  # project-local chat histories
]
SOURCE_KINDS = ["file"]
DEDUP_STRATEGY = "file"  # one conversation per analytics log or chat history file

# Harness metadata
HARNESS_SOURCE = "multi"  # Aider supports multiple providers
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Aider"

# Session event types in analytics log
_SESSION_START_EVENTS = {"cli session", "gui session", "launched"}
_MESSAGE_EVENT = "message_send"
_EXIT_EVENT = "exit"


def discover() -> Iterable[Source]:
    """Yield Source objects for Aider analytics logs and chat history files.

    Searches:
    1. Default analytics log locations
    2. Home directory for .aider.chat.history.md files (up to 3 levels deep)
    """
    # Analytics JSONL files
    for location in DEFAULT_LOCATIONS:
        path = Path(location).expanduser()
        if path.exists() and path.is_file():
            yield Source(kind="file", location=path, metadata={"aider_type": "analytics"})

    # Chat history markdown files in home directory
    home = Path.home()
    for glob_pattern in CHAT_HISTORY_GLOBS:
        for md_file in home.glob(glob_pattern):
            yield Source(kind="file", location=md_file, metadata={"aider_type": "chat_history"})


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)

    # Analytics JSONL
    if path.suffix == ".jsonl" and "aider" in str(path).lower():
        return True

    # Chat history markdown
    if path.name == ".aider.chat.history.md":
        return True

    return False


def parse(source: Source) -> Iterable[Conversation]:
    """Parse an Aider source file and yield Conversation objects."""
    path = Path(source.location)
    aider_type = source.metadata.get("aider_type", "")

    if aider_type == "analytics" or path.suffix == ".jsonl":
        yield from _parse_analytics(path)
    elif aider_type == "chat_history" or path.name == ".aider.chat.history.md":
        yield from _parse_chat_history(path)


def _parse_analytics(path: Path) -> Iterable[Conversation]:
    """Parse an Aider analytics JSONL file into sessions.

    Groups events by session boundaries (cli session / gui session / launched events)
    and creates one Conversation per session.
    """
    records = _load_jsonl(path)
    if not records:
        return

    # Group records into sessions
    sessions = _split_into_sessions(records)

    for session_records in sessions:
        conversation = _session_to_conversation(session_records, path)
        if conversation is not None:
            yield conversation


def _parse_chat_history(path: Path) -> Iterable[Conversation]:
    """Parse an Aider chat history markdown file.

    Format:
    - Lines starting with '# aider chat started at YYYY-MM-DD HH:MM:SS' start new sessions
    - Lines starting with '#### ' are user messages
    - Lines starting with '> ' are tool/function responses
    - All other lines are assistant messages
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    # Split into sessions by header
    session_pattern = re.compile(r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", re.MULTILINE)
    splits = list(session_pattern.finditer(text))

    if not splits:
        return

    harness = Harness(
        name=NAME,
        source=HARNESS_SOURCE,
        log_format="markdown",
        display_name=HARNESS_DISPLAY_NAME,
    )

    for i, match in enumerate(splits):
        start_pos = match.end()
        end_pos = splits[i + 1].start() if i + 1 < len(splits) else len(text)
        session_text = text[start_pos:end_pos].strip()

        if not session_text:
            continue

        timestamp_str = match.group(1)
        external_id = f"{NAME}::{path.stem}::{timestamp_str}"

        conversation = Conversation(
            external_id=external_id,
            harness=harness,
            started_at=timestamp_str,
            workspace_path=str(path.parent),
        )

        _parse_markdown_messages(session_text, conversation, external_id)

        if conversation.prompts:
            yield conversation


def _parse_markdown_messages(text: str, conversation: Conversation, base_id: str) -> None:
    """Parse user/assistant message blocks from a chat history session."""
    lines = text.split("\n")
    current_role = None
    current_lines: list[str] = []
    prompt_idx = 0
    current_prompt: Prompt | None = None

    def flush():
        nonlocal current_prompt, prompt_idx
        if not current_lines:
            return
        content_text = "\n".join(current_lines).strip()
        if not content_text:
            return

        if current_role == "user":
            prompt_idx += 1
            current_prompt = Prompt(
                timestamp="",
                external_id=f"{base_id}::p{prompt_idx}",
                content=[ContentBlock(block_type="text", content={"text": content_text})],
            )
            conversation.prompts.append(current_prompt)
        elif current_role == "assistant" and current_prompt is not None:
            response = Response(
                timestamp="",
                external_id=f"{base_id}::p{prompt_idx}::r",
                content=[ContentBlock(block_type="text", content={"text": content_text})],
            )
            current_prompt.responses.append(response)

    for line in lines:
        if line.startswith("#### "):
            flush()
            current_role = "user"
            current_lines = [line[5:]]  # strip '#### ' prefix
        elif current_role == "user" and not line.startswith("#### "):
            # First non-user line after user message switches to assistant
            if line.strip() or current_lines:
                flush()
                current_role = "assistant"
                current_lines = [line]
        elif current_role == "assistant":
            current_lines.append(line)
        else:
            # Before any user message, skip
            continue

    flush()


def _load_jsonl(path: Path) -> list[dict]:
    """Load a JSONL file, returning parsed records."""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, UnicodeDecodeError):
        pass
    return records


def _split_into_sessions(records: list[dict]) -> list[list[dict]]:
    """Split analytics records into session groups.

    A new session starts when a session-start event is encountered.
    Records before the first session-start are grouped as a standalone session.
    """
    sessions: list[list[dict]] = []
    current: list[dict] = []

    for record in records:
        event = record.get("event", "")
        if event in _SESSION_START_EVENTS and current:
            sessions.append(current)
            current = []
        current.append(record)

    if current:
        sessions.append(current)

    return sessions


def _session_to_conversation(records: list[dict], source_path: Path) -> Conversation | None:
    """Convert a list of session records into a Conversation."""
    if not records:
        return None

    # Find session metadata from first record
    first = records[0]
    first_props = first.get("properties", {})
    first_time = first.get("time")

    # Determine timestamps
    started_at = _unix_to_iso(first_time) if first_time else ""
    last_time = max((r.get("time", 0) for r in records), default=0)
    ended_at = _unix_to_iso(last_time) if last_time else None

    # Determine model
    default_model = first_props.get("main_model")

    # Build external_id from source path + session start time
    external_id = f"{NAME}::{source_path.stem}::{first_time or 'unknown'}"

    harness = Harness(
        name=NAME,
        source=HARNESS_SOURCE,
        log_format=HARNESS_LOG_FORMAT,
        display_name=HARNESS_DISPLAY_NAME,
    )

    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at,
        ended_at=ended_at,
        default_model=default_model,
    )

    # Process message_send events as prompt/response pairs
    prompt_idx = 0
    for record in records:
        if record.get("event") != _MESSAGE_EVENT:
            continue

        props = record.get("properties", {})
        timestamp = _unix_to_iso(record.get("time")) if record.get("time") else ""

        prompt_idx += 1
        prompt = Prompt(
            timestamp=timestamp,
            external_id=f"{external_id}::msg{prompt_idx}",
        )

        # Build usage from token data
        input_tokens = props.get("prompt_tokens")
        output_tokens = props.get("completion_tokens")
        usage = None
        if input_tokens is not None or output_tokens is not None:
            usage = Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        # Build response with model and cost info
        model = props.get("main_model", default_model)
        response = Response(
            timestamp=timestamp,
            usage=usage,
            model=model,
            external_id=f"{external_id}::msg{prompt_idx}::r",
        )

        # Store cost and edit_format as attributes
        cost = props.get("cost")
        if cost is not None:
            response.attributes["cost"] = str(cost)
        total_cost = props.get("total_cost")
        if total_cost is not None:
            response.attributes["total_cost"] = str(total_cost)
        edit_format = props.get("edit_format")
        if edit_format:
            response.attributes["edit_format"] = edit_format

        prompt.responses.append(response)
        conversation.prompts.append(prompt)

    # Only yield if we have message data
    if not conversation.prompts:
        return None

    return conversation


def _unix_to_iso(ts: int | float | None) -> str:
    """Convert a Unix timestamp to ISO 8601 string."""
    if ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError, OSError):
        return str(ts)
