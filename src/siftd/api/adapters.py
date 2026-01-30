"""Adapter discovery API."""

from dataclasses import dataclass
from pathlib import Path

from siftd.adapters import aider, claude_code, codex_cli, gemini_cli
from siftd.adapters.registry import (
    load_builtin_adapters,
    load_dropin_adapters,
    load_entrypoint_adapters,
)
from siftd.paths import adapters_dir
from siftd.plugin_discovery import PluginInfo

# Built-in adapters with their source modules (for copying)
_BUILTIN_ADAPTERS = {
    "aider": aider,
    "claude_code": claude_code,
    "codex_cli": codex_cli,
    "gemini_cli": gemini_cli,
}


@dataclass
class AdapterInfo:
    """Extended adapter information for display/reporting.

    Extends PluginInfo with adapter-specific metadata like DEFAULT_LOCATIONS.
    """

    name: str
    origin: str  # "builtin", "dropin", "entrypoint"
    locations: list[str]
    source_path: str | None = None  # For drop-in, the .py file path
    entrypoint: str | None = None  # For entry points, the entry point name


def plugin_to_adapter_info(plugin: PluginInfo) -> AdapterInfo:
    """Convert a PluginInfo to an AdapterInfo with adapter-specific metadata.

    Args:
        plugin: Plugin information from the registry.

    Returns:
        AdapterInfo with locations extracted from the adapter module.
    """
    locations = getattr(plugin.module, "DEFAULT_LOCATIONS", [])
    return AdapterInfo(
        name=plugin.name,
        origin=plugin.origin,
        locations=locations,
        source_path=str(plugin.source_path) if plugin.source_path else None,
        entrypoint=plugin.entrypoint,
    )


def list_adapters(*, dropin_path: Path | None = None) -> list[AdapterInfo]:
    """List all discovered adapters from all sources.

    Unlike load_all_adapters(), this function:
    - Returns metadata instead of modules
    - Does NOT deduplicate (shows all sources)
    - Includes adapter-specific info like DEFAULT_LOCATIONS

    Args:
        dropin_path: Custom drop-in directory. Uses default if not specified.

    Returns:
        List of AdapterInfo for all discovered adapters.
    """
    if dropin_path is None:
        dropin_path = adapters_dir()

    result: list[AdapterInfo] = []

    # Built-in adapters
    for plugin in load_builtin_adapters():
        result.append(plugin_to_adapter_info(plugin))

    # Drop-in adapters
    for plugin in load_dropin_adapters(dropin_path):
        result.append(plugin_to_adapter_info(plugin))

    # Entry point adapters
    for plugin in load_entrypoint_adapters():
        result.append(plugin_to_adapter_info(plugin))

    return result


def list_builtin_adapters() -> list[str]:
    """Return names of built-in adapters (for copy command).

    Returns:
        List of adapter names that can be copied.
    """
    return list(_BUILTIN_ADAPTERS.keys())
