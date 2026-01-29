"""Output formatters for search results."""

from siftd.output.formatters import (
    ChunkListFormatter,
    ContextFormatter,
    ConversationFormatter,
    FormatterContext,
    FullExchangeFormatter,
    JsonFormatter,
    OutputFormatter,
    ThreadFormatter,
    VerboseFormatter,
    format_refs_annotation,
    print_refs_content,
    select_formatter,
)
from siftd.output.registry import (
    FormatterRegistry,
    get_formatter,
    get_registry,
)

__all__ = [
    # Protocol
    "OutputFormatter",
    # Context
    "FormatterContext",
    # Built-in formatters
    "ChunkListFormatter",
    "VerboseFormatter",
    "FullExchangeFormatter",
    "ContextFormatter",
    "ThreadFormatter",
    "ConversationFormatter",
    "JsonFormatter",
    # Selection
    "select_formatter",
    # Registry
    "FormatterRegistry",
    "get_formatter",
    "get_registry",
    # Utilities
    "format_refs_annotation",
    "print_refs_content",
]
