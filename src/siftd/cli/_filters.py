"""Shared CLI filter arguments for conversation commands.

Provides a single definition of the standard filter vocabulary
(workspace, model, since/before, tags, tool, search) that is
composed into query, search, and export parsers.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from siftd.dateparse import DATE_VOCABULARY, parse_date


def date_arg(value: str) -> str | None:
    """`parse_date` as an argparse ``type=``, preserving its message.

    argparse swallows a bare ``ValueError`` from a type callable and reports a
    generic "invalid date_arg value", so the vocabulary hint never reaches the
    user. ``ArgumentTypeError`` is the one it prints verbatim.
    """
    try:
        return parse_date(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


@dataclass
class FilterArgs:
    """Typed container for extracted filter values.

    Bridge between argparse namespace and API layer.
    Not coupled to WhereBuilder — callers unpack fields as kwargs.
    """

    workspace: str | None = None
    model: str | None = None
    since: str | None = None
    before: str | None = None
    tag: list[str] | None = None
    all_tags: list[str] | None = None
    no_tag: list[str] | None = None
    tag_kind: list[str] | None = None
    tool: str | None = None
    tool_tag: str | None = None
    search: str | None = None
    owner: str | None = None


def add_filter_args(
    parser,
    *,
    include_model: bool = True,
    include_tool: bool = False,
    include_tool_tag: bool = False,
    include_search: bool = False,
    include_all_tags: bool = True,
) -> None:
    """Add the standard filter argument group to a parser.

    Callers opt in to extensions beyond the base set
    (workspace, since, before, tag, no_tag).
    """
    # One "filters" group: which conversations to match — workspace/model/date/
    # tool/owner and the tag predicates together. (Tag filters used to be a
    # separate "tag filtering" group; merged so the help reads as one purpose.)
    filter_group = parser.add_argument_group("filters")
    filter_group.add_argument(
        "-w", "--workspace", metavar="SUBSTR",
        help="Filter by workspace path substring",
    )
    if include_model:
        filter_group.add_argument(
            "-m", "--model", metavar="NAME",
            help="Filter by model name",
        )
    filter_group.add_argument(
        "--since", metavar="DATE", type=date_arg,
        help=f"Conversations after this date ({DATE_VOCABULARY})",
    )
    filter_group.add_argument(
        "--before", metavar="DATE", type=date_arg,
        help=f"Conversations before this date ({DATE_VOCABULARY})",
    )

    tag_group = filter_group  # tag predicates live in the same "filters" group
    tag_group.add_argument(
        "-l", "--tag", action="append", metavar="NAME",
        help="Filter by tag (repeatable, OR logic)",
    )
    if include_all_tags:
        tag_group.add_argument(
            "--all-tags", action="append", metavar="NAME",
            help="Require all specified tags (AND logic)",
        )
    tag_group.add_argument(
        "--no-tag", action="append", metavar="NAME",
        help="Exclude conversations with this tag (NOT logic)",
    )
    tag_group.add_argument(
        "--on", action="append", metavar="KIND",
        choices=["conversation", "prompt", "response", "tool_call", "exchange", "block"],
        dest="tag_kind",
        help=(
            "Scope tag filters to a specific target kind (repeatable). "
            "Default: match tags on any kind (conversation, prompt, response, tool_call, exchange)."
        ),
    )
    if include_tool:
        filter_group.add_argument(
            "-t", "--tool", metavar="NAME",
            help="Filter by canonical tool name (e.g. shell.execute)",
        )
    if include_tool_tag:
        tag_group.add_argument(
            "--tool-tag", metavar="NAME",
            help="Filter by tool call tag (e.g. shell:test)",
        )
    if include_search:
        filter_group.add_argument(
            "-s", "--search", metavar="QUERY",
            help="Full-text search filter",
        )
    filter_group.add_argument(
        "--owner", metavar="USER",
        help="Filter to conversations owned by this user",
    )


def extract_filter_args(args) -> FilterArgs:
    """Pull filter values from a parsed argparse namespace.

    Uses getattr for optional fields that may not be present
    depending on which add_filter_args options were enabled.
    """
    return FilterArgs(
        workspace=getattr(args, "workspace", None),
        model=getattr(args, "model", None),
        since=getattr(args, "since", None),
        before=getattr(args, "before", None),
        tag=getattr(args, "tag", None),
        all_tags=getattr(args, "all_tags", None),
        no_tag=getattr(args, "no_tag", None),
        tag_kind=getattr(args, "tag_kind", None),
        tool=getattr(args, "tool", None),
        tool_tag=getattr(args, "tool_tag", None),
        search=getattr(args, "search", None),
        owner=getattr(args, "owner", None),
    )
