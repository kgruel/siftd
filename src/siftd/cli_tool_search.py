"""CLI for tool-oriented search."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from siftd.api.tool_search import group_tool_search_results, search_tool_calls
from siftd.cli_filters import add_filter_args, extract_filter_args
from siftd.output.common import fmt_timestamp, fmt_workspace, truncate_text


def cmd_tool_search(args) -> int:
    query = " ".join(args.query) if args.query else ""
    filters = extract_filter_args(args)
    has_filters = any(v is not None for v in vars(filters).values())
    if not query and not has_filters and not args.rebuild_index:
        print("Usage: siftd tool-search <query>")
        print("       siftd tool-search --rebuild-index")
        return 1

    db = Path(args.db) if args.db else None
    try:
        parsed, results = search_tool_calls(
            query,
            db_path=db,
            limit=args.limit,
            rebuild_index=args.rebuild_index,
            workspace=filters.workspace,
            model=filters.model,
            since=filters.since,
            before=filters.before,
            tags=filters.tags,
            all_tags=filters.all_tags,
            exclude_tags=filters.exclude_tags,
            tool=filters.tool,
            tool_tag=filters.tool_tag,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    groups = group_tool_search_results(results)

    if args.json:
        payload = {
            "query": parsed.raw,
            "fields": parsed.fields,
            "bare_terms": parsed.bare_terms,
            "unknown_fields": parsed.unknown_fields,
            "results": [r.to_dict() for r in results],
        }
        if args.grouped:
            payload["groups"] = [g.to_dict() for g in groups]
        print(json.dumps(payload, indent=2))
        return 0

    if parsed.unknown_fields:
        unknown = ", ".join(sorted(parsed.unknown_fields))
        print(f"Warning: ignoring unknown fields: {unknown}", file=sys.stderr)
        print(file=sys.stderr)

    if not results:
        print("No tool-call matches found.")
        return 0

    if args.grouped:
        for group in groups:
            workspace = _format_workspace_group(group.workspace_path)
            short_conv = group.conversation_id[:12]
            match_label = "match" if group.tool_call_count == 1 else "matches"
            print(f"{workspace}  {short_conv}  {group.tool_call_count} {match_label}")
            for item in _summarize_group_results(group.results):
                print(f"  {item['line']}")
                if args.show_snippets and item["snippet"]:
                    print(f"    {item['snippet']}")
            print()
        return 0

    for r in results:
        head = f"{r.tool_name or 'unknown'}"
        if r.status:
            head += f" [{r.status}]"
        if r.timestamp:
            head += f"  {r.timestamp}"
        print(head)
        print(f"  tool_call: {r.tool_call_id}  conv: {r.conversation_id}")
        if r.workspace_path:
            print(f"  workspace: {r.workspace_path}")
        if r.path:
            print(f"  path: {r.path}")
        if r.command:
            print(f"  cmd: {r.command}")
        if r.pattern:
            print(f"  pattern: {r.pattern}")
        if r.result_snippet:
            print(f"  result: {r.result_snippet}")
        print()

    return 0


def _format_workspace_group(path: str | None) -> str:
    if not path:
        return "(no workspace)"
    leaf = fmt_workspace(path)
    if leaf and leaf != "(root)":
        parent = Path(path).parent.name
        if parent and parent != "/":
            return f"{parent}/{leaf}"
        return leaf
    return path


def _format_compact_match(result) -> str:
    timestamp = fmt_timestamp(result.timestamp)[:10] if result.timestamp else "??????????"
    tool_name = result.tool_name or "unknown"
    status = (result.status or "?").ljust(7)
    subject = _compact_subject(result)
    return f"{timestamp}  {tool_name}  {status}  {subject}"


def _compact_subject(result) -> str:
    if (result.tool_name or "") == "search.grep" and result.pattern:
        path = f"  {_compact_display_path(result.path)}" if result.path else ""
        return truncate_text(f"grep {result.pattern}{path}", 70)
    if result.path:
        return _compact_display_path(result.path)
    if result.command:
        return truncate_text(result.command, 50)
    if result.pattern:
        prefix = "grep " if (result.tool_name or "") == "search.grep" else "pattern "
        return truncate_text(f"{prefix}{result.pattern}", 50)
    if result.arg:
        return truncate_text(result.arg, 50)
    return result.basename or result.command_verb or result.pattern or (result.tool_name or "unknown")


def _compact_snippet(result) -> str | None:
    if not result.result_snippet:
        return None
    snippet = result.result_snippet.replace("\n", " ").strip()
    if result.tool_name == "file.read":
        snippet = snippet.replace("→", " ")
    return truncate_text(" ".join(snippet.split()), 120)


def _summarize_group_results(results):
    summarized = []
    i = 0
    while i < len(results):
        result = results[i]
        line = _format_compact_match(result)
        snippet = _compact_snippet(result)
        count = 1
        j = i + 1
        while j < len(results):
            other = results[j]
            if _format_compact_match(other) != line or _compact_snippet(other) != snippet:
                break
            count += 1
            j += 1
        if count > 1:
            line = f"{line}  ×{count}"
        summarized.append({"line": line, "snippet": snippet})
        i = j
    return summarized


def _compact_display_path(path: str) -> str:
    path_obj = Path(path)
    parts = [part for part in path_obj.parts if part not in ("/", "")]
    if len(parts) <= 3:
        return "/".join(parts) if parts else path_obj.name
    if len(parts) == 4:
        return "/".join(parts)

    # Prefer the last 3-4 components for disambiguation in monorepos, while
    # still preserving repo-root context for common paths under /work/<repo>/...
    if len(parts) >= 5 and parts[0] == "work":
        return "/".join(parts[1:5]) if len(parts) == 5 else "/".join(parts[-4:])
    return "/".join(parts[-4:])


def build_tool_search_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "tool-search",
        help="Search tool calls with fielded query syntax",
        description=(
            "Search tool calls with structured inline fields plus bare-term FTS.\n\n"
            "Filter contract:\n"
            "- repeated same field = OR\n"
            "- different fields = AND\n"
            "- CLI flags and inline fields accumulate into the same field set\n"
            "- inline field aliases: all-tags/all_tag/all-tag → all_tags; no-tag → no_tag; tool-tag → tool_tag\n"
            "- date fields (`since:`, `before:`) accept YYYY-MM-DD, Nd, Nw, today, yesterday\n"
            "- invalid inline dates fail with a parse error\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd tool-search tool:file.read path:pyproject.toml
  siftd tool-search status:error tool:shell.execute git
  siftd tool-search -w siftd --since 7d tool:file.edit
  siftd tool-search --tool-tag shell:test cmd:pytest
  siftd tool-search --show-snippets tool:file.read path:pyproject.toml
  siftd tool-search --ungrouped status:error tool:shell.execute
  siftd tool-search --json cmd:git tool:shell.execute
""",
    )
    p.add_argument("query", nargs="*", help="Tool search query")
    add_filter_args(p, include_tool=True, include_tool_tag=True)
    p.add_argument("-n", "--limit", type=int, default=20, help="Max results (default: 20)")
    p.add_argument("--json", action="store_true", help="Output as structured JSON")
    p.add_argument("--grouped", action="store_true", default=True, help="Group results by conversation (default: on)")
    p.add_argument("--ungrouped", action="store_false", dest="grouped", help="Show one row per tool call")
    p.add_argument("--show-snippets", action="store_true", help="Show short per-match snippets under compact grouped results")
    p.add_argument("--rebuild-index", action="store_true", help="Rebuild the tool-search projection before searching")
    p.set_defaults(func=cmd_tool_search)
