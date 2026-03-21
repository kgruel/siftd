"""Adapter authoring SDK.

Helpers that reduce boilerplate in adapter implementations.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.domain import Harness, Source

if TYPE_CHECKING:
    from siftd.domain.peek import PeekExchange, PeekScanResult


def open_external_db(path: Path) -> sqlite3.Connection:
    """Open an external SQLite database in read-only mode.

    For adapters that need to read third-party tool databases.
    Uses URI mode to ensure no WAL/SHM files are created.

    Args:
        path: Path to the SQLite database file.

    Returns:
        Read-only sqlite3.Connection with row_factory set.

    Raises:
        sqlite3.Error: If the database cannot be opened.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def discover_files(
    locations: Iterable[str | Path] | None,
    default_locations: list[str],
    glob_patterns: list[str],
) -> Iterator[Source]:
    """Walk locations and glob for files, yielding Source objects.

    Args:
        locations: Explicit paths to scan. If None, uses default_locations.
        default_locations: Fallback paths when locations is None.
        glob_patterns: Glob patterns to match (e.g., ["**/*.jsonl", "*.json"]).

    Yields:
        Source objects for each matched file.

    Example:
        def discover(locations=None):
            yield from discover_files(
                locations,
                DEFAULT_LOCATIONS,
                ["**/*.jsonl"],
            )
    """
    for location in locations or default_locations:
        base = Path(location).expanduser()
        if not base.exists():
            continue
        for pattern in glob_patterns:
            for match in base.glob(pattern):
                if match.is_file():
                    yield Source(kind="file", location=match)


def build_harness(
    name: str,
    source: str,
    log_format: str,
    display_name: str | None = None,
) -> Harness:
    """Construct a Harness with consistent defaults.

    Args:
        name: Adapter name (e.g., "claude_code").
        source: Provider source (e.g., "anthropic", "google").
        log_format: Log format (e.g., "jsonl", "json", "markdown").
        display_name: Human-readable name. Defaults to name.title().

    Returns:
        Configured Harness object.

    Example:
        harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT)
    """
    return Harness(
        name=name,
        source=source,
        log_format=log_format,
        display_name=display_name or name.replace("_", " ").title(),
    )


def timestamp_bounds(
    records: Iterable[dict],
    key: str = "timestamp",
) -> tuple[str | None, str | None]:
    """Return (min_ts, max_ts) from records.

    Scans records once, extracting string timestamps by key.
    Returns (None, None) if no timestamps found.

    Args:
        records: Iterable of dicts that may contain timestamp values.
        key: Key to look for timestamps (default: "timestamp").

    Returns:
        Tuple of (earliest_timestamp, latest_timestamp).

    Example:
        started_at, ended_at = timestamp_bounds(records)
    """
    min_ts: str | None = None
    max_ts: str | None = None

    for record in records:
        ts = record.get(key)
        if ts is None:
            continue
        if min_ts is None or ts < min_ts:
            min_ts = ts
        if max_ts is None or ts > max_ts:
            max_ts = ts

    return min_ts, max_ts


@dataclass
class ParseError:
    """Error from parsing a single line/record."""

    line_number: int
    error: str
    raw_line: str


def load_jsonl(path: Path) -> tuple[list[dict], list[ParseError]]:
    """Load JSONL file with line-numbered parse errors.

    Unlike the simple load_jsonl in _jsonl.py, this variant collects
    parse errors with line numbers instead of raising.

    Args:
        path: Path to JSONL file.

    Returns:
        Tuple of (records, errors) where records are successfully parsed
        dicts and errors contain line-specific parse failures.

    Example:
        records, errors = load_jsonl(path)
        if errors:
            for e in errors:
                log.warning(f"Line {e.line_number}: {e.error}")
    """
    records: list[dict] = []
    errors: list[ParseError] = []

    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                errors.append(
                    ParseError(
                        line_number=line_num,
                        error=str(e),
                        raw_line=stripped[:200],  # truncate for safety
                    )
                )

    return records, errors


class ToolCallLinker:
    """Pair tool_use blocks with their tool_result by id.

    Handles the common pattern in Claude-style APIs where tool calls
    and results are split across messages.

    Example:
        linker = ToolCallLinker()

        # In assistant message:
        for block in message.content:
            if block.type == "tool_use":
                linker.add_use(block.id, name=block.name, input=block.input)

        # In subsequent user message:
        for block in message.content:
            if block.type == "tool_result":
                linker.add_result(block.tool_use_id, result=block.content)

        # After processing all messages:
        for tool_use_id, use_data, result_data in linker.get_pairs():
            tool_call = ToolCall(
                tool_name=use_data["name"],
                input=use_data["input"],
                result=result_data.get("result") if result_data else None,
                status="success" if result_data else "pending",
            )
    """

    def __init__(self):
        self._uses: dict[str, dict] = {}  # id -> use data
        self._results: dict[str, dict] = {}  # id -> result data

    def add_use(self, tool_id: str, **data) -> None:
        """Register a tool_use block.

        Args:
            tool_id: The tool call ID (used to match with result).
            **data: Additional data to store (name, input, timestamp, etc).
        """
        self._uses[tool_id] = data

    def add_result(self, tool_id: str, **data) -> None:
        """Register a tool_result block.

        Args:
            tool_id: The tool call ID from the corresponding tool_use.
            **data: Result data (content, is_error, etc).
        """
        self._results[tool_id] = data

    def get_pairs(self) -> list[tuple[str, dict, dict | None]]:
        """Return matched pairs as (tool_id, use_data, result_data).

        result_data is None for tool uses that never received a result.

        Returns:
            List of (tool_id, use_data, result_data) tuples.
        """
        pairs: list[tuple[str, dict, dict | None]] = []
        for tool_id, use_data in self._uses.items():
            result_data = self._results.get(tool_id)
            pairs.append((tool_id, use_data, result_data))
        return pairs

    def pending_uses(self) -> list[tuple[str, dict]]:
        """Return tool uses that have no result yet.

        Useful for creating pending ToolCall objects at end of parsing.

        Returns:
            List of (tool_id, use_data) for unmatched uses.
        """
        return [
            (tool_id, use_data)
            for tool_id, use_data in self._uses.items()
            if tool_id not in self._results
        ]


def flush_pending_calls(
    pending_calls: dict,
) -> None:
    """Finalize tool calls that never received results.

    Iterates pending_calls and appends a ToolCall with status="pending"
    to each response. Adapters call this at the end of parse() to handle
    tool uses that were cut off (session ended mid-tool-call).

    Args:
        pending_calls: Dict of call_id -> (response, tool_name, input_data).
            This is the standard pending tracking dict used by adapters.
    """
    from siftd.domain import ToolCall

    for call_id, (response, tool_name, input_data) in pending_calls.items():
        tool_call = ToolCall(
            tool_name=tool_name,
            input=input_data if isinstance(input_data, dict) else {"raw": input_data},
            result=None,
            status="pending",
            external_id=call_id,
            timestamp=None,
        )
        response.tool_calls.append(tool_call)


# =============================================================================
# Peek helpers — for implementing optional peek hooks in adapters
# =============================================================================


def seek_last_lines(path: Path, n: int, chunk_size: int = 8192) -> list[str]:
    """Efficiently read last N non-empty lines by seeking from end.

    Uses binary seek-from-end to avoid loading entire file.
    For small files, falls back to full read.

    Args:
        path: Path to file.
        n: Number of lines to return.
        chunk_size: Bytes to read per chunk when seeking backwards.

    Returns:
        List of line strings (without newlines), in file order.
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        return []

    if file_size == 0:
        return []

    # For small files, just read the whole thing
    if file_size < chunk_size * 2:
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = [line.rstrip("\n\r") for line in f if line.strip()]
                return lines[-n:] if n > 0 else lines
        except (OSError, UnicodeDecodeError):
            return []

    # Seek from end in chunks
    try:
        with path.open("rb") as f:
            chunks: list[bytes] = []
            position = file_size

            while position > 0:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position)
                chunk = f.read(read_size)
                chunks.insert(0, chunk)

                # Check if we have enough lines
                text = b"".join(chunks).decode("utf-8", errors="replace")
                lines = [line for line in text.split("\n") if line.strip()]
                if len(lines) >= n:
                    return lines[-n:]

            # Read entire file if not enough lines found
            text = b"".join(chunks).decode("utf-8", errors="replace")
            lines = [line for line in text.split("\n") if line.strip()]
            return lines[-n:] if n > 0 else lines
    except (OSError, UnicodeDecodeError):
        return []


def canonicalize_tool_name(raw_name: str, aliases: dict[str, str]) -> str:
    """Apply TOOL_ALIASES mapping to raw tool name.

    Args:
        raw_name: Raw tool name from log file.
        aliases: Mapping of raw names to canonical names.

    Returns:
        Canonical name if mapped, otherwise original name.
    """
    return aliases.get(raw_name, raw_name)


def extract_text_with_placeholders(
    blocks: list,
    *,
    include_thinking: bool = False,
) -> str | None:
    """Extract text from content blocks, adding placeholders for non-text.

    Unlike simple text extraction, this indicates presence of images,
    tool uses, etc. with readable placeholders.

    Args:
        blocks: List of content blocks (dicts or strings).
        include_thinking: Render thinking text inline instead of a placeholder.

    Returns:
        Combined text with placeholders, or None if no content.
    """
    parts: list[str] = []

    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
            elif block_type == "image":
                parts.append("[image]")
            elif block_type == "tool_use":
                tool_name = block.get("name", "tool")
                parts.append(f"[tool: {tool_name}]")
            elif block_type == "tool_result":
                parts.append("[tool result]")
            elif block_type == "thinking":
                if include_thinking:
                    text = block.get("thinking") or block.get("text") or ""
                    if text:
                        parts.append(f"[thinking] {text}")
                    else:
                        parts.append("[thinking]")
                else:
                    parts.append("[thinking]")
            elif block_type:
                parts.append(f"[{block_type}]")

    return "\n".join(parts) if parts else None


def _is_tool_placeholder_only(text: str) -> bool:
    """Return True if text is composed only of tool placeholders."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return all(line.startswith("[tool: ") and line.endswith("]") for line in lines)


def extract_tool_hint(
    raw_name: str,
    input_dict: dict,
    hint_keys: dict[str, list[str]],
    *,
    max_len: int = 60,
) -> str | None:
    """Extract a short summary from a tool_use input dict.

    Uses adapter-provided hint_keys mapping: canonical tool name -> list of
    input keys to try (in priority order). First non-empty value wins.

    Special handling:
    - File paths: shows last 2 components (e.g., "src/config.py")

    Args:
        raw_name: Canonical tool name (e.g., "file.read").
        input_dict: The tool_use input dict.
        hint_keys: Mapping of canonical name -> list of input keys to try.
        max_len: Maximum length of the returned hint.

    Returns:
        Short hint string, or None if no hint available.
    """
    keys = hint_keys.get(raw_name)
    if not keys:
        return None

    for key in keys:
        value = input_dict.get(key)
        if not value or not isinstance(value, str):
            continue

        hint = value.strip()
        if not hint:
            continue

        # File paths: show last 2 components
        if key in ("file_path", "path", "notebook_path"):
            parts = Path(hint).parts
            if len(parts) > 2:
                hint = str(Path(*parts[-2:]))

        if len(hint) > max_len:
            hint = hint[: max_len - 3] + "..."

        return hint

    return None


def peek_jsonl_scan(
    path: Path,
    *,
    user_type: str = "user",
    assistant_type: str = "assistant",
    type_key: str = "type",
    cwd_key: str = "cwd",
    session_id_key: str = "sessionId",
    model_path: tuple[str, ...] = ("message", "model"),
    timestamp_key: str = "timestamp",
    is_tool_result: Callable[[dict], bool] | None = None,
) -> PeekScanResult | None:
    """Generic JSONL scanner with configurable keys.

    Scans a JSONL file to extract lightweight session metadata.
    Configurable to handle different schemas (Claude Code, Codex, etc).

    Note: For adapters with subagent/parent relationships (like Claude Code),
    implement a custom peek_scan that handles the specific detection logic.

    Args:
        path: Path to JSONL file.
        user_type: Value of type_key for user records.
        assistant_type: Value of type_key for assistant records.
        type_key: Key that contains record type.
        cwd_key: Key that contains workspace path.
        session_id_key: Key that contains session ID.
        model_path: Tuple of keys to traverse to find model name.
        timestamp_key: Key that contains timestamp.
        is_tool_result: Optional callable(record) -> bool to detect tool_result
            messages that should not count as exchanges.

    Returns:
        PeekScanResult or None if file can't be parsed.
    """
    from siftd.domain.peek import PeekScanResult

    session_id = path.stem
    workspace_path: str | None = None
    model: str | None = None
    exchange_count = 0
    saw_user = False
    started_at: str | None = None
    last_activity_at: str | None = None

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                record_type = record.get(type_key)
                ts = record.get(timestamp_key)

                # Track timestamp bounds
                if ts:
                    if started_at is None or ts < started_at:
                        started_at = ts
                    if last_activity_at is None or ts > last_activity_at:
                        last_activity_at = ts

                if record_type == user_type:
                    # Check if it's a tool_result (not a real exchange)
                    if is_tool_result and is_tool_result(record):
                        continue

                    exchange_count += 1
                    saw_user = True

                    # Extract metadata from first user record
                    if workspace_path is None:
                        workspace_path = record.get(cwd_key)
                        session_id_from_record = record.get(session_id_key)
                        if session_id_from_record:
                            session_id = session_id_from_record

                elif record_type == assistant_type:
                    if exchange_count == 0 and not saw_user:
                        exchange_count = 1
                    # Extract model from path
                    obj = record
                    for key in model_path:
                        if isinstance(obj, dict):
                            obj = obj.get(key)
                        else:
                            obj = None
                            break
                    if obj and isinstance(obj, str):
                        model = obj

    except (OSError, UnicodeDecodeError):
        return None

    if exchange_count == 0 or not saw_user:
        return None

    return PeekScanResult(
        session_id=session_id,
        workspace_path=workspace_path,
        model=model,
        exchange_count=exchange_count,
        started_at=started_at,
        last_activity_at=last_activity_at,
    )


def peek_jsonl_exchanges(
    path: Path,
    last_n: int,
    *,
    user_type: str = "user",
    assistant_type: str = "assistant",
    type_key: str = "type",
    timestamp_key: str = "timestamp",
    get_content_blocks: Callable[[dict], list],
    get_usage: Callable[[dict], tuple[int, int]] | None = None,
    is_tool_result: Callable[[dict], bool] | None = None,
    tool_aliases: dict[str, str] | None = None,
    include_thinking: bool = False,
) -> list[PeekExchange]:
    """Generic JSONL exchange extractor.

    Args:
        path: Path to JSONL file.
        last_n: Number of most recent exchanges to return (minimum 1).
        user_type: Value of type_key for user records.
        assistant_type: Value of type_key for assistant records.
        type_key: Key that contains record type.
        timestamp_key: Key that contains timestamp.
        get_content_blocks: callable(record) -> list[dict] to extract content blocks.
        get_usage: Optional callable(record) -> (input_tokens, output_tokens) tuple.
        is_tool_result: Optional callable(record) -> bool to detect tool_result.
        tool_aliases: Optional TOOL_ALIASES dict for canonicalizing tool names.

    Returns:
        List of PeekExchange objects.
    """
    from siftd.domain.peek import PeekExchange, PeekNarrativeBlock, PeekToolCall

    # Enforce minimum
    if last_n < 1:
        last_n = 1

    exchanges: list[PeekExchange] = []
    current_exchange: PeekExchange | None = None
    tool_counter: Counter[str] = Counter()

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                record_type = record.get(type_key)
                content_blocks = get_content_blocks(record)

                if record_type == user_type:
                    if is_tool_result and is_tool_result(record):
                        continue

                    current_exchange = PeekExchange(
                        timestamp=record.get(timestamp_key),
                        prompt_text=extract_text_with_placeholders(content_blocks),
                    )
                    exchanges.append(current_exchange)
                    # Reset per-exchange accumulator for tool calls
                    tool_counter = Counter()

                elif record_type == assistant_type:
                    if current_exchange is None:
                        current_exchange = PeekExchange(
                            timestamp=record.get(timestamp_key),
                            prompt_text=None,
                        )
                        exchanges.append(current_exchange)
                        tool_counter = Counter()

                    if get_usage:
                        input_tokens, output_tokens = get_usage(record)
                        current_exchange.input_tokens += input_tokens
                        current_exchange.output_tokens += output_tokens

                    # Keep first non-empty response_text (shows reasoning/intent)
                    text = extract_text_with_placeholders(
                        content_blocks,
                        include_thinking=include_thinking,
                    )
                    if text and not current_exchange.response_text:
                        if _is_tool_placeholder_only(text):
                            text = None
                    if text and not current_exchange.response_text:
                        current_exchange.response_text = text

                    # Build narrative blocks and accumulate tool calls across assistant turns
                    pending_tool_blocks: list[PeekToolCall] = []
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text":
                            text_val = block.get("text", "")
                            if text_val:
                                current_exchange.narrative.append(
                                    PeekNarrativeBlock(block_type="text", content=text_val)
                                )
                        elif block_type == "thinking":
                            if include_thinking:
                                text_val = block.get("thinking") or block.get("text") or ""
                                if text_val:
                                    current_exchange.narrative.append(
                                        PeekNarrativeBlock(block_type="thinking", content=text_val)
                                    )
                        elif block_type == "tool_use":
                            tool_name = block.get("name", "unknown")
                            if tool_aliases:
                                tool_name = canonicalize_tool_name(tool_name, tool_aliases)
                            tool_counter[tool_name] += 1
                            input_dict = block.get("input") if isinstance(block.get("input"), dict) else {}
                            hint = None
                            if input_dict:
                                hint = str(input_dict.get("description") or input_dict.get("command") or input_dict.get("file_path") or input_dict.get("path") or input_dict.get("pattern") or "") or None
                            pending_tool_blocks.append(PeekToolCall(tool_name=tool_name, input=hint))
                    if pending_tool_blocks:
                        current_exchange.narrative.append(
                            PeekNarrativeBlock(block_type="tool_calls", tool_calls=pending_tool_blocks)
                        )
                    if tool_counter:
                        current_exchange.tool_calls = list(tool_counter.most_common())

    except (OSError, UnicodeDecodeError):
        return []

    # Return last N
    return exchanges[-last_n:] if len(exchanges) > last_n else exchanges


def peek_jsonl_tail(
    path: Path, lines: int, *, parse_json: bool = True
) -> Iterator[dict | str]:
    """Read last N records from JSONL file.

    Uses seek_last_lines for efficiency.

    Args:
        path: Path to JSONL file.
        lines: Number of records to return.
        parse_json: If True, parse lines as JSON. If False, return raw strings.

    Yields:
        Parsed JSON dicts (if parse_json=True) or raw line strings.
    """
    raw_lines = seek_last_lines(path, lines)
    for line in raw_lines:
        if parse_json:
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                yield line
        else:
            yield line


# =============================================================================
# Record normalization — the general pattern for adapter peek support
# =============================================================================
#
# Adapters that provide a normalize_record() function get peek_scan,
# peek_exchanges, and peek_tail for free. The normalizer maps native
# records to NormalizedRecord, and the SDK provides the scan/exchange logic.


@dataclass
class NormalizedRecord:
    """A record normalized to a common form for SDK peek helpers.

    Adapters produce these from their native record format. The SDK
    consumes them to implement peek_scan, peek_exchanges, etc.

    kind values:
        "user"        — A real user prompt (counts as an exchange).
        "assistant"   — An assistant response.
        "tool_result" — A tool result that looks like a user message
                        but should not count as an exchange.
        "tool_use"    — A tool invocation (separate from assistant content,
                        e.g., Codex function_call records).
        "metadata"    — Session metadata (session_id, workspace, model).
        "usage"       — Standalone usage/token record.
    """

    kind: str
    timestamp: str | None = None
    content_blocks: list[dict] = field(default_factory=list)
    session_id: str | None = None
    workspace_path: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_name: str | None = None
    extra: dict = field(default_factory=dict)


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Iterate parsed JSON records from a JSONL file.

    Skips blank lines and unparseable lines silently.

    Args:
        path: Path to JSONL file.

    Yields:
        Parsed dicts, one per valid JSONL line.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except (OSError, UnicodeDecodeError):
        return


def peek_scan_from_records(
    records: Iterable[dict],
    normalize: Callable[[dict], NormalizedRecord | None],
    *,
    default_session_id: str,
    subagent_path_marker: str | None = None,
    file_path: Path | None = None,
) -> PeekScanResult | None:
    """Build PeekScanResult from an iterable of raw records and a normalizer.

    This is the format-agnostic core. Callers provide records however they
    are loaded (JSONL lines, JSON array, SQLite rows, etc.).

    Subagent detection: if a record's extra dict contains "agent_id", or
    if the file_path contains subagent_path_marker, the session is treated
    as a subagent. The session_id is synthesized as "{session_id}:{agent_id}"
    and parent_session_id is set to the original session_id.

    Args:
        records: Iterable of raw record dicts.
        normalize: Maps a raw record to NormalizedRecord, or None to skip.
        default_session_id: Fallback session ID (typically path.stem).
        subagent_path_marker: Path substring that indicates a subagent file
            (e.g., "/subagents/"). None to disable path-based detection.
        file_path: Path to the file being scanned (used for path-based
            subagent detection).

    Returns:
        PeekScanResult or None if no exchanges found.
    """
    from siftd.domain.peek import PeekScanResult

    session_id = default_session_id
    workspace_path: str | None = None
    model: str | None = None
    exchange_count = 0
    saw_user = False
    started_at: str | None = None
    last_activity_at: str | None = None
    agent_id: str | None = None
    session_id_from_record: str | None = None

    for raw in records:
        nr = normalize(raw)
        if nr is None:
            continue

        # Track timestamp bounds
        if nr.timestamp:
            if started_at is None or nr.timestamp < started_at:
                started_at = nr.timestamp
            if last_activity_at is None or nr.timestamp > last_activity_at:
                last_activity_at = nr.timestamp

        if nr.kind == "user":
            exchange_count += 1
            saw_user = True
            # First user record wins for metadata
            if workspace_path is None:
                workspace_path = nr.workspace_path or workspace_path
                if nr.session_id:
                    session_id = nr.session_id
                    session_id_from_record = nr.session_id
                # Subagent detection from record extra
                if nr.extra.get("agent_id") and agent_id is None:
                    agent_id = nr.extra["agent_id"]

        elif nr.kind == "assistant":
            if exchange_count == 0 and not saw_user:
                exchange_count = 1
            model = nr.model or model

        elif nr.kind == "metadata":
            if nr.session_id:
                session_id = nr.session_id
                session_id_from_record = nr.session_id
            workspace_path = nr.workspace_path or workspace_path
            model = nr.model or model

    if exchange_count == 0 or not saw_user:
        return None

    # Subagent detection: record-level or path-level
    is_subagent = agent_id is not None
    if (
        not is_subagent
        and subagent_path_marker
        and file_path
        and subagent_path_marker in str(file_path)
    ):
        is_subagent = True

    parent_session_id: str | None = None
    if is_subagent and agent_id:
        session_id = f"{session_id_from_record or default_session_id}:{agent_id}"
        parent_session_id = session_id_from_record
    elif is_subagent:
        # Path-based subagent without agent_id — use session_id as-is
        parent_session_id = session_id_from_record

    return PeekScanResult(
        session_id=session_id,
        workspace_path=workspace_path,
        model=model,
        exchange_count=exchange_count,
        started_at=started_at,
        last_activity_at=last_activity_at,
        parent_session_id=parent_session_id,
    )


def peek_exchanges_from_records(
    records: Iterable[dict],
    normalize: Callable[[dict], NormalizedRecord | None],
    last_n: int,
    *,
    tool_aliases: dict[str, str] | None = None,
    include_thinking: bool = False,
) -> list[PeekExchange]:
    """Build PeekExchange list from an iterable of raw records and a normalizer.

    Format-agnostic core for exchange extraction.

    Args:
        records: Iterable of raw record dicts.
        normalize: Maps a raw record to NormalizedRecord, or None to skip.
        last_n: Number of most recent exchanges to return (minimum 1).
        tool_aliases: Optional mapping of raw tool names to canonical names.
        include_thinking: Include thinking/reasoning blocks in narrative.

    Returns:
        List of PeekExchange objects.
    """
    from siftd.domain.peek import PeekExchange, PeekNarrativeBlock, PeekToolCall

    if last_n < 1:
        last_n = 1

    exchanges: list[PeekExchange] = []
    current_exchange: PeekExchange | None = None
    tool_counter: Counter[str] = Counter()

    for raw in records:
        nr = normalize(raw)
        if nr is None:
            continue

        if nr.kind == "user":
            current_exchange = PeekExchange(
                timestamp=nr.timestamp,
                prompt_text=extract_text_with_placeholders(nr.content_blocks),
            )
            exchanges.append(current_exchange)
            tool_counter = Counter()

        elif nr.kind == "assistant":
            if current_exchange is None:
                current_exchange = PeekExchange(
                    timestamp=nr.timestamp,
                    prompt_text=None,
                )
                exchanges.append(current_exchange)
                tool_counter = Counter()

            # Accumulate usage
            current_exchange.input_tokens += nr.input_tokens
            current_exchange.output_tokens += nr.output_tokens

            # First non-empty response text wins
            text = extract_text_with_placeholders(
                nr.content_blocks,
                include_thinking=include_thinking,
            )
            if text and not current_exchange.response_text:
                if _is_tool_placeholder_only(text):
                    text = None
            if text and not current_exchange.response_text:
                current_exchange.response_text = text

            # Build narrative blocks and track tool calls
            pending_tool_blocks: list[PeekToolCall] = []
            for block in nr.content_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_val = block.get("text", "")
                    if text_val:
                        current_exchange.narrative.append(
                            PeekNarrativeBlock(block_type="text", content=text_val)
                        )
                elif block_type == "thinking":
                    if include_thinking:
                        text_val = block.get("thinking") or block.get("text") or ""
                        if text_val:
                            current_exchange.narrative.append(
                                PeekNarrativeBlock(
                                    block_type="thinking", content=text_val
                                )
                            )
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    if tool_aliases:
                        tool_name = canonicalize_tool_name(tool_name, tool_aliases)
                    tool_counter[tool_name] += 1
                    input_dict = (
                        block.get("input")
                        if isinstance(block.get("input"), dict)
                        else {}
                    )
                    hint = None
                    if input_dict:
                        hint = (
                            str(
                                input_dict.get("description")
                                or input_dict.get("command")
                                or input_dict.get("file_path")
                                or input_dict.get("path")
                                or input_dict.get("pattern")
                                or ""
                            )
                            or None
                        )
                    pending_tool_blocks.append(
                        PeekToolCall(tool_name=tool_name, input=hint)
                    )
            if pending_tool_blocks:
                current_exchange.narrative.append(
                    PeekNarrativeBlock(
                        block_type="tool_calls", tool_calls=pending_tool_blocks
                    )
                )
            if tool_counter:
                current_exchange.tool_calls = list(tool_counter.most_common())

        elif nr.kind == "tool_use":
            # Standalone tool invocation (e.g., Codex function_call records)
            if current_exchange is not None and nr.tool_name:
                tool_name = nr.tool_name
                if tool_aliases:
                    tool_name = canonicalize_tool_name(tool_name, tool_aliases)
                tool_counter[tool_name] += 1
                current_exchange.tool_calls = list(tool_counter.most_common())

        elif nr.kind == "usage":
            # Standalone usage record (e.g., Codex event_msg token_count)
            if current_exchange is not None:
                current_exchange.input_tokens += nr.input_tokens
                current_exchange.output_tokens += nr.output_tokens

    return exchanges[-last_n:] if len(exchanges) > last_n else exchanges


def make_peek_hooks(
    normalize: Callable[[dict], NormalizedRecord | None],
    *,
    tool_aliases: dict[str, str] | None = None,
    log_format: str = "jsonl",
    subagent_path_marker: str | None = None,
) -> tuple[
    Callable[[Path], PeekScanResult | None],
    Callable[..., list[PeekExchange]],
    Callable[[Path, int], Iterator[dict | str]],
]:
    """Generate peek_scan, peek_exchanges, peek_tail from a normalizer.

    This is the convenience function adapters use to get full peek support.
    Returns three callables with the standard peek hook signatures.

    Args:
        normalize: Record normalizer function.
        tool_aliases: Optional tool name canonicalization mapping.
        log_format: File format ("jsonl" for line-oriented JSON).
        subagent_path_marker: Path substring for subagent detection
            (e.g., "/subagents/"). Passed to peek_scan_from_records.

    Returns:
        Tuple of (peek_scan, peek_exchanges, peek_tail) functions.
    """

    def peek_scan(path: Path) -> PeekScanResult | None:
        return peek_scan_from_records(
            iter_jsonl(path),
            normalize,
            default_session_id=path.stem,
            subagent_path_marker=subagent_path_marker,
            file_path=path,
        )

    def peek_exchanges(
        path: Path,
        last_n: int = 5,
        *,
        include_thinking: bool = False,
    ) -> list[PeekExchange]:
        return peek_exchanges_from_records(
            iter_jsonl(path),
            normalize,
            last_n,
            tool_aliases=tool_aliases,
            include_thinking=include_thinking,
        )

    def peek_tail(
        path: Path, lines: int = 20
    ) -> Iterator[dict | str]:
        yield from peek_jsonl_tail(path, lines, parse_json=True)

    return peek_scan, peek_exchanges, peek_tail
