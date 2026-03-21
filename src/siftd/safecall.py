"""Safe operations that handle common failure modes.

Every function in this module:
- Catches a well-defined set of exceptions
- Logs a structured debug message with context
- Returns a typed fallback value (None, [], {})
- Never raises

This eliminates the need for try/except at every call site.
Adapters, storage, and API modules all use these instead of
inline exception handling.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("siftd.safecall")

# Exception tuples — defined once, used everywhere
_IO_ERRORS = (OSError, UnicodeDecodeError)
_JSON_ERRORS = (json.JSONDecodeError, TypeError, ValueError)


# ── File I/O ─────────────────────────────────────────────────────────


def read_text(path: Path, *, context: str = "") -> str | None:
    """Read a text file, returning None on any OS/encoding error."""
    try:
        return path.read_text(encoding="utf-8")
    except _IO_ERRORS as e:
        log.debug("read_text failed: %s (%s) %s", path, e, context)
        return None


def load_json(path: Path, *, context: str = "") -> dict | None:
    """Load a JSON file, returning None on any read/parse error."""
    text = read_text(path, context=context)
    if text is None:
        return None
    try:
        return json.loads(text)
    except _JSON_ERRORS as e:
        log.debug("load_json failed: %s (%s) %s", path, e, context)
        return None


def iter_jsonl(path: Path, *, context: str = "") -> list[dict]:
    """Load a JSONL file, skipping malformed lines. Returns parsed dicts."""
    text = read_text(path, context=context)
    if text is None:
        return []
    results: list[dict] = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except _JSON_ERRORS:
            log.debug("iter_jsonl: skipped line %d in %s %s", i, path, context)
    return results


# ── JSON Parsing ─────────────────────────────────────────────────────


def parse_json(raw: str | None, *, fallback: Any = None, context: str = "") -> Any:
    """Parse a JSON string, returning *fallback* on failure."""
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except _JSON_ERRORS:
        log.debug("parse_json failed: %r %s", raw[:100] if isinstance(raw, str) else raw, context)
        return fallback


def parse_json_args(raw: Any) -> dict:
    """Parse tool-call arguments into a dict.

    Handles the three shapes adapters encounter:
    - dict → returned as-is
    - JSON string → parsed, must be a dict
    - anything else → {"raw": str(raw)}

    Used by codex_cli, copilot_cli, pi_agent for function_call arguments.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            result = json.loads(raw)
            if isinstance(result, dict):
                return result
        except _JSON_ERRORS:
            pass
        return {"raw": raw} if raw else {}
    return {"raw": str(raw)} if raw else {}


# ── Timestamp Parsing ────────────────────────────────────────────────


def epoch_ms_to_iso(ms: int | float | None, *, context: str = "") -> str | None:
    """Convert epoch milliseconds to ISO 8601, returning None on failure."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    except (ValueError, TypeError, OSError, OverflowError) as e:
        log.debug("epoch_ms_to_iso failed: %r (%s) %s", ms, e, context)
        return None
