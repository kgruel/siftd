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


def _get_version() -> str:
    """Get package version from metadata."""
    try:
        from importlib.metadata import version

        return version("siftd")
    except Exception:
        return "unknown"
