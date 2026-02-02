"""Common formatting utilities for CLI output.

Shared by peek, query, search, and export commands.
"""

from pathlib import Path


def fmt_tokens(n: int) -> str:
    """Format token count: 1234 -> '1.2k', 12345 -> '12.3k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_workspace(path: str | None) -> str:
    """Format workspace path for display. Shows (root) for root/empty paths."""
    if path is None:
        return ""
    if path == "/" or path == "":
        return "(root)"
    return Path(path).name


def fmt_ago(seconds: float) -> str:
    """Format seconds as a human-readable 'ago' string.

    Examples:
        30 -> "just now"
        120 -> "2m ago"
        3700 -> "1h 1m ago"
        7200 -> "2h ago"
    """
    minutes = int(seconds / 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining:
        return f"{hours}h {remaining}m ago"
    return f"{hours}h ago"


def fmt_timestamp(iso_timestamp: str | None, *, time_only: bool = False) -> str:
    """Format ISO timestamp for display.

    Args:
        iso_timestamp: ISO 8601 timestamp string (e.g., "2024-01-15T10:23:45")
        time_only: If True, return just HH:MM. Otherwise YYYY-MM-DD HH:MM.

    Returns:
        Formatted timestamp string, or empty string if input is None.
        For date-only strings (<16 chars), returns raw string if not time_only.
    """
    if not iso_timestamp:
        return ""
    if len(iso_timestamp) < 16:
        # Date-only or short string: return raw for full mode, empty for time_only
        return "" if time_only else iso_timestamp
    if time_only:
        return iso_timestamp[11:16]  # HH:MM
    return iso_timestamp[:16].replace("T", " ")  # YYYY-MM-DD HH:MM


def truncate_text(text: str, limit: int, *, suffix: str = "...") -> str:
    """Truncate text to limit characters, adding suffix if truncated.

    Args:
        text: Text to truncate
        limit: Maximum characters (0 means no truncation)
        suffix: String to append when truncated (default: "...")

    Returns:
        Truncated text with suffix, or original if under limit.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + suffix


def fmt_model(model: str | None, *, strip_date: bool = True) -> str:
    """Format model name for display.

    Args:
        model: Model identifier (e.g., "claude-opus-4-5-20251101")
        strip_date: If True, remove trailing YYYYMMDD date suffix

    Returns:
        Formatted model name, or empty string if None.
    """
    if not model:
        return ""
    if strip_date and "-" in model:
        # e.g. "claude-opus-4-5-20251101" -> "claude-opus-4-5"
        parts = model.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
            return parts[0]
    return model


def print_indented(text: str, indent: str = "  ") -> None:
    """Print text with each line indented.

    Args:
        text: Text to print (may contain newlines)
        indent: String to prepend to each line (default: two spaces)
    """
    for line in text.splitlines():
        print(f"{indent}{line}")
