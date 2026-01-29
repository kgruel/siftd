"""Session reader: parse full session detail from JSONL files."""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from siftd.peek.scanner import SessionInfo, _scan_session_file


@dataclass
class PeekExchange:
    timestamp: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    tool_calls: list[tuple[str, int]] = field(default_factory=list)  # (tool_name, count)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionDetail:
    info: SessionInfo
    started_at: str | None = None
    exchanges: list[PeekExchange] = field(default_factory=list)


def read_session_detail(path: Path, *, last_n: int = 5) -> SessionDetail | None:
    """Read session detail from a JSONL file.

    Args:
        path: Path to the JSONL session file.
        last_n: Number of most recent exchanges to include.

    Returns:
        SessionDetail or None if the file can't be read.
    """
    stat = path.stat()
    info = _scan_session_file(path, stat.st_mtime)
    if info is None:
        return None

    # Full parse for exchange detail
    records = _load_records(path)
    if not records:
        return None

    # Find started_at from first timestamped record
    started_at = None
    for record in records:
        ts = record.get("timestamp")
        if ts:
            started_at = ts
            break

    # Build exchanges: pair user prompts with subsequent assistant responses
    exchanges: list[PeekExchange] = []
    current_exchange: PeekExchange | None = None

    for record in records:
        record_type = record.get("type")
        msg = record.get("message") or {}
        content_blocks = _normalize_content(msg.get("content"))

        if record_type == "user":
            # Skip tool_result messages (they're part of the previous exchange)
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content_blocks
            )
            if has_tool_result:
                continue

            # Start a new exchange
            current_exchange = PeekExchange(
                timestamp=record.get("timestamp"),
                prompt_text=_extract_text(content_blocks),
            )
            exchanges.append(current_exchange)

        elif record_type == "assistant" and current_exchange is not None:
            usage = msg.get("usage") or {}
            current_exchange.input_tokens += usage.get("input_tokens", 0)
            current_exchange.output_tokens += usage.get("output_tokens", 0)
            current_exchange.response_text = _extract_text(content_blocks)

            # Collect tool calls
            tool_counter: Counter[str] = Counter()
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_counter[tool_name] += 1
            if tool_counter:
                current_exchange.tool_calls = [
                    (name, count) for name, count in tool_counter.most_common()
                ]

    # Trim to last N exchanges
    if last_n > 0 and len(exchanges) > last_n:
        exchanges = exchanges[-last_n:]

    return SessionDetail(
        info=info,
        started_at=started_at,
        exchanges=exchanges,
    )


def tail_session(path: Path, *, lines: int = 20) -> list[str]:
    """Read and format the last N lines of a session file.

    Args:
        path: Path to the JSONL session file.
        lines: Number of lines to return.

    Returns:
        List of formatted JSON strings (pretty-printed single records).
    """
    raw_lines = _read_last_lines(path, lines)
    result = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            result.append(json.dumps(record, indent=2))
        except (json.JSONDecodeError, ValueError):
            result.append(line)
    return result


def find_session_file(session_id_prefix: str) -> Path | None:
    """Find a session file by ID prefix match.

    Searches adapter DEFAULT_LOCATIONS for files whose stem starts
    with the given prefix.

    Args:
        session_id_prefix: Prefix of the session UUID to match.

    Returns:
        Path to the matching file, or None if not found.
    """
    from siftd.adapters.registry import load_all_adapters

    prefix_lower = session_id_prefix.lower()
    adapters = load_all_adapters()

    for adapter in adapters:
        locations = getattr(adapter, "DEFAULT_LOCATIONS", [])
        for location in locations:
            base = Path(location).expanduser()
            if not base.exists():
                continue
            for jsonl_file in base.glob("**/*.jsonl"):
                if jsonl_file.stem.lower().startswith(prefix_lower):
                    return jsonl_file

    return None


def _load_records(path: Path) -> list[dict]:
    """Load all valid JSON records from a JSONL file."""
    records = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except (OSError, UnicodeDecodeError):
        pass
    return records


def _read_last_lines(path: Path, n: int) -> list[str]:
    """Read the last N non-empty lines from a file."""
    try:
        with path.open("r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []
    # Filter empty lines, take last N
    non_empty = [line for line in all_lines if line.strip()]
    return non_empty[-n:]


def _normalize_content(content) -> list:
    """Normalize message content to a list of blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _extract_text(blocks: list) -> str | None:
    """Extract concatenated text from content blocks."""
    texts = []
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                texts.append(text)
    return "\n".join(texts) if texts else None
