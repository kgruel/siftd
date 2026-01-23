"""Adapter registry: discovers built-in, drop-in, and entry point adapters."""

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from adapters import cline, claude_code, codex_cli, gemini_cli, goose

# Required module-level attributes for a valid adapter
_REQUIRED_ATTRS = {
    "NAME": str,
    "DEFAULT_LOCATIONS": list,
    "DEDUP_STRATEGY": str,
    "HARNESS_SOURCE": str,
}

# Required callable attributes
_REQUIRED_CALLABLES = ["discover", "can_handle", "parse"]

_VALID_DEDUP_STRATEGIES = {"file", "session"}


def _validate_adapter(module: ModuleType, origin: str) -> str | None:
    """Validate an adapter module has the required interface.

    Returns an error message string if invalid, None if valid.
    """
    for attr, expected_type in _REQUIRED_ATTRS.items():
        if not hasattr(module, attr):
            return f"{origin}: missing required attribute '{attr}'"
        value = getattr(module, attr)
        if not isinstance(value, expected_type):
            return f"{origin}: '{attr}' must be {expected_type.__name__}, got {type(value).__name__}"

    if module.DEDUP_STRATEGY not in _VALID_DEDUP_STRATEGIES:
        return f"{origin}: DEDUP_STRATEGY must be 'file' or 'session', got '{module.DEDUP_STRATEGY}'"

    for func_name in _REQUIRED_CALLABLES:
        if not hasattr(module, func_name) or not callable(getattr(module, func_name)):
            return f"{origin}: missing required function '{func_name}'"

    return None


def load_builtin_adapters() -> list:
    """Return the built-in adapter modules."""
    return [claude_code, cline, codex_cli, gemini_cli, goose]


def load_dropin_adapters(path: Path) -> list:
    """Scan a directory for .py adapter files, import and validate them."""
    adapters = []
    if not path.is_dir():
        return adapters

    for py_file in sorted(path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"tbd_dropin_adapter_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                print(f"Warning: could not load drop-in adapter {py_file.name}", file=sys.stderr)
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            print(f"Warning: failed to import drop-in adapter {py_file.name}: {e}", file=sys.stderr)
            continue

        error = _validate_adapter(module, f"drop-in {py_file.name}")
        if error:
            print(f"Warning: {error}", file=sys.stderr)
            continue

        adapters.append(module)

    return adapters


def load_entrypoint_adapters() -> list:
    """Discover adapters registered via the 'tbd.adapters' entry point group."""
    adapters = []

    try:
        eps = importlib.metadata.entry_points(group="tbd.adapters")
    except TypeError:
        # Python 3.9-3.11 compatibility: entry_points() may not support group kwarg
        eps = importlib.metadata.entry_points().get("tbd.adapters", [])

    for ep in eps:
        try:
            module = ep.load()
        except Exception as e:
            print(f"Warning: failed to load entry point adapter '{ep.name}': {e}", file=sys.stderr)
            continue

        error = _validate_adapter(module, f"entry point '{ep.name}'")
        if error:
            print(f"Warning: {error}", file=sys.stderr)
            continue

        adapters.append(module)

    return adapters


def load_all_adapters(dropin_path: Path | None = None) -> list:
    """Load adapters from all sources, deduplicated by NAME.

    Priority: built-in > drop-in > entry point.
    """
    from paths import adapters_dir

    if dropin_path is None:
        dropin_path = adapters_dir()

    builtins = load_builtin_adapters()
    dropins = load_dropin_adapters(dropin_path)
    entrypoints = load_entrypoint_adapters()

    seen_names: set[str] = set()
    result: list = []

    for source_label, adapter_list in [
        ("built-in", builtins),
        ("drop-in", dropins),
        ("entry point", entrypoints),
    ]:
        for adapter in adapter_list:
            name = getattr(adapter, "NAME", None)
            if name in seen_names:
                print(
                    f"Warning: duplicate adapter NAME '{name}' from {source_label}, skipping",
                    file=sys.stderr,
                )
                continue
            seen_names.add(name)
            result.append(adapter)

    return result
