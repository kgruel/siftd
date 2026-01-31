"""Plugin discovery: shared utilities for loading drop-in and entry point plugins."""

import ast
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

# Type alias for validator functions
Validator = Callable[[ModuleType, str], str | None]

# Type alias for plugin origins
PluginOrigin = Literal["builtin", "dropin", "entrypoint"]


@dataclass
class PluginInfo:
    """Unified provenance information for discovered plugins.

    Attributes:
        name: Human-readable name of the plugin.
        origin: How the plugin was discovered ("builtin", "dropin", "entrypoint").
        module: The loaded module object.
        source_path: For drop-ins, the path to the .py file. None otherwise.
        entrypoint: For entry points, the entry point name. None otherwise.
    """

    name: str
    origin: PluginOrigin
    module: ModuleType
    source_path: Path | None = None
    entrypoint: str | None = None


def validate_required_interface(
    module: ModuleType,
    origin: str,
    required_attrs: dict[str, type],
    required_callables: list[str],
) -> str | None:
    """Validate a module has required attributes and callables.

    Args:
        module: The module to validate.
        origin: Human-readable origin string for error messages.
        required_attrs: Dict mapping attribute name to expected type.
        required_callables: List of required callable attribute names.

    Returns:
        Error message string if invalid, None if valid.
    """
    errors = []

    for attr, expected_type in required_attrs.items():
        if not hasattr(module, attr):
            errors.append(f"missing '{attr}'")
        elif not isinstance(getattr(module, attr), expected_type):
            actual = type(getattr(module, attr)).__name__
            errors.append(f"'{attr}' wrong type (expected {expected_type.__name__}, got {actual})")

    for func_name in required_callables:
        if not hasattr(module, func_name):
            errors.append(f"missing function '{func_name}'")
        elif not callable(getattr(module, func_name)):
            errors.append(f"'{func_name}' is not callable")

    if errors:
        return f"{origin}: {', '.join(errors)}"
    return None


def load_dropin_modules(
    path: Path,
    module_name_prefix: str,
    validate: Validator,
    *,
    get_name: Callable[[ModuleType], str] | None = None,
) -> list[PluginInfo]:
    """Load .py files from a directory as drop-in plugin modules.

    Args:
        path: Directory to scan for .py files.
        module_name_prefix: Prefix for generated module names (e.g., "siftd_dropin_adapter_").
        validate: Validation function that returns error string or None.
        get_name: Optional function to extract plugin name from module. Defaults to file stem.

    Returns:
        List of PluginInfo for successfully loaded and validated modules.
    """
    if not path.is_dir():
        return []

    plugins: list[PluginInfo] = []

    for py_file in sorted(path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        module_name = f"{module_name_prefix}{py_file.stem}"
        origin = f"drop-in {py_file.name}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                print(f"Warning: {origin}: could not create module spec", file=sys.stderr)
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            error = validate(module, origin)
            if error:
                print(f"Warning: {error}", file=sys.stderr)
                continue

            name = get_name(module) if get_name else py_file.stem
            plugins.append(
                PluginInfo(
                    name=name,
                    origin="dropin",
                    module=module,
                    source_path=py_file,
                )
            )

        except Exception as e:
            print(f"Warning: {origin}: import failed: {e}", file=sys.stderr)

    return plugins


def load_entrypoint_modules(
    group: str,
    validate: Validator,
    *,
    get_name: Callable[[ModuleType], str] | None = None,
) -> list[PluginInfo]:
    """Load plugin modules registered via entry points.

    Args:
        group: Entry point group name (e.g., "siftd.adapters").
        validate: Validation function that returns error string or None.
        get_name: Optional function to extract plugin name from module. Defaults to entry point name.

    Returns:
        List of PluginInfo for successfully loaded and validated modules.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return []

    plugins: list[PluginInfo] = []

    # Python 3.10+ returns SelectableGroups, earlier returns dict
    eps = entry_points()
    if hasattr(eps, "select"):
        group_eps = eps.select(group=group)
    else:
        group_eps = eps.get(group, [])

    for ep in group_eps:
        origin = f"entry point {ep.name}"
        try:
            module = ep.load()
            error = validate(module, origin)
            if error:
                print(f"Warning: {error}", file=sys.stderr)
                continue

            name = get_name(module) if get_name else ep.name
            plugins.append(
                PluginInfo(
                    name=name,
                    origin="entrypoint",
                    module=module,
                    entrypoint=ep.name,
                )
            )
        except Exception as e:
            print(f"Warning: {origin}: load failed: {e}", file=sys.stderr)

    return plugins


def validate_dropin_module(
    py_file: Path,
    module_name_prefix: str,
    validate: Validator,
) -> tuple[ModuleType | None, list[str]]:
    """Validate a single drop-in module file, returning all errors.

    Unlike load_dropin_modules which prints warnings and skips invalid modules,
    this function returns detailed error information for diagnostics.

    Args:
        py_file: Path to the .py file.
        module_name_prefix: Prefix for generated module name.
        validate: Validation function that returns error string or None.

    Returns:
        Tuple of (module or None, list of error strings).
        If module is returned, errors list is empty.
        If module is None, errors list contains the failure reasons.
    """
    module_name = f"{module_name_prefix}{py_file.stem}"
    origin = f"drop-in {py_file.name}"
    errors = []

    try:
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            return None, [f"{origin}: could not create module spec"]

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        error = validate(module, origin)
        if error:
            # Parse errors from validate message (format: "origin: err1, err2")
            # Extract just the error part after the origin prefix
            if ": " in error:
                error_part = error.split(": ", 1)[1]
                errors = [e.strip() for e in error_part.split(", ")]
            else:
                errors = [error]
            return None, errors

        return module, []

    except Exception as e:
        return None, [f"import failed: {e}"]


def validate_dropin_ast(
    py_file: Path,
    required_names: list[str],
) -> list[str]:
    """Validate a drop-in module has required names using AST parsing only.

    This is a side-effect-free validation that doesn't import/execute the module.
    Useful for doctor checks to validate plugins without running arbitrary code.

    Args:
        py_file: Path to the .py file.
        required_names: List of required module-level names (attributes or functions).

    Returns:
        List of error strings. Empty list if all required names are present.
    """
    try:
        source = py_file.read_text()
    except Exception as e:
        return [f"read failed: {e}"]

    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError as e:
        return [f"syntax error: {e.msg} (line {e.lineno})"]

    # Collect all module-level names (assignments, function defs, class defs)
    defined_names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined_names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_names.add(node.name)

    # Check for missing required names
    missing = [name for name in required_names if name not in defined_names]
    if missing:
        return [f"missing '{name}'" for name in missing]

    return []
