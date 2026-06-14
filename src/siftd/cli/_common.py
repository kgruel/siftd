"""Shared CLI utilities."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def print_ambiguous_error(exc) -> None:
    """Print a user-facing error for AmbiguousPrefix to stderr."""
    print(f"Error: prefix {exc.prefix!r} matches {exc.total} conversations:", file=sys.stderr)
    for mid in exc.matched_ids:
        print(f"  {mid}", file=sys.stderr)
    if exc.total > len(exc.matched_ids):
        print(f"  ... and {exc.total - len(exc.matched_ids)} more", file=sys.stderr)
    print("Disambiguate with a longer prefix or full ID.", file=sys.stderr)


def resolve_db(args) -> Path:
    """Resolve database path from args."""
    from siftd.paths import db_path

    return Path(args.db) if args.db else db_path()


def apply_config_defaults(
    args,
    config_getter,
    field_defaults: dict | None = None,
    *,
    skip_if=None,
) -> None:
    """Apply config-file defaults where CLI didn't provide a value.

    config_getter: callable returning dict of config values
    field_defaults: {arg_name: hardcoded_default} fallbacks when config is unset
    skip_if: optional predicate on args; if True, skip all defaults
    """
    if skip_if is not None and skip_if(args):
        return
    config = config_getter()
    all_fields = set(config) | set(field_defaults or {})
    for field in all_fields:
        if getattr(args, field, None) is None:
            value = config.get(field)
            if value is None and field_defaults:
                value = field_defaults.get(field)
            if value is not None:
                setattr(args, field, value)


def fidelity_from_args(args, *, default_chars: int = 0):  # -> painted.Fidelity
    """Build a Fidelity spec from standard CLI flags.

    Reads --brief, --full, --thinking, --tools, --chars from args.
    Missing flags are treated as False/None (safe for any command).

    Args:
        args: Parsed argparse namespace.
        default_chars: Base char limit when no flags given (0 = no truncation).
    """
    from painted import Fidelity

    is_full = getattr(args, "full", False)

    visible: set[str] = {"text"}
    if getattr(args, "thinking", False) or is_full:
        visible.add("thinking")
    tools_flag = getattr(args, "tools", None)
    if (tools_flag is not None and tools_flag is not False) or is_full:
        visible.add("tools")

    chars = default_chars
    if getattr(args, "brief", False):
        chars = 80
    if getattr(args, "chars", None) is not None:
        chars = args.chars
    if is_full:
        chars = 0

    depth = 3 if is_full else (0 if getattr(args, "brief", False) else 1)

    return Fidelity(
        depth=depth,
        visible=frozenset(visible),
        chars=chars,
    )


def tool_chars_from_args(args, fidelity) -> int:
    """Derive tool content char limit from args and fidelity.

    Args:
        args: Parsed argparse namespace.
        fidelity: The Fidelity object (from fidelity_from_args).

    Returns:
        Character limit for tool content (0 = no truncation).
    """
    if fidelity.depth >= 3:  # --full
        return 0
    explicit = getattr(args, "tool_chars", None)
    if explicit is not None:
        return explicit
    if getattr(args, "brief", False):
        return 80
    chars = getattr(args, "chars", None)
    if chars is not None:
        return 0 if chars <= 0 else min(chars, 120)
    return 120


def add_fidelity_args(
    parser,
    *,
    full: bool = False,
    brief: bool = False,
    chars: bool = False,
    thinking: bool = False,
    tools: bool = False,
    tool_chars: bool = False,
) -> None:
    """Add the standard fidelity argument group to a parser.

    Opt-in switches for each fidelity-shape flag. Callers enable only
    the axes each verb supports. Mirrors the opt-in composition pattern
    of _filters.add_filter_args.

    tools=True registers --tools as nargs="?" (optional filter string).
    For boolean-only tools (peek, export), leave --tools inline.
    """
    if not any([full, brief, chars, thinking, tools, tool_chars]):
        return
    g = parser.add_argument_group("fidelity")
    if full:
        g.add_argument("-F", "--full", action="store_true", help="Full text (no truncation)")
    if brief:
        g.add_argument("-b", "--brief", action="store_true", help="Compact view (80 char truncation)")
    if chars:
        g.add_argument("--chars", type=int, metavar="N", help="Truncate text at N characters")
    if thinking:
        g.add_argument("--thinking", action="store_true", help="Show model thinking/reasoning blocks")
    if tools:
        g.add_argument(
            "--tools", nargs="?", const="all", metavar="FILTER",
            help="Show tool inputs/results (optional filter: tool name prefix or 'errors')",
        )
    if tool_chars:
        g.add_argument(
            "--tool-chars", type=int, metavar="N", default=None,
            help="Truncate tool input/result at N characters (default: 120)",
        )


def add_output_args(
    parser,
    *,
    json: bool = False,
    limit: bool = False,
    limit_default: int | None = 10,
    no_hints: bool = False,
) -> None:
    """Add the standard output argument group to a parser.

    Opt-in switches for common output controls. Mirrors the opt-in
    composition pattern of _filters.add_filter_args.
    """
    if not any([json, limit, no_hints]):
        return
    g = parser.add_argument_group("output")
    if json:
        g.add_argument("--json", action="store_true", help="Output as JSON")
    if limit:
        default_hint = f" (default: {limit_default})" if limit_default is not None else ""
        g.add_argument(
            "-n", "--limit", type=int, default=limit_default,
            help=f"Number of results to show{default_hint}",
        )
    if no_hints:
        g.add_argument(
            "--no-hints", action="store_true", dest="no_hints",
            help="Suppress hint-severity caveat findings.",
        )


def _parse_turns_range(s: str) -> tuple[int, int]:
    """Parse a turns range string like '-2:+2' or '5:10' into (start, end) offsets.

    Returns (window_start, window_end) as signed integers.
    Raises SystemExit(2) on invalid format or if end < start.
    """
    parts = s.split(":")
    if len(parts) != 2:
        print(f"error: --turns must be in A:B format (e.g. -2:+2, 5:10), got: {s!r}", file=sys.stderr)
        sys.exit(2)
    try:
        start = int(parts[0].lstrip("+"))
        end = int(parts[1].lstrip("+"))
    except ValueError:
        print(f"error: --turns values must be integers, got: {s!r}", file=sys.stderr)
        sys.exit(2)
    if end < start:
        print(f"error: --turns end ({end}) must be >= start ({start})", file=sys.stderr)
        sys.exit(2)
    return start, end


class _TurnsRangeAction(argparse.Action):
    """Consume the next argv token unconditionally for --turns.

    argparse's prefix-heuristic rejects '-2:+2' as a flag when spaced
    (--turns -2:+2). nargs=1 forces the following token to be consumed
    as the option's argument, bypassing the heuristic. The '=' form
    (--turns=-2:+2) is unaffected.
    """

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.setdefault("nargs", 1)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ARG002
        setattr(namespace, self.dest, values[0] if isinstance(values, list) else values)


_ALL_ANCHORS: frozenset[str] = frozenset({"from-start", "from-end", "at-turn", "around"})
_ALL_WINDOWS: frozenset[str] = frozenset({"exchanges", "turns"})


def add_anchor_window_args(
    parser,
    *,
    anchors: frozenset[str] = _ALL_ANCHORS,
    windows: frozenset[str] = _ALL_WINDOWS,
) -> None:
    """Add anchor + window argument groups to a parser.

    Registers a mutually-exclusive anchor group and two window flags.
    Callers can restrict to a subset via the anchors/windows params —
    e.g., search uses anchors=frozenset({"around"}), windows=frozenset({"turns"}).

    Designed for reuse across query <id> (Slice 1) and search (Slice 2).
    Neither the group name nor any help text assumes a specific command.
    """
    unknown_anchors = anchors - _ALL_ANCHORS
    if unknown_anchors:
        raise ValueError(f"unknown anchors: {unknown_anchors!r}; valid: {_ALL_ANCHORS!r}")
    unknown_windows = windows - _ALL_WINDOWS
    if unknown_windows:
        raise ValueError(f"unknown windows: {unknown_windows!r}; valid: {_ALL_WINDOWS!r}")
    g = parser.add_argument_group("navigation")
    anchor = g.add_mutually_exclusive_group()
    if "from-start" in anchors:
        anchor.add_argument(
            "--from-start", action="store_true", dest="from_start",
            help="Anchor at the start of the conversation (turn 0)",
        )
    if "from-end" in anchors:
        anchor.add_argument(
            "--from-end", action="store_true", dest="from_end",
            help="Anchor at the end of the conversation (last turn)",
        )
    if "at-turn" in anchors:
        anchor.add_argument(
            "--at-turn", type=int, dest="at_turn", metavar="N",
            help="Anchor at the N-th turn (0-indexed)",
        )
    if "around" in anchors:
        anchor.add_argument(
            "--around", dest="around", metavar="PHRASE",
            help="Anchor at the first FTS5 phrase match in the conversation",
        )
    window = g.add_mutually_exclusive_group()
    if "exchanges" in windows:
        window.add_argument(
            "--exchanges", type=int, metavar="N",
            help="Number of turns to show from anchor (requires an anchor flag)",
        )
    if "turns" in windows:
        window.add_argument(
            "--turns", dest="turns_range", metavar="A:B",
            action=_TurnsRangeAction,
            help="Turn range relative to anchor, e.g. -2:+2 or 5:10 (requires an anchor flag)",
        )


# Deprecated-surface registry. Each entry is (old_surface, new_surface).
# Kept as a single list so a test can snapshot the full deprecated set and
# prevent silent accretion of shims — the migration-cost discipline from the
# CLI UX audit. Notices fire at most once per process (per old surface).
DEPRECATED_SURFACES: list[tuple[str, str]] = [
    ("query sql", "report"),
]
_DEPRECATION_EMITTED: set[str] = set()


def deprecation_notice(old: str, new: str) -> None:
    """Print a one-line deprecation notice to stderr, once per process.

    Keeps the old surface working (alias-first migration) while steering
    callers to the canonical one. stderr so it never pollutes piped stdout
    or --json output.
    """
    if old in _DEPRECATION_EMITTED:
        return
    _DEPRECATION_EMITTED.add(old)
    print(
        f"note: '{old}' is deprecated and will be removed in a future release; use '{new}'.",
        file=sys.stderr,
    )


def _get_version() -> str:
    """Get package version from metadata."""
    try:
        from importlib.metadata import version

        return version("siftd")
    except Exception:
        return "unknown"
