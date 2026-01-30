"""Formatter registry: discovers built-in, drop-in, and entry point formatters."""

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from siftd.plugin_discovery import (
    load_dropin_modules,
    load_entrypoint_modules,
    validate_required_interface,
)

if TYPE_CHECKING:
    from siftd.output.formatters import OutputFormatter

# Required module-level attributes for a valid formatter module
_REQUIRED_ATTRS = {
    "NAME": str,
}

# Required callable attributes
_REQUIRED_CALLABLES = ["create_formatter"]


def _validate_formatter(module: ModuleType, origin: str) -> str | None:
    """Validate a formatter module has the required interface.

    Returns an error message string if invalid, None if valid.
    """
    return validate_required_interface(
        module,
        origin,
        required_attrs=_REQUIRED_ATTRS,
        required_callables=_REQUIRED_CALLABLES,
    )


def load_builtin_formatters() -> dict[str, "OutputFormatter"]:
    """Return the built-in formatter classes, keyed by name."""
    from siftd.output.formatters import (
        ChunkListFormatter,
        ConversationFormatter,
        FullExchangeFormatter,
        JsonFormatter,
        ThreadFormatter,
        VerboseFormatter,
    )

    return {
        "default": ChunkListFormatter(),
        "verbose": VerboseFormatter(),
        "full": FullExchangeFormatter(),
        "thread": ThreadFormatter(),
        "conversations": ConversationFormatter(),
        "json": JsonFormatter(),
        # ContextFormatter is parameterized, handled separately in select_formatter
    }


def load_dropin_formatters(path: Path) -> dict[str, ModuleType]:
    """Scan a directory for .py formatter files, import and validate them."""
    modules = load_dropin_modules(
        path,
        module_name_prefix="siftd_dropin_formatter_",
        validate=_validate_formatter,
    )
    return {m.NAME: m for m in modules}


def load_entrypoint_formatters() -> dict[str, ModuleType]:
    """Discover formatters registered via the 'siftd.formatters' entry point group."""
    modules = load_entrypoint_modules(
        "siftd.formatters",
        validate=_validate_formatter,
    )
    return {m.NAME: m for m in modules}


class FormatterRegistry:
    """Registry for output formatters with plugin discovery."""

    def __init__(self, dropin_path: Path | None = None):
        from siftd.paths import formatters_dir

        if dropin_path is None:
            dropin_path = formatters_dir()

        self._builtin = load_builtin_formatters()
        self._dropin_modules = load_dropin_formatters(dropin_path)
        self._entrypoint_modules = load_entrypoint_formatters()

    def get(self, name: str) -> "OutputFormatter | None":
        """Get a formatter by name.

        Priority: drop-in > entry point > built-in (drop-ins can override built-ins).
        """
        # Drop-in has highest priority (allows overriding built-ins)
        if name in self._dropin_modules:
            module = self._dropin_modules[name]
            return module.create_formatter()

        # Entry point next
        if name in self._entrypoint_modules:
            module = self._entrypoint_modules[name]
            return module.create_formatter()

        # Built-in last
        return self._builtin.get(name)

    def list_names(self) -> list[str]:
        """List all available formatter names."""
        names = set(self._builtin.keys())
        names.update(self._dropin_modules.keys())
        names.update(self._entrypoint_modules.keys())
        return sorted(names)


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
