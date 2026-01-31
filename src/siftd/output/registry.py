"""Formatter registry: discovers built-in, drop-in, and entry point formatters."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.output.validation import validate_formatter
from siftd.plugin_discovery import (
    PluginInfo,
    load_dropin_modules,
    load_entrypoint_modules,
)

if TYPE_CHECKING:
    from siftd.output.formatters import OutputFormatter

# Type alias for formatter factories
FormatterFactory = Callable[[], "OutputFormatter"]

# Re-export for backwards compatibility (deprecated, use siftd.output.validation)
_validate_formatter = validate_formatter


def load_builtin_factories() -> dict[str, FormatterFactory]:
    """Return factories for built-in formatters, keyed by name."""
    from siftd.output.formatters import (
        ChunkListFormatter,
        ConversationFormatter,
        FullExchangeFormatter,
        JsonFormatter,
        ThreadFormatter,
        VerboseFormatter,
    )

    return {
        "default": ChunkListFormatter,
        "verbose": VerboseFormatter,
        "full": FullExchangeFormatter,
        "thread": ThreadFormatter,
        "conversations": ConversationFormatter,
        "json": JsonFormatter,
        # ContextFormatter is parameterized, handled separately in select_formatter
    }


def load_dropin_formatters(path: Path) -> list[PluginInfo]:
    """Scan a directory for .py formatter files, import and validate them."""
    return load_dropin_modules(
        path,
        module_name_prefix="siftd_dropin_formatter_",
        validate=_validate_formatter,
        get_name=lambda m: getattr(m, "NAME", "unknown"),
    )


def load_entrypoint_formatters() -> list[PluginInfo]:
    """Discover formatters registered via the 'siftd.formatters' entry point group."""
    return load_entrypoint_modules(
        group="siftd.formatters",
        validate=_validate_formatter,
        get_name=lambda m: getattr(m, "NAME", "unknown"),
    )


class FormatterRegistry:
    """Registry for output formatters with plugin discovery."""

    def __init__(self, dropin_path: Path | None = None):
        from siftd.paths import formatters_dir

        if dropin_path is None:
            dropin_path = formatters_dir()

        # Build unified factory dict with priority: drop-in > entry point > built-in
        self._factories: dict[str, FormatterFactory] = {}

        # Start with built-ins (lowest priority)
        self._factories.update(load_builtin_factories())

        # Entry points override built-ins
        for plugin in load_entrypoint_formatters():
            self._factories[plugin.name] = plugin.module.create_formatter

        # Drop-ins have highest priority
        for plugin in load_dropin_formatters(dropin_path):
            self._factories[plugin.name] = plugin.module.create_formatter

    def get(self, name: str) -> "OutputFormatter | None":
        """Get a formatter by name."""
        factory = self._factories.get(name)
        if factory is None:
            return None
        return factory()

    def list_names(self) -> list[str]:
        """List all available formatter names."""
        return sorted(self._factories.keys())


# Module-level singleton, lazily initialized
_registry: FormatterRegistry | None = None


def get_registry() -> FormatterRegistry:
    """Get the global formatter registry (lazily initialized)."""
    global _registry
    if _registry is None:
        _registry = FormatterRegistry()
    return _registry


def get_formatter(name: str) -> "OutputFormatter | None":
    """Get a formatter by name from the global registry."""
    return get_registry().get(name)
