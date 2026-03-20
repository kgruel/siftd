"""Public validation utilities for formatter modules.

Provides validation logic for both built-in and drop-in formatters.
Drop-in formatters are Python modules in ~/.config/siftd/formatters/
that implement the formatter interface.
"""

from types import ModuleType

from siftd.plugin_discovery import validate_required_interface

# Expected formatter interface version.  Each formatter module declares its own
# FORMATTER_INTERFACE_VERSION; validate_formatter() rejects mismatches.  This
# intentional duplication mirrors the adapter versioning pattern — formatters
# and the registry evolve independently, and the check surfaces incompatibility
# at load time rather than at runtime.
FORMATTER_INTERFACE_VERSION = 1

# Required module-level attributes for a valid formatter module
REQUIRED_ATTRS = {
    "FORMATTER_INTERFACE_VERSION": int,
    "name": str,
    "media_type": str,
}

# Required callable attributes
REQUIRED_CALLABLES = ["render_detail"]


def validate_formatter(module: ModuleType, origin: str = "formatter") -> str | None:
    """Validate a formatter module has the required interface.

    Args:
        module: The loaded formatter module to validate.
        origin: Human-readable origin string for error messages.

    Returns:
        Error message string if invalid, None if valid.
    """
    error = validate_required_interface(
        module, origin, REQUIRED_ATTRS, REQUIRED_CALLABLES
    )
    if error:
        return error

    version = getattr(module, "FORMATTER_INTERFACE_VERSION")
    if version != FORMATTER_INTERFACE_VERSION:
        return f"{origin}: incompatible interface version {version}, expected {FORMATTER_INTERFACE_VERSION}"

    return None
