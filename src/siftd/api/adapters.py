"""Adapter discovery API."""

import importlib.metadata
import importlib.util
from dataclasses import dataclass
from pathlib import Path

from siftd.adapters import aider, claude_code, codex_cli, gemini_cli
from siftd.paths import adapters_dir

# Built-in adapters with their source modules (for copying)
_BUILTIN_ADAPTERS = {
    "aider": aider,
    "claude_code": claude_code,
    "codex_cli": codex_cli,
    "gemini_cli": gemini_cli,
}


@dataclass
class AdapterInfo:
    """Information about a discovered adapter."""

    name: str
    source: str  # "built-in", "drop-in", "entrypoint"
    locations: list[str]
    file_path: str | None = None  # For drop-in, the .py file path


def list_adapters(*, dropin_path: Path | None = None) -> list[AdapterInfo]:
    """List all discovered adapters from all sources.

    Unlike load_all_adapters(), this function:
    - Returns metadata instead of modules
    - Does NOT deduplicate (shows all sources)
    - Includes invalid adapters with error info (future)

    Args:
        dropin_path: Custom drop-in directory. Uses default if not specified.

    Returns:
        List of AdapterInfo for all discovered adapters.
    """
    if dropin_path is None:
        dropin_path = adapters_dir()

    result: list[AdapterInfo] = []

    # Built-in adapters
    for name, module in _BUILTIN_ADAPTERS.items():
        locations = getattr(module, "DEFAULT_LOCATIONS", [])
        result.append(
            AdapterInfo(
                name=name,
                source="built-in",
                locations=locations,
            )
        )

    # Drop-in adapters
    if dropin_path.is_dir():
        for py_file in sorted(dropin_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            module_name = f"siftd_dropin_adapter_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                name = getattr(module, "NAME", py_file.stem)
                locations = getattr(module, "DEFAULT_LOCATIONS", [])
                result.append(
                    AdapterInfo(
                        name=name,
                        source="drop-in",
                        locations=locations,
                        file_path=str(py_file),
                    )
                )
            except Exception:
                # Include broken adapters with just the filename as name
                result.append(
                    AdapterInfo(
                        name=py_file.stem,
                        source="drop-in",
                        locations=[],
                        file_path=str(py_file),
                    )
                )

    # Entry point adapters
    eps = importlib.metadata.entry_points(group="siftd.adapters")
    for ep in eps:
        try:
            module = ep.load()
            name = getattr(module, "NAME", ep.name)
            locations = getattr(module, "DEFAULT_LOCATIONS", [])
            result.append(
                AdapterInfo(
                    name=name,
                    source="entrypoint",
                    locations=locations,
                )
            )
        except Exception:
            result.append(
                AdapterInfo(
                    name=ep.name,
                    source="entrypoint",
                    locations=[],
                )
            )

    return result


def list_builtin_adapters() -> list[str]:
    """Return names of built-in adapters (for copy command).

    Returns:
        List of adapter names that can be copied.
    """
    return list(_BUILTIN_ADAPTERS.keys())
