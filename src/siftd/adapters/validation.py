"""The adapter contract, and its enforcement.

This module is the one definition of what an adapter module must export. The
registry, ``siftd doctor`` (drop-in check) and the architecture tests all read
it; nothing else lists the required names.

Two interface versions are accepted:

- **Version 2 (decoding).** The adapter exports ``read(source)`` and
  ``decoder(source)``; the framework derives ``parse()`` and the peek hooks from
  them (:mod:`siftd.adapters.contract`), so ingest and peek classify each record
  once and every record is accounted for in a ledger.
- **Version 1 (legacy).** The adapter exports ``parse(source)`` and builds its
  own conversations. It keeps working unchanged, but there is no records tree to
  measure, so it has no ledger. Drop-ins written before version 2 are version 1.

Optional hooks are part of the contract too: when present they are validated
(:data:`OPTIONAL_ATTRS`, :data:`OPTIONAL_CALLABLES`), because a drop-in that
misspells one would otherwise lose the behavior silently.
"""

import inspect
from types import ModuleType

#: The interface a new adapter is written against: decode, and let the framework assemble.
DECODING_VERSION = 2

#: The interface adapters wrote before decoding existed: ``parse()`` builds the tree.
LEGACY_VERSION = 1

SUPPORTED_VERSIONS = frozenset({LEGACY_VERSION, DECODING_VERSION})

#: Compatibility alias: before version 2 this was *the* interface version, and
#: drop-ins written then import it (``from siftd.adapters.validation import
#: ADAPTER_INTERFACE_VERSION``) to declare theirs. It keeps its old value, the
#: legacy interface, so those drop-ins still load as version 1. Retired with
#: version 1 (``api/compat.py``, ``V1_INTERFACE_REMOVAL``); new code names
#: ``LEGACY_VERSION`` or ``DECODING_VERSION``.
ADAPTER_INTERFACE_VERSION = LEGACY_VERSION

# Required module-level attributes for a valid adapter, in every version
REQUIRED_ATTRS = {
    "ADAPTER_INTERFACE_VERSION": int,
    "NAME": str,
    "DEFAULT_LOCATIONS": list,
    "DEDUP_STRATEGY": str,
    "HARNESS_SOURCE": str,
}

#: Required callables by interface version. ``discover`` and ``can_handle`` say
#: which sources are the adapter's; the rest is how a source becomes conversations.
REQUIRED_CALLABLES_BY_VERSION = {
    LEGACY_VERSION: ["discover", "can_handle", "parse"],
    DECODING_VERSION: ["discover", "can_handle", "read", "decoder"],
}

#: Callables whose first parameter is the source, and which are checked for it.
SOURCE_CALLABLES = frozenset({"can_handle", "parse", "read", "decoder", "default_session", "peek_read", "change_paths"})

# Optional module-level attributes: type when present. A bool or list is checked
# by ``_check_optional_attr`` so an element type can be named.
OPTIONAL_ATTRS: dict[str, type] = {
    "SUPPORT_TIER": str,
    "HARNESS_LOG_FORMAT": str,
    "HARNESS_DISPLAY_NAME": str,
    "SUPPORTS_LIVE_REGISTRATION": bool,
    "SUBAGENT_PATH_MARKER": str,
    "PEEK_GLOB_PATTERNS": list,
    "SESSION_ID_IN_PATH": bool,
    "TOOL_HINT_KEYS": dict,
    "TOOL_ALIASES": dict,
    "SUPPORTS_PEEK": bool,
    "PEEK_LAST_OF_MANY": bool,
}

# Optional callables: validated for being callable, and for the source parameter
# where they take one. ``peek_enrich(path, scan)`` takes a path, not a source.
OPTIONAL_CALLABLES = [
    "change_paths",
    "default_session",
    "peek_read",
    "peek_enrich",
    "peek_scan",
    "peek_exchanges",
    "peek_tail",
]

# Valid dedup strategies
VALID_DEDUP_STRATEGIES = {"file", "session"}

# Valid support tiers (optional SUPPORT_TIER attribute; defaults to "contrib")
VALID_SUPPORT_TIERS = {"core", "contrib", "frozen"}

# Default tier when an adapter doesn't declare SUPPORT_TIER (drop-ins, old adapters)
DEFAULT_SUPPORT_TIER = "contrib"


def support_tier(module: ModuleType) -> str:
    """An adapter module's declared SUPPORT_TIER, defaulted for legacy/drop-ins."""
    return getattr(module, "SUPPORT_TIER", DEFAULT_SUPPORT_TIER)


def required_names(version: int = LEGACY_VERSION) -> list[str]:
    """Every module-level name an adapter of ``version`` must define.

    The AST pre-check ``siftd doctor`` runs on a drop-in file (which cannot import
    it yet) and the architecture tests use this, so there is one list.
    """
    return [*REQUIRED_ATTRS, *REQUIRED_CALLABLES_BY_VERSION[version]]


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
    if adapter_version not in SUPPORTED_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_VERSIONS))
        return f"{origin}: incompatible interface version {adapter_version}, expected one of {supported}"

    if module.DEDUP_STRATEGY not in VALID_DEDUP_STRATEGIES:
        return f"{origin}: DEDUP_STRATEGY must be 'file' or 'session', got '{module.DEDUP_STRATEGY}'"

    # SUPPORT_TIER is optional (defaults to "contrib") but must be valid when present
    tier = getattr(module, "SUPPORT_TIER", None)
    if tier is not None and tier not in VALID_SUPPORT_TIERS:
        return f"{origin}: SUPPORT_TIER must be 'core', 'contrib', or 'frozen', got '{tier}'"

    for func_name in REQUIRED_CALLABLES_BY_VERSION[adapter_version]:
        if not hasattr(module, func_name) or not callable(getattr(module, func_name)):
            return f"{origin}: missing required function '{func_name}'"

    # Validate discover() accepts locations= keyword argument
    discover_func = getattr(module, "discover")
    sig = inspect.signature(discover_func)
    if "locations" not in sig.parameters:
        return f"{origin}: discover() must accept 'locations' keyword argument"

    for func_name in REQUIRED_CALLABLES_BY_VERSION[adapter_version]:
        error = _check_source_parameter(module, func_name, origin)
        if error:
            return error

    return _validate_optional(module, origin)


def _check_source_parameter(module: ModuleType, func_name: str, origin: str) -> str | None:
    if func_name not in SOURCE_CALLABLES:
        return None
    if "source" not in inspect.signature(getattr(module, func_name)).parameters:
        return f"{origin}: {func_name}() must accept a 'source' positional argument"
    return None


def _validate_optional(module: ModuleType, origin: str) -> str | None:
    """Check the optional hooks an adapter chose to declare."""
    for attr, expected_type in OPTIONAL_ATTRS.items():
        if not hasattr(module, attr):
            continue
        value = getattr(module, attr)
        if not isinstance(value, expected_type):
            return f"{origin}: '{attr}' must be {expected_type.__name__}, got {type(value).__name__}"

    patterns = getattr(module, "PEEK_GLOB_PATTERNS", [])
    if not all(isinstance(p, str) for p in patterns):
        return f"{origin}: 'PEEK_GLOB_PATTERNS' must be a list of str"

    for attr in ("TOOL_ALIASES", "TOOL_HINT_KEYS"):
        for key, value in getattr(module, attr, {}).items():
            if not isinstance(key, str):
                return f"{origin}: '{attr}' keys must be str, got {type(key).__name__}"
            if attr == "TOOL_ALIASES" and not isinstance(value, str):
                return f"{origin}: 'TOOL_ALIASES' values must be str, got {type(value).__name__}"
            if attr == "TOOL_HINT_KEYS" and not (isinstance(value, list) and all(isinstance(k, str) for k in value)):
                return f"{origin}: 'TOOL_HINT_KEYS' values must be a list of str"

    for func_name in OPTIONAL_CALLABLES:
        if not hasattr(module, func_name):
            continue
        if not callable(getattr(module, func_name)):
            return f"{origin}: '{func_name}' must be callable"
        error = _check_source_parameter(module, func_name, origin)
        if error:
            return error

    return None
