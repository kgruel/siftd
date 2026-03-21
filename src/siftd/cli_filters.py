"""Shared CLI filter arguments for conversation commands.

Provides a single definition of the standard filter vocabulary
(workspace, model, since/before, tags, tool, search) that is
composed into query, search, and export parsers.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    tags: list[str] | None = None
    all_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    tool: str | None = None
    tool_tag: str | None = None
    search: str | None = None


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
    (workspace, since, before, tags, exclude_tags).
    """
    import argparse as _ap

    from siftd.dateparse import parse_date

    def _date_arg(value: str) -> str | None:
        try:
            return parse_date(value)
        except ValueError as e:
            raise _ap.ArgumentTypeError(str(e)) from e

    filter_group = parser.add_argument_group("filtering")
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
        "--since", metavar="DATE", type=_date_arg,
        help="Conversations started after this date (YYYY-MM-DD, 7d, 1w, yesterday, today)",
    )
    filter_group.add_argument(
        "--before", metavar="DATE", type=_date_arg,
        help="Conversations started before this date (YYYY-MM-DD, 7d, 1w, yesterday, today)",
    )

    tag_group = parser.add_argument_group("tag filtering")
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


    @classmethod
    def from_query_params(cls, params: dict) -> FilterArgs:
        """Build FilterArgs from HTTP query parameters.

        Normalizes HTTP quirks: empty strings → None, string lists
        from doseq encoding, etc.
        """

        def _str_or_none(v: object) -> str | None:
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None

        def _str_list_or_none(v: object) -> list[str] | None:
            if v is None:
                return None
            if isinstance(v, list):
                result = [str(x).strip() for x in v if str(x).strip()]
                return result if result else None
            s = str(v).strip()
            return [s] if s else None

        return cls(
            workspace=_str_or_none(params.get("workspace")),
            model=_str_or_none(params.get("model")),
            since=_str_or_none(params.get("since")),
            before=_str_or_none(params.get("before")),
            tags=_str_list_or_none(params.get("tag")),
            all_tags=_str_list_or_none(params.get("all_tags")),
            exclude_tags=_str_list_or_none(params.get("no_tag")),
            tool=_str_or_none(params.get("tool")),
            tool_tag=_str_or_none(params.get("tool_tag")),
            search=_str_or_none(params.get("search")),
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
        tags=getattr(args, "tag", None),
        all_tags=getattr(args, "all_tags", None),
        exclude_tags=getattr(args, "no_tag", None),
        tool=getattr(args, "tool", None),
        tool_tag=getattr(args, "tool_tag", None),
        search=getattr(args, "search", None),
    )
