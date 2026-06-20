"""Output formatting: common utilities and format registry."""

from siftd.output.common import (
    fmt_ago,
    fmt_model,
    fmt_timestamp,
    fmt_tokens,
    fmt_workspace,
    format_refs_annotation,
    format_table,
    print_indented,
    print_refs_content,
    term_width,
    truncate_text,
)
from siftd.output.table import (
    Col,
    print_table,
    render_string_table,
    render_table,
)

__all__ = [
    "fmt_tokens",
    "fmt_workspace",
    "fmt_ago",
    "fmt_timestamp",
    "fmt_model",
    "truncate_text",
    "term_width",
    "print_indented",
    "format_table",
    "print_table",
    "render_table",
    "render_string_table",
    "Col",
    "format_refs_annotation",
    "print_refs_content",
]
