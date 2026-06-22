"""Common formatting utilities for CLI output.

Shared by peek, query, search, and export commands.
"""

import os
import re
import shutil
import sys
from datetime import datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.domain.search_types import MATCH_CLOSE, MATCH_OPEN

if TYPE_CHECKING:
    from typing import TextIO


def supports_unicode() -> bool:
    """Whether stdout can encode the Unicode glyphs the painted UI draws.

    A terminal under a non-UTF-8 locale (e.g. ``LANG=C``) is a TTY but can't
    render box-drawing/check glyphs; callers route it to a plain ASCII path
    instead of crashing or garbling. Evaluated against the live ``sys.stdout``
    (not cached) so redirected/captured streams are respected.
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        # The full glyph surface the painted UI draws: severity marks, the
        # progress bar fill, rounded box corners, separators, the search rank
        # rail (◆ top / │ mid / · tail) and context caret (▸), and the ``…``
        # truncation marker.
        "✓⚠ℹ✗─│█░╭╮╰╯↳·◆▸…".encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def prefers_ascii(stream: "TextIO | None" = None) -> bool:
    """Whether ``stream`` should get the plain ASCII glyph/rule forms.

    The single capability gate behind ASCII degradation: a non-TTY (a pipe) or a
    non-UTF-8 TTY (``LANG=C``) can't render — or shouldn't be sent — the painted
    Unicode set (severity marks, the ``─`` rules, box corners), so it gets the
    ``-``/``x``/``!`` forms instead. This is the ``not (isatty and
    supports_unicode())`` couplet the status, doctor, and table surfaces each
    used to compute by hand, named once. ``stream`` defaults to ``sys.stdout``.
    """
    out = stream if stream is not None else sys.stdout
    return not (out.isatty() and supports_unicode())


def should_use_ansi(stream: "TextIO | None" = None) -> bool:
    """Whether ``stream`` should receive ANSI colour/style escapes.

    painted's ``print_block`` defaults ``use_ansi`` to ``stream.isatty()`` and
    never consults ``NO_COLOR``; this honours the convention so every CLI surface
    strips colour when the user asks (https://no-color.org), not only when
    piped. A non-empty ``NO_COLOR`` (the widely-adopted reading) disables
    colour even on a TTY; otherwise an interactive TTY gets colour and a pipe
    does not. Callers pass the result as ``print_block(..., use_ansi=...)``.
    ``stream`` defaults to ``sys.stdout``.
    """
    out = stream if stream is not None else sys.stdout
    isatty = hasattr(out, "isatty") and out.isatty()
    return isatty and not os.environ.get("NO_COLOR")


def term_width(fallback: int = 80) -> int:
    """Current terminal width in columns.

    The single terminal-width helper for the whole output layer (tables,
    search snippets, doctor progress). Uses ``shutil.get_terminal_size`` so the
    ``COLUMNS`` env var is honored — which lets callers and tests pin a width
    (e.g. eyeballing at ``COLUMNS=80``) and degenerate ptys fall back cleanly
    instead of reporting 0.
    """
    return shutil.get_terminal_size((fallback, 24)).columns


_MATCH_RE = re.compile(re.escape(MATCH_OPEN) + r"(.*?)" + re.escape(MATCH_CLOSE), re.DOTALL)


def split_match_segments(text: str) -> list[tuple[str, bool]]:
    """Split FTS5 snippet text into ``(segment, is_match)`` runs.

    Matched terms arrive wrapped in the ``MATCH_OPEN``/``MATCH_CLOSE`` delimiters
    embedded in the data by the snippet() SQL. This yields the alternating literal
    and matched runs so each formatter can style the matches its own way (terminal
    → accent span, markdown → ``**bold**``) instead of leaking the raw ``>>>``/
    ``<<<`` markers. Unbalanced markers (an open with no close) stay literal text.
    The painted-free splitter shared by every search renderer.
    """
    out: list[tuple[str, bool]] = []
    last = 0
    for m in _MATCH_RE.finditer(text):
        if m.start() > last:
            out.append((text[last : m.start()], False))
        out.append((m.group(1), True))
        last = m.end()
    if last < len(text):
        out.append((text[last:], False))
    return out or [(text, False)]


_ROLE_ABBREV = {"assistant": "asst"}


def role_label(role: str, *, abbrev: bool = False) -> str:
    """Normalize a role / display label to its lowercase presentation token.

    Detail surfaces pass the role through full (you're reading a transcript);
    search passes ``abbrev=True`` for the dense scan list, where ``assistant``
    collapses to ``asst``. The brackets and styling stay with each renderer — this
    owns only the casing and the abbreviation, so the terminal detail and search
    paths render the same role identically instead of drifting (full/UPPER/abbrev).
    """
    token = role.lower()
    return _ROLE_ABBREV.get(token, token) if abbrev else token


def fmt_tokens(n: int) -> str:
    """Format token count: 1234 -> '1.2k', 1_500_000 -> '1.5M', 46_590_000_000 -> '46.6B'.

    Rolls over through k/M/B so billion-scale corpora (post cache-fold) read
    sanely instead of overflowing the 'k' suffix (e.g. '46822076.3k'). The 'M'/'B'
    forms are round-trip compatible with the adapter token parsers that read them.
    """
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
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


def fmt_timestamp(
    iso_timestamp: str | None,
    *,
    time_only: bool = False,
    local_tz: tzinfo | None = None,
) -> str:
    """Format ISO timestamp for display in local time when timezone info exists.

    Args:
        iso_timestamp: ISO 8601 timestamp string (e.g., "2024-01-15T10:23:45Z")
        time_only: If True, return just HH:MM. Otherwise YYYY-MM-DD HH:MM.
        local_tz: Optional local timezone override for tests/callers.

    Returns:
        Formatted timestamp string, or empty string if input is None.
        For date-only strings (<16 chars), returns raw string if not time_only.
        Naive timestamps are treated as already-local and are not shifted.
    """
    if not iso_timestamp:
        return ""
    if len(iso_timestamp) < 16:
        # Date-only or short string: return raw for full mode, empty for time_only
        return "" if time_only else iso_timestamp

    normalized = iso_timestamp.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        if time_only:
            return iso_timestamp[11:16]
        return iso_timestamp[:16].replace("T", " ")

    if dt.tzinfo is not None:
        if local_tz is None:
            dt = dt.astimezone()
        else:
            dt = dt.astimezone(local_tz)

    return dt.strftime("%H:%M" if time_only else "%Y-%m-%d %H:%M")


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


def format_table(columns: list[str], rows: list[list[str]], *, sep: str = "  ") -> str:
    """Format column-aligned table with header and separator line.

    Args:
        columns: Header labels.
        rows: List of string rows (each same length as columns).
        sep: Column separator (default: two spaces).

    Returns:
        Formatted table as a string.
    """
    widths = [len(c) for c in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))
    lines = [sep.join(c.ljust(widths[i]) for i, c in enumerate(columns))]
    lines.append(sep.join("-" * w for w in widths))
    for row in rows:
        lines.append(sep.join(val.ljust(widths[i]) for i, val in enumerate(row)))
    return "\n".join(lines)


def print_indented(text: str, indent: str = "  ") -> None:
    """Print text with each line indented.

    Args:
        text: Text to print (may contain newlines)
        indent: String to prepend to each line (default: two spaces)
    """
    for line in text.splitlines():
        print(f"{indent}{line}")


def format_refs_annotation(refs: list, *, max_shown: int = 5) -> str:
    """Compact one-liner: 'refs: file(r) file(w) +N more'."""
    if not refs:
        return ""

    # Deduplicate: same basename+op shown once
    seen = set()
    unique = []
    for ref in refs:
        key = (ref.basename, ref.op)
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    shown = unique[:max_shown]
    parts = [f"{r.basename}({r.op})" for r in shown]
    overflow = len(unique) - max_shown
    if overflow > 0:
        parts.append(f"+{overflow} more")

    return "refs: " + " ".join(parts)


def print_refs_content(
    all_refs: list, filter_basenames: list[str] | None = None
) -> None:
    """Print file reference content dump section."""
    if not all_refs:
        return

    # Deduplicate by path+op (keep first occurrence for point-in-time snapshot)
    seen = set()
    unique = []
    for ref in all_refs:
        key = (ref.path, ref.op)
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    # Apply basename filter if provided
    if filter_basenames:
        filter_set = {b.lower() for b in filter_basenames}
        unique = [r for r in unique if r.basename.lower() in filter_set]
        if not unique:
            names = ", ".join(filter_basenames)
            print(f"No file references matching: {names}")
            return

    op_labels = {"r": "read", "w": "write", "e": "edit"}

    print(f"\n{'─── File References ─' * 1}{'─' * 30}")
    print()

    for i, ref in enumerate(unique, 1):
        op_label = op_labels.get(ref.op, ref.op)
        print(f"[{i}] {ref.basename} ({op_label})")
        print(f"    {ref.path}")
        print("────")
        if ref.content:
            print(ref.content)
        else:
            print("(no content available)")
        print("────")
        print()
