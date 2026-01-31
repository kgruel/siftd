"""Adapter registry: discovers built-in, drop-in, and entry point adapters."""

from pathlib import Path

from siftd.adapters import aider, claude_code, codex_cli, gemini_cli
from siftd.adapters.validation import validate_adapter
from siftd.plugin_discovery import PluginInfo, load_dropin_modules, load_entrypoint_modules

# Re-export for backwards compatibility (deprecated, use siftd.adapters.validation)
_validate_adapter = validate_adapter


def load_builtin_adapters() -> list[PluginInfo]:
    """Return the built-in adapter modules as PluginInfo."""
    builtins = [aider, claude_code, codex_cli, gemini_cli]
    return [
        PluginInfo(
            name=getattr(m, "NAME", m.__name__.split(".")[-1]),
            origin="builtin",
            module=m,
        )
        for m in builtins
    ]


def load_dropin_adapters(path: Path) -> list[PluginInfo]:
    """Scan a directory for .py adapter files, import and validate them."""
    return load_dropin_modules(
        path,
        module_name_prefix="siftd_dropin_adapter_",
        validate=_validate_adapter,
        get_name=lambda m: getattr(m, "NAME", "unknown"),
    )


def load_entrypoint_adapters() -> list[PluginInfo]:
    """Discover adapters registered via the 'siftd.adapters' entry point group."""
    return load_entrypoint_modules(
        group="siftd.adapters",
        validate=_validate_adapter,
        get_name=lambda m: getattr(m, "NAME", "unknown"),
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


def load_all_adapters(dropin_path: Path | None = None) -> list[PluginInfo]:
    """Load adapters from all sources, deduplicated by NAME.

    Priority: drop-in > entry point > built-in (drop-ins can override built-ins).

    Returns:
        List of PluginInfo for all discovered adapters, deduplicated by name.
    """
    from siftd.paths import adapters_dir

    if dropin_path is None:
        dropin_path = adapters_dir()

    dropins = load_dropin_adapters(dropin_path)
    entrypoints = load_entrypoint_adapters()
    builtins = load_builtin_adapters()

    seen_names: set[str] = set()
    result: list[PluginInfo] = []

    # Priority order: drop-in > entry point > built-in
    for plugin_list in [dropins, entrypoints, builtins]:
        for plugin in plugin_list:
            if plugin.name in seen_names:
                # Silently skip - expected when drop-in overrides built-in
                continue
            seen_names.add(plugin.name)
            result.append(plugin)

    return result
