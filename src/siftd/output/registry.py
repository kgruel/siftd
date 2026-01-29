"""Formatter registry: discovers built-in, drop-in, and entry point formatters."""

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

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
    for attr, expected_type in _REQUIRED_ATTRS.items():
        if not hasattr(module, attr):
            return f"{origin}: missing required attribute '{attr}'"
        value = getattr(module, attr)
        if not isinstance(value, expected_type):
            return f"{origin}: '{attr}' must be {expected_type.__name__}, got {type(value).__name__}"

    for func_name in _REQUIRED_CALLABLES:
        if not hasattr(module, func_name) or not callable(getattr(module, func_name)):
            return f"{origin}: missing required function '{func_name}'"

    return None


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
    formatters: dict[str, ModuleType] = {}
    if not path.is_dir():
        return formatters

    for py_file in sorted(path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"siftd_dropin_formatter_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                print(f"Warning: could not load drop-in formatter {py_file.name}", file=sys.stderr)
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            print(f"Warning: failed to import drop-in formatter {py_file.name}: {e}", file=sys.stderr)
            continue

        error = _validate_formatter(module, f"drop-in {py_file.name}")
        if error:
            print(f"Warning: {error}", file=sys.stderr)
            continue

        formatters[module.NAME] = module

    return formatters


def load_entrypoint_formatters() -> dict[str, ModuleType]:
    """Discover formatters registered via the 'siftd.formatters' entry point group."""
    formatters: dict[str, ModuleType] = {}
    eps = importlib.metadata.entry_points(group="siftd.formatters")

    for ep in eps:
        try:
            module = ep.load()
        except Exception as e:
            print(f"Warning: failed to load entry point formatter '{ep.name}': {e}", file=sys.stderr)
            continue

        error = _validate_formatter(module, f"entry point '{ep.name}'")
        if error:
            print(f"Warning: {error}", file=sys.stderr)
            continue

        name = getattr(module, "NAME", ep.name)
        formatters[name] = module

    return formatters


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
