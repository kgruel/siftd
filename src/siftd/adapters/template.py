"""Template adapter: copy and customize for a new log format (interface version 2).

Copy this file to ~/.config/siftd/adapters/my_harness.py and customize.

An adapter only decodes. A reader yields the raw units of a source (here, the
lines of a JSONL file); a decoder turns each unit into the canonical records in
siftd.adapters.records; the shared assembler decides where each record attaches
(which prompt a response answers, which call a result completes) and counts
every record in a ledger. The framework derives parse() and the peek hooks from
the declarations below, so there is no tree to build and nothing to keep in step.

Required exports:
    ADAPTER_INTERFACE_VERSION (int): 2 (decoding). 1 is the legacy parse() interface.
    NAME (str): Unique adapter identifier (e.g., "my_harness").
    DEFAULT_LOCATIONS (list[str]): Paths to scan when no --path given.
    DEDUP_STRATEGY (str): "file" or "session".
    HARNESS_SOURCE (str): Provider name (e.g., "anthropic", "openai").

    discover(locations=None) -> Iterable[Source]: Find log sources.
    can_handle(source: Source) -> bool: Check if this adapter handles source.
    read(source: Source) -> Iterable[unit]: Yield each raw unit of the source.
    decoder(source: Source) -> Decoder: A decoder (a class taking the source works).

Optional exports:
    SUPPORT_TIER (str): "core", "contrib", or "frozen" (defaults to "contrib").
    HARNESS_LOG_FORMAT (str): Log format (e.g., "jsonl", "json").
    HARNESS_DISPLAY_NAME (str): Human-readable name.
    TOOL_ALIASES (dict[str, str]): Map raw tool names to canonical names.
    default_session(source) -> Session | None: Names a source whose records state no session.
    change_paths(source) -> list[Path]: Files whose change means the source changed.
    SUPPORTS_LIVE_REGISTRATION (bool): Tagging during a live session is supported.
    SUBAGENT_PATH_MARKER (str): Path marker identifying subagent logs.
    PEEK_GLOB_PATTERNS (list[str]): Globs `siftd peek` scans under DEFAULT_LOCATIONS.
    TOOL_HINT_KEYS (dict[str, list[str]]): Input keys that summarize a canonical tool call.
    SUPPORTS_PEEK (bool): False when peek must not be derived (default True).
    PEEK_LAST_OF_MANY (bool): A source holding several conversations peeks as its last.
    peek_read(source), peek_enrich(path, scan), peek_scan / peek_exchanges / peek_tail:
        peek refinements and overrides. See docs/guides/writing-adapters.md.

Peek needs nothing more: it is derived from read and decoder. See
docs/guides/writing-adapters.md for what every record type means and how the
assembler places it.
"""

from collections.abc import Iterable
from pathlib import Path

from siftd.adapters.assembly import read_jsonl
from siftd.adapters.records import (
    Ignored,
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

# ============================================================================
# Adapter metadata: customize these
# ============================================================================

ADAPTER_INTERFACE_VERSION = 2

# Support tier (optional): "core" (maintained by siftd, ingest expected to work),
# "contrib" (best-effort, the default for drop-ins), or "frozen" (kept working
# as-is; upstream format changes may not be tracked).
SUPPORT_TIER = "contrib"

# Unique identifier for this adapter
NAME = "my_harness"

# Default paths to scan for log files (~ is expanded)
DEFAULT_LOCATIONS = ["~/.my_harness/logs"]

# Deduplication strategy:
#   "file"    one conversation per source (most common); a source may not
#             yield more than one
#   "session" the source is a container of many conversations, each deduped
#             by external_id and replaced when its ended_at moves. A session
#             reporting no ended_at is read as still open and replaced on
#             every content change, so state one wherever your format can
#             bound a session.
DEDUP_STRATEGY = "file"

# Provider/source name (e.g., "anthropic", "openai", "google", "multi")
HARNESS_SOURCE = "openai"

# Log format (e.g., "jsonl", "json", "markdown")
HARNESS_LOG_FORMAT = "jsonl"

# Human-readable display name (optional)
HARNESS_DISPLAY_NAME = "My Harness"

# Map raw tool names to canonical names (optional)
# Canonical names use dot notation: "file.read", "shell.execute", etc.
TOOL_ALIASES: dict[str, str] = {
    # "raw_tool_name": "canonical.name",
}


# ============================================================================
# Required functions
# ============================================================================


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all log files.

    Called during `siftd ingest` to find files to read.
    """
    # Use SDK helper for standard file discovery
    yield from discover_files(
        locations,
        DEFAULT_LOCATIONS,
        glob_patterns=["**/*.jsonl"],  # adjust for your format
    )


def can_handle(source: Source) -> bool:
    """Return True if this adapter can read the given source.

    Called to determine which adapter handles a discovered source.
    Be specific to avoid conflicts with other adapters.
    """
    if source.kind != "file":
        return False
    path = Path(source.location)
    # Customize: check file extension, parent directory, or file contents
    return path.suffix == ".jsonl"


# One JSON object per line. A line that is not JSON is yielded as Malformed, so
# the ledger counts it. For another format, write a generator that yields each
# unit (a JSON message, a database row) and never skips one silently.
read = read_jsonl


# ============================================================================
# The decoder: customize for your log format
# ============================================================================

# Record types that are deliberately not stored. Name each and say why.
_IGNORED_TYPES = frozenset({"note"})


class MyDecoder:
    """Turns each line of a my_harness log into canonical records.

    This example format has one JSON object per line, with a `type`:

    | Record | Becomes | Why |
    |---|---|---|
    | `session` | Session | Names the conversation (`id`) and where it ran (`cwd`). |
    | `user` | PromptRec | A human turn; `text` is its content. |
    | `assistant` | ResponseRec, then one ToolCallRec per `tool_calls` entry | One model message. `usage` is the message's token total. |
    | `tool_result` | ToolResultRec | Completes the call named by `call_id`, whichever came first. |
    | `note` | Ignored | Bookkeeping that is not conversation content. |
    | anything else | Unknown | A new type in the log shows up in the ledger instead of vanishing. |

    Replace the table with what your format holds. Say why every ignored type is
    ignored: the table is how the next reader knows nothing was lost by accident.
    """

    def __init__(self, source: Source):
        self._source = source  # state is allowed, but only decoding state

    def decode(self, unit: dict) -> list[Record]:
        """One unit in, the records it means out. Never return nothing for a unit."""
        at = unit.get("timestamp")
        kind = unit.get("type")
        if kind == "session":
            return [Session(external_id=f"{NAME}::{unit.get('id')}", workspace_path=unit.get("cwd"))]
        if kind == "user":
            return [PromptRec(at=at, content=_text(unit.get("text")), external_id=unit.get("id"))]
        if kind == "assistant":
            return self._assistant(unit, at)
        if kind == "tool_result":
            call_id = unit.get("call_id")
            if not call_id:
                return [Unknown(reason="tool_result:no-call_id")]
            return [
                ToolResultRec(
                    at=at,
                    call_id=str(call_id),
                    result={"output": unit.get("output")},
                    status="error" if unit.get("is_error") else "success",
                )
            ]
        if kind in _IGNORED_TYPES:
            return [Ignored(reason=f"type:{kind}")]
        return [Unknown(reason=f"type:{kind}")]

    def _assistant(self, unit: dict, at: str | None) -> list[Record]:
        usage = unit.get("usage") or {}
        calls = unit.get("tool_calls") or []
        response = ResponseRec(
            at=at,
            content=_text(unit.get("text")),
            external_id=unit.get("id"),
            model=unit.get("model"),
            usage=Usage(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")) if usage else None,
        )
        # A call attaches to the latest response, so it follows the response it belongs to.
        tool_calls = [
            ToolCallRec(at=at, name=c.get("name", "unknown"), input=c.get("input") or {}, call_id=c.get("id"))
            for c in calls
        ]
        return [response, *tool_calls]


def _text(text: str | None) -> list[ContentBlock]:
    return [ContentBlock(block_type="text", content={"text": text})] if text else []


decoder = MyDecoder


def default_session(source: Source) -> Session:
    """The conversation of a file whose records name no session: its file name.

    Optional. Without it, a source that states no `session` record fails
    assembly loudly instead of guessing an identity.
    """
    return Session(external_id=f"{NAME}::{source.as_path.stem}")
