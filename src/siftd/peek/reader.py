"""Session reader: parse full session detail from JSONL files."""

import json
import logging
from pathlib import Path

from siftd.adapters.registry import load_all_adapters
from siftd.peek.scanner import DiscoveredFile, _discover_files, _scan_session_file
from siftd.peek.types import SessionDetail

logger = logging.getLogger(__name__)


class AmbiguousSessionError(Exception):
    """Raised when a session ID prefix matches multiple files."""

    def __init__(self, prefix: str, matches: list[Path]):
        self.prefix = prefix
        self.matches = matches
        match_list = "\n  ".join(str(m) for m in matches[:10])
        if len(matches) > 10:
            match_list += f"\n  ... and {len(matches) - 10} more"
        super().__init__(
            f"Ambiguous session ID prefix '{prefix}' matches {len(matches)} files:\n  {match_list}"
        )


def read_session_detail(path: Path, *, last_n: int = 5) -> SessionDetail | None:
    """Read session detail from a session file.

    Delegates to the appropriate adapter's peek hooks.

    Args:
        path: Path to the session file.
        last_n: Number of most recent exchanges to include (minimum 1).

    Returns:
        SessionDetail or None if the file can't be read.
    """
    # Enforce minimum
    if last_n < 1:
        last_n = 1

    # Find the adapter for this file
    adapter = _find_adapter_for_file(path)
    if adapter is None:
        return None

    try:
        stat = path.stat()
    except OSError:
        return None

    # Get session info via peek_scan
    file_info = DiscoveredFile(
        path=path,
        mtime=stat.st_mtime,
        adapter_module=adapter,
        adapter_name=getattr(adapter, "NAME", "unknown"),
    )
    info = _scan_session_file(file_info)
    if info is None:
        return None

    # Get exchanges via peek_exchanges
    peek_exchanges_fn = getattr(adapter, "peek_exchanges", None)
    if peek_exchanges_fn is None:
        # Adapter doesn't support exchange detail
        return SessionDetail(
            info=info,
            started_at=None,
            exchanges=[],
        )

    try:
        exchanges = peek_exchanges_fn(path, last_n)
    except Exception as e:
        logger.debug("peek_exchanges failed for %s: %s", path, e, exc_info=True)
        exchanges = []

    # Extract started_at from first exchange or scan result
    started_at = None
    if exchanges and exchanges[0].timestamp:
        started_at = exchanges[0].timestamp

    return SessionDetail(
        info=info,
        started_at=started_at,
        exchanges=exchanges,
    )


def tail_session(path: Path, *, lines: int = 20, raw: bool = True) -> list[str]:
    """Read and format the last N records of a session file.

    Delegates to the appropriate adapter's peek_tail hook.

    Args:
        path: Path to the session file.
        lines: Number of records to return.
        raw: If True, return one JSON object per line (JSONL format).
             If False, pretty-print each record.

    Returns:
        List of formatted strings — one per record.
    """
    adapter = _find_adapter_for_file(path)
    if adapter is None:
        return []

    peek_tail_fn = getattr(adapter, "peek_tail", None)
    if peek_tail_fn is None:
        # Fallback: read raw lines
        return _fallback_tail(path, lines, raw)

    try:
        records = list(peek_tail_fn(path, lines))
    except Exception as e:
        logger.debug("peek_tail failed for %s: %s", path, e, exc_info=True)
        return _fallback_tail(path, lines, raw)

    result = []
    for record in records:
        if isinstance(record, dict):
            if raw:
                result.append(json.dumps(record, separators=(",", ":")))
            else:
                result.append(json.dumps(record, indent=2))
        else:
            result.append(str(record))

    return result


def find_session_file(session_id_prefix: str) -> Path | None:
    """Find a session file by ID prefix match.

    Searches adapter DEFAULT_LOCATIONS for files whose session_id starts
    with the given prefix.

    Args:
        session_id_prefix: Prefix of the session ID to match.

    Returns:
        Path to the matching file, or None if not found.

    Raises:
        AmbiguousSessionError: If multiple files match the prefix.
    """
    prefix_lower = session_id_prefix.lower()
    matches: list[Path] = []

    # Use discovery to find all session files
    discovered = _discover_files(
        threshold_seconds=0,  # Include all
        include_inactive=True,
    )

    for file_info in discovered:
        # Check if session_id starts with prefix
        # First try the file stem
        if file_info.path.stem.lower().startswith(prefix_lower):
            matches.append(file_info.path)
            continue

        # Then try scanning for the real session_id
        try:
            peek_scan = getattr(file_info.adapter_module, "peek_scan", None)
            if peek_scan:
                result = peek_scan(file_info.path)
                if result and result.session_id.lower().startswith(prefix_lower):
                    matches.append(file_info.path)
        except Exception:
            continue

    if not matches:
        return None

    if len(matches) == 1:
        return matches[0]

    raise AmbiguousSessionError(session_id_prefix, matches)


def _find_adapter_for_file(path: Path) -> object | None:
    """Find the adapter module that can handle a file."""
    plugins = load_all_adapters()

    for plugin in plugins:
        module = plugin.module

        # Check if adapter has peek hooks
        if not hasattr(module, "peek_scan"):
            continue

        # Check if file is in adapter's locations
        locations = getattr(module, "DEFAULT_LOCATIONS", [])
        for location in locations:
            try:
                base = Path(location).expanduser().resolve()
                file_resolved = path.resolve()
                if str(file_resolved).startswith(str(base)):
                    return module
            except (OSError, ValueError):
                continue

        # Also try can_handle if available
        can_handle = getattr(module, "can_handle", None)
        if can_handle:
            from siftd.domain import Source

            try:
                source = Source(kind="file", location=path)
                if can_handle(source):
                    return module
            except Exception:
                continue

    return None


def _fallback_tail(path: Path, lines: int, raw: bool) -> list[str]:
    """Fallback tail implementation using seek_last_lines."""
    from siftd.adapters.sdk import seek_last_lines

    raw_lines = seek_last_lines(path, lines)
    result = []
    for line in raw_lines:
        if raw:
            # Return as-is (already one line)
            result.append(line)
        else:
            # Try to pretty-print
            try:
                record = json.loads(line)
                result.append(json.dumps(record, indent=2))
            except (json.JSONDecodeError, ValueError):
                result.append(line)
    return result
