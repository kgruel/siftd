"""Adapter authoring SDK.

Helpers that reduce boilerplate in adapter implementations.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from siftd.domain import Harness, Source


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
