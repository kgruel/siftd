"""Output format registry: discovers built-in, drop-in, and entry point output formatters.

Output formatters handle rendering conversation data to different media types
(terminal, markdown, JSON, HTML, etc.). Each formatter implements render methods
for the content types it supports (detail views, list views, search results).

This replaces the former search-specific FormatterRegistry that lived in
registry.py — the two systems were unified in 0.6.0.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from siftd.output.validation import validate_formatter
from siftd.plugin_discovery import PluginInfo, load_all_extensions

if TYPE_CHECKING:
    from painted import Fidelity


@runtime_checkable
class OutputFormat(Protocol):
    """Protocol for output format modules.

    Required attributes:
        name: str — format identifier (e.g., "terminal", "markdown", "json")
        media_type: str — MIME-like type ("terminal", "text/markdown", "application/json")

    Required methods:
        render_detail(turns, fidelity, **ctx) -> Any
            Render conversation detail. Return type varies by format:
            terminal returns a painted Block, markdown returns str, json returns dict.
            Callers dispatch on isinstance(result, str/dict) with Block as fallback.
        render_list(summaries, fidelity, **ctx) -> str | dict
            Render conversation list.
        render_search(results, fidelity, **ctx) -> str | dict
            Render search results.

    All built-in formatters implement all three render methods. Drop-in
    formatters that omit render_list or render_search will raise AttributeError
    when the corresponding CLI command invokes them.
    """

    name: str
    media_type: str

    def render_detail(self, result: Any, fidelity: Fidelity, **context: Any) -> Any: ...


def _get_name(module: ModuleType) -> str:
    return getattr(module, "name", "unknown")


def _load_builtin_formats() -> list[PluginInfo]:
    """Load built-in output format modules."""
    from siftd.output import html_fmt, json_fmt, markdown_fmt, terminal_fmt

    builtins = [terminal_fmt, markdown_fmt, json_fmt, html_fmt]
    return [
        PluginInfo(
            name=getattr(m, "name", m.__name__.split(".")[-1]),
            origin="builtin",
            module=m,
        )
        for m in builtins
    ]


def load_all_formats(dropin_path: Path | None = None) -> list[PluginInfo]:
    """Load output formats from all sources, deduplicated by name."""
    from siftd.paths import formatters_dir

    if dropin_path is None:
        dropin_path = formatters_dir()

    return load_all_extensions(
        builtins=_load_builtin_formats(),
        dropin_path=dropin_path,
        dropin_prefix="siftd_dropin_formatter_",
        entrypoint_group="siftd.formatters",
        validate=validate_formatter,
        get_name=_get_name,
    )


# Lazily initialized format lookup
_formats: dict[str, ModuleType] | None = None


def _ensure_loaded() -> dict[str, ModuleType]:
    global _formats
    if _formats is None:
        _formats = {p.name: p.module for p in load_all_formats()}
    return _formats


def get_format(name: str) -> ModuleType | None:
    """Get an output format module by name."""
    return _ensure_loaded().get(name)


def list_format_names() -> list[str]:
    """List all available output format names."""
    return sorted(_ensure_loaded().keys())


def select_format(
    *,
    name: str | None = None,
    is_tty: bool = True,
    json_mode: bool = False,
) -> ModuleType:
    """Select the appropriate output format.

    Args:
        name: Explicit format name (--format flag). Takes priority.
        is_tty: Whether stdout is a terminal.
        json_mode: Whether --json was specified.

    Returns:
        The selected output format module.

    Raises:
        ValueError: If the requested format is not found.
    """
    if name:
        fmt = get_format(name)
        if fmt is None:
            available = ", ".join(list_format_names())
            raise ValueError(f"Unknown format '{name}'. Available: {available}")
        return fmt

    if json_mode:
        fmt = get_format("json")
        if fmt:
            return fmt

    if not is_tty:
        fmt = get_format("markdown")
        if fmt:
            return fmt

    fmt = get_format("terminal")
    if fmt:
        return fmt

    # Fallback: first available
    formats = _ensure_loaded()
    if formats:
        return next(iter(formats.values()))
    raise ValueError("No output formats available")
