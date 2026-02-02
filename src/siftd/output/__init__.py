"""Output formatters for search results."""

from siftd.output.common import (
    fmt_ago,
    fmt_model,
    fmt_timestamp,
    fmt_tokens,
    fmt_workspace,
    print_indented,
    truncate_text,
)
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
    # Utilities (search-specific)
    "format_refs_annotation",
    "print_refs_content",
    # Utilities (common formatting)
    "fmt_tokens",
    "fmt_workspace",
    "fmt_ago",
    "fmt_timestamp",
    "fmt_model",
    "truncate_text",
    "print_indented",
]
