"""Shared plugin discovery helpers.

siftd supports extensibility via:
- Drop-in Python files under XDG config dirs (e.g. ~/.config/siftd/adapters/*.py)
- Python entry points (e.g. group 'siftd.adapters')

This module centralizes the common discovery mechanics so individual registries
only need to define interface validation rules and how to map modules to names.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

Validator = Callable[[ModuleType, str], str | None]


def warn(message: str) -> None:
    """Print a warning message to stderr."""
    print(f"Warning: {message}", file=sys.stderr)


def validate_required_interface(
    module: ModuleType,
    origin: str,
    *,
    required_attrs: dict[str, type],
    required_callables: list[str],
) -> str | None:
    """Validate a module has required attrs and callables.

    Returns an error message string if invalid, None if valid.
    """
    for attr, expected_type in required_attrs.items():
        if not hasattr(module, attr):
            return f"{origin}: missing required attribute '{attr}'"
        value = getattr(module, attr)
        if not isinstance(value, expected_type):
            return f"{origin}: '{attr}' must be {expected_type.__name__}, got {type(value).__name__}"

    for func_name in required_callables:
        if not hasattr(module, func_name) or not callable(getattr(module, func_name)):
            return f"{origin}: missing required function '{func_name}'"

    return None


def load_dropin_modules(
    path: Path,
    *,
    module_name_prefix: str,
    validate: Validator,
    warn_fn: Callable[[str], None] = warn,
) -> list[ModuleType]:
    """Load validated drop-in modules from a directory.

    Args:
        path: Directory containing .py files.
        module_name_prefix: Prefix used to construct synthetic module names.
        validate: Validation callback returning error string, or None if valid.
        warn_fn: Called with warning messages (defaults to printing to stderr).

    Returns:
        List of loaded modules that passed validation.
    """
    modules: list[ModuleType] = []
    if not path.is_dir():
        return modules

    for py_file in sorted(path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        module_name = f"{module_name_prefix}{py_file.stem}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                warn_fn(f"could not load drop-in module {py_file.name}")
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            warn_fn(f"failed to import drop-in module {py_file.name}: {e}")
            continue

        error = validate(module, f"drop-in {py_file.name}")
        if error:
            warn_fn(error)
            continue

        modules.append(module)

    return modules


def load_entrypoint_modules(
    group: str,
    *,
    validate: Validator,
    warn_fn: Callable[[str], None] = warn,
) -> list[ModuleType]:
    """Load validated modules from a Python entry point group.

    Args:
        group: Entry point group name (e.g. "siftd.adapters").
        validate: Validation callback returning error string, or None if valid.
        warn_fn: Called with warning messages (defaults to printing to stderr).

    Returns:
        List of loaded modules that passed validation.
    """
    modules: list[ModuleType] = []
    eps = importlib.metadata.entry_points(group=group)

    for ep in eps:
        try:
            module = ep.load()
        except Exception as e:
            warn_fn(f"failed to load entry point {group} '{ep.name}': {e}")
            continue

        origin = f"entry point '{ep.name}'"
        error = validate(module, origin)
        if error:
            warn_fn(error)
            continue

        modules.append(module)

    return modules

