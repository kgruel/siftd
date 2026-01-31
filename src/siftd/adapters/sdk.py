"""Adapter authoring SDK.

Helpers that reduce boilerplate in adapter implementations.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.domain import Harness, Source

if TYPE_CHECKING:
    from siftd.peek.types import PeekExchange, PeekScanResult


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


def extract_text_with_placeholders(blocks: list) -> str | None:
    """Extract text from content blocks, adding placeholders for non-text.

    Unlike simple text extraction, this indicates presence of images,
    tool uses, etc. with readable placeholders.

    Args:
        blocks: List of content blocks (dicts or strings).

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
                parts.append("[thinking]")
            elif block_type:
                parts.append(f"[{block_type}]")

    return "\n".join(parts) if parts else None


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
    from siftd.peek.types import PeekScanResult

    session_id = path.stem
    workspace_path: str | None = None
    model: str | None = None
    exchange_count = 0
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

                    # Extract metadata from first user record
                    if workspace_path is None:
                        workspace_path = record.get(cwd_key)
                        session_id_from_record = record.get(session_id_key)
                        if session_id_from_record:
                            session_id = session_id_from_record

                elif record_type == assistant_type:
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

    if exchange_count == 0:
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
    from siftd.peek.types import PeekExchange

    # Enforce minimum
    if last_n < 1:
        last_n = 1

    exchanges: list[PeekExchange] = []
    current_exchange: PeekExchange | None = None

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

                elif record_type == assistant_type and current_exchange is not None:
                    if get_usage:
                        input_tokens, output_tokens = get_usage(record)
                        current_exchange.input_tokens += input_tokens
                        current_exchange.output_tokens += output_tokens

                    current_exchange.response_text = extract_text_with_placeholders(
                        content_blocks
                    )

                    # Collect tool calls
                    tool_counter: Counter[str] = Counter()
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_name = block.get("name", "unknown")
                            if tool_aliases:
                                tool_name = canonicalize_tool_name(
                                    tool_name, tool_aliases
                                )
                            tool_counter[tool_name] += 1
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
