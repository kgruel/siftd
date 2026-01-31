"""Public validation utilities for formatter modules.

Provides the same validation logic used by FormatterRegistry,
exposed for external tools (SDK, doctor) to validate formatters
without importing private registry internals.
"""

from types import ModuleType

from siftd.plugin_discovery import validate_required_interface

# Required module-level attributes for a valid formatter module
REQUIRED_ATTRS = {
    "NAME": str,
}

# Required callable attributes
REQUIRED_CALLABLES = ["create_formatter"]


def validate_formatter(module: ModuleType, origin: str = "formatter") -> str | None:
    """Validate a formatter module has the required interface.

    This is the public validation function. Use this instead of importing
    private validators from the registry.

    Args:
        module: The loaded formatter module to validate.
        origin: Human-readable origin string for error messages.

    Returns:
        Error message string if invalid, None if valid.

    Example:
        import my_formatter
        error = validate_formatter(my_formatter)
        if error:
            print(f"Invalid formatter: {error}")
    """
    return validate_required_interface(
        module, origin, REQUIRED_ATTRS, REQUIRED_CALLABLES
    )
