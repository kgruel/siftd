"""Resource copy API for adapters, queries, and formatters."""

import importlib.resources
import shutil
from pathlib import Path

from siftd.paths import adapters_dir, formatters_dir, queries_dir


class CopyError(Exception):
    """Error copying a resource."""

    pass


def copy_adapter(
    name: str,
    *,
    dest_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Copy a built-in adapter to the config directory for customization.

    Args:
        name: Adapter name (e.g., "claude_code").
        dest_dir: Destination directory. Uses default adapters_dir if not specified.
        force: Overwrite existing file if True.

    Returns:
        Path to the copied file.

    Raises:
        CopyError: If adapter not found, file exists (without force), or copy fails.
    """
    if dest_dir is None:
        dest_dir = adapters_dir()

    # Locate source file in package
    try:
        source_ref = importlib.resources.files("siftd.adapters").joinpath(f"{name}.py")
    except (ModuleNotFoundError, TypeError) as e:
        raise CopyError(f"Cannot locate adapter package: {e}") from e

    if not source_ref.is_file():
        from siftd.api.adapters import list_builtin_adapters

        available = ", ".join(list_builtin_adapters())
        raise CopyError(f"Built-in adapter not found: {name}. Available: {available}")

    dest_path = dest_dir / f"{name}.py"

    if dest_path.exists() and not force:
        raise CopyError(f"File exists: {dest_path}. Use --force to overwrite.")

    # Create destination directory if needed
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy file
    with importlib.resources.as_file(source_ref) as source_path:
        shutil.copy2(source_path, dest_path)

    return dest_path


def copy_query(
    name: str,
    *,
    dest_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Copy a built-in query to the config directory for customization.

    Args:
        name: Query name without .sql extension (e.g., "cost").
        dest_dir: Destination directory. Uses default queries_dir if not specified.
        force: Overwrite existing file if True.

    Returns:
        Path to the copied file.

    Raises:
        CopyError: If query not found, file exists (without force), or copy fails.
    """
    if dest_dir is None:
        dest_dir = queries_dir()

    try:
        source_ref = importlib.resources.files("siftd.builtin_queries").joinpath(
            f"{name}.sql"
        )
    except (ModuleNotFoundError, TypeError) as e:
        raise CopyError(f"Cannot locate built-in queries package: {e}") from e

    if not source_ref.is_file():
        available = ", ".join(list_builtin_queries())
        if available:
            raise CopyError(
                f"Built-in query not found: {name}. Available: {available}"
            )
        else:
            raise CopyError(f"Built-in query not found: {name}. No built-in queries available.")

    dest_path = dest_dir / f"{name}.sql"

    if dest_path.exists() and not force:
        raise CopyError(f"File exists: {dest_path}. Use --force to overwrite.")

    # Create destination directory if needed
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy file
    with importlib.resources.as_file(source_ref) as source_path:
        shutil.copy2(source_path, dest_path)

    return dest_path


def list_builtin_queries() -> list[str]:
    """Return names of built-in queries (for copy command).

    Returns:
        List of query names that can be copied.
    """
    try:
        queries_pkg = importlib.resources.files("siftd.builtin_queries")
        return sorted(
            f.name[:-4]  # Remove .sql extension
            for f in queries_pkg.iterdir()
            if f.name.endswith(".sql") and f.is_file()
        )
    except (ModuleNotFoundError, TypeError):
        return []


def copy_formatter(
    name: str,
    *,
    dest_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Copy a built-in formatter to the config directory for customization.

    Args:
        name: Formatter name (e.g., "terminal", "markdown", "json").
        dest_dir: Destination directory. Uses default formatters_dir if not specified.
        force: Overwrite existing file if True.

    Returns:
        Path to the copied file.

    Raises:
        CopyError: If formatter not found, file exists (without force), or copy fails.
    """
    if dest_dir is None:
        dest_dir = formatters_dir()

    filename = f"{name}_fmt.py" if not name.endswith("_fmt") else f"{name}.py"
    try:
        source_ref = importlib.resources.files("siftd.output").joinpath(filename)
    except (ModuleNotFoundError, TypeError) as e:
        raise CopyError(f"Cannot locate formatter package: {e}") from e

    if not source_ref.is_file():
        available = ", ".join(list_builtin_formatters())
        raise CopyError(f"Built-in formatter not found: {name}. Available: {available}")

    dest_path = dest_dir / filename
    if dest_path.exists() and not force:
        raise CopyError(f"File exists: {dest_path}. Use --force to overwrite.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    with importlib.resources.as_file(source_ref) as source_path:
        shutil.copy2(source_path, dest_path)
    return dest_path


def list_builtin_formatters() -> list[str]:
    """Return names of built-in formatters (for copy command)."""
    return ["terminal", "markdown", "json"]
