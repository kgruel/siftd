"""Public validation utilities for adapter modules.

Provides the same validation logic used by AdapterRegistry,
exposed for external tools (SDK, doctor) to validate adapters
without importing private registry internals.
"""

import inspect
from types import ModuleType

# Current adapter interface version
ADAPTER_INTERFACE_VERSION = 1

# Required module-level attributes for a valid adapter
REQUIRED_ATTRS = {
    "ADAPTER_INTERFACE_VERSION": int,
    "NAME": str,
    "DEFAULT_LOCATIONS": list,
    "DEDUP_STRATEGY": str,
    "HARNESS_SOURCE": str,
}

# Required callable attributes
REQUIRED_CALLABLES = ["discover", "can_handle", "parse"]

# Valid dedup strategies
VALID_DEDUP_STRATEGIES = {"file", "session"}

# Valid support tiers (optional SUPPORT_TIER attribute; defaults to "contrib")
VALID_SUPPORT_TIERS = {"core", "contrib", "frozen"}

# Default tier when an adapter doesn't declare SUPPORT_TIER (drop-ins, old adapters)
DEFAULT_SUPPORT_TIER = "contrib"


def support_tier(module: ModuleType) -> str:
    """An adapter module's declared SUPPORT_TIER, defaulted for legacy/drop-ins."""
    return getattr(module, "SUPPORT_TIER", DEFAULT_SUPPORT_TIER)


def validate_adapter(module: ModuleType, origin: str = "adapter") -> str | None:
    """Validate an adapter module has the required interface.

    This is the public validation function. Use this instead of importing
    private validators from the registry.

    Args:
        module: The loaded adapter module to validate.
        origin: Human-readable origin string for error messages.

    Returns:
        Error message string if invalid, None if valid.

    Example:
        import my_adapter
        error = validate_adapter(my_adapter)
        if error:
            print(f"Invalid adapter: {error}")
    """
    for attr, expected_type in REQUIRED_ATTRS.items():
        if not hasattr(module, attr):
            return f"{origin}: missing required attribute '{attr}'"
        value = getattr(module, attr)
        if not isinstance(value, expected_type):
            return f"{origin}: '{attr}' must be {expected_type.__name__}, got {type(value).__name__}"

    adapter_version = getattr(module, "ADAPTER_INTERFACE_VERSION")
    if adapter_version != ADAPTER_INTERFACE_VERSION:
        return f"{origin}: incompatible interface version {adapter_version}, expected {ADAPTER_INTERFACE_VERSION}"

    if module.DEDUP_STRATEGY not in VALID_DEDUP_STRATEGIES:
        return f"{origin}: DEDUP_STRATEGY must be 'file' or 'session', got '{module.DEDUP_STRATEGY}'"

    # SUPPORT_TIER is optional (defaults to "contrib") but must be valid when present
    tier = getattr(module, "SUPPORT_TIER", None)
    if tier is not None and tier not in VALID_SUPPORT_TIERS:
        return f"{origin}: SUPPORT_TIER must be 'core', 'contrib', or 'frozen', got '{tier}'"

    for func_name in REQUIRED_CALLABLES:
        if not hasattr(module, func_name) or not callable(getattr(module, func_name)):
            return f"{origin}: missing required function '{func_name}'"

    # Validate discover() accepts locations= keyword argument
    discover_func = getattr(module, "discover")
    sig = inspect.signature(discover_func)
    if "locations" not in sig.parameters:
        return f"{origin}: discover() must accept 'locations' keyword argument"

    # Validate can_handle(source) and parse(source) signatures
    for func_name in ("can_handle", "parse"):
        func = getattr(module, func_name)
        sig = inspect.signature(func)
        if "source" not in sig.parameters:
            return f"{origin}: {func_name}() must accept a 'source' positional argument"

    return None
