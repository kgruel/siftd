"""Shared CLI utilities."""

from __future__ import annotations

from pathlib import Path


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
            Formatters may override this via their brief_chars attribute.
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


def _get_version() -> str:
    """Get package version from metadata."""
    try:
        from importlib.metadata import version

        return version("siftd")
    except Exception:
        return "unknown"
