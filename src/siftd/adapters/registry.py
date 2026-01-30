"""Adapter registry: discovers built-in, drop-in, and entry point adapters."""

import sys
from pathlib import Path
from types import ModuleType

from siftd.adapters import aider, claude_code, codex_cli, gemini_cli
from siftd.plugin_discovery import (
    load_dropin_modules,
    load_entrypoint_modules,
    validate_required_interface,
)

# Current adapter interface version
ADAPTER_INTERFACE_VERSION = 1

# Required module-level attributes for a valid adapter
_REQUIRED_ATTRS = {
    "ADAPTER_INTERFACE_VERSION": int,
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
    import inspect

    error = validate_required_interface(
        module,
        origin,
        required_attrs=_REQUIRED_ATTRS,
        required_callables=_REQUIRED_CALLABLES,
    )
    if error:
        return error

    adapter_version = getattr(module, "ADAPTER_INTERFACE_VERSION")
    if adapter_version != ADAPTER_INTERFACE_VERSION:
        return f"{origin}: incompatible interface version {adapter_version}, expected {ADAPTER_INTERFACE_VERSION}"

    if module.DEDUP_STRATEGY not in _VALID_DEDUP_STRATEGIES:
        return f"{origin}: DEDUP_STRATEGY must be 'file' or 'session', got '{module.DEDUP_STRATEGY}'"

    # Validate discover() accepts locations= keyword argument
    discover_func = getattr(module, "discover")
    sig = inspect.signature(discover_func)
    if "locations" not in sig.parameters:
        return f"{origin}: discover() must accept 'locations' keyword argument"

    return None


def load_builtin_adapters() -> list:
    """Return the built-in adapter modules."""
    return [aider, claude_code, codex_cli, gemini_cli]


def load_dropin_adapters(path: Path) -> list:
    """Scan a directory for .py adapter files, import and validate them."""
    return load_dropin_modules(
        path,
        module_name_prefix="siftd_dropin_adapter_",
        validate=_validate_adapter,
    )


def load_entrypoint_adapters() -> list:
    """Discover adapters registered via the 'siftd.adapters' entry point group."""
    return load_entrypoint_modules(
        "siftd.adapters",
        validate=_validate_adapter,
    )


class _AdapterPathOverride:
    """Wrapper that overrides an adapter's DEFAULT_LOCATIONS."""

    def __init__(self, adapter, paths: list[str]):
        self._adapter = adapter
        self._paths = paths

    def __getattr__(self, name):
        if name == "DEFAULT_LOCATIONS":
            return self._paths
        return getattr(self._adapter, name)

    def discover(self, locations=None):
        """Discover using overridden paths, delegating to the adapter."""
        return self._adapter.discover(locations=self._paths)


def wrap_adapter_paths(adapter, paths: list[str]):
    """Create an adapter wrapper with custom discovery paths.

    Args:
        adapter: An adapter module.
        paths: List of directory paths to use instead of DEFAULT_LOCATIONS.

    Returns:
        Wrapped adapter that discovers from the given paths.
    """
    return _AdapterPathOverride(adapter, paths)


def load_all_adapters(dropin_path: Path | None = None) -> list:
    """Load adapters from all sources, deduplicated by NAME.

    Priority: drop-in > entry point > built-in (drop-ins can override built-ins).
    """
    from siftd.paths import adapters_dir

    if dropin_path is None:
        dropin_path = adapters_dir()

    dropins = load_dropin_adapters(dropin_path)
    entrypoints = load_entrypoint_adapters()
    builtins = load_builtin_adapters()

    seen_names: set[str] = set()
    result: list = []

    for source_label, adapter_list in [
        ("drop-in", dropins),
        ("entry point", entrypoints),
        ("built-in", builtins),
    ]:
        for adapter in adapter_list:
            name = getattr(adapter, "NAME", None)
            if name is None:
                print(
                    f"Warning: adapter from {source_label} has no NAME, skipping",
                    file=sys.stderr,
                )
                continue
            if name in seen_names:
                # Silently skip - expected when drop-in overrides built-in
                continue
            seen_names.add(name)
            result.append(adapter)

    return result
