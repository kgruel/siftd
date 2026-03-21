"""CLI handlers for query commands (query, tools)."""

import argparse
import sqlite3
import sys
from pathlib import Path

from siftd.cli_common import apply_config_defaults, fidelity_from_args, resolve_db
from siftd.output import fmt_timestamp, fmt_tokens, fmt_workspace, print_table
from siftd.output.painted_bridge import emit_output
from siftd.paths import queries_dir


def cmd_tools(args) -> int:
    """Show tool usage summary by category."""
    import json

    from siftd.config import get_tools_defaults

    apply_config_defaults(args, get_tools_defaults, {"limit": 20})
    from siftd.api import get_tool_tag_summary, get_tool_tags_by_workspace

    db = resolve_db(args)
    prefix = args.prefix or "shell:"

    # Try serve delegation
    try:
        from siftd.serve.delegation import try_delegate

        params: dict[str, object] = {"prefix": prefix, "by_workspace": args.by_workspace, "n": args.limit}
        params = {k: v for k, v in params.items() if v is not None and v is not False}
        result = try_delegate("/v1/tools", params=params, db=db)
        if result is not None:
            if args.json:
                print(json.dumps(result, indent=2))
            elif args.by_workspace and "workspaces" in result:
                for ws in result["workspaces"]:
                    ws_display = fmt_workspace(ws["workspace"])
                    print(f"\n{ws_display} ({ws['total']} total)")
                    for tag in ws["tags"]:
                        category = tag["name"][len(prefix):] if tag["name"].startswith(prefix) else tag["name"]
                        print(f"  {category}: {tag['count']}")
            elif "tags" in result:
                total = result.get("total", 0)
                print(f"Tool call tags ({prefix}*): {total} total\n")
                for tag in result["tags"]:
                    category = tag["name"][len(prefix):] if tag["name"].startswith(prefix) else tag["name"]
                    print(f"  {category}: {tag['count']} ({tag.get('percentage', 0)}%)")
            return 0
    except Exception:
        pass

    if not db.exists():
        if args.json:
            print("[]")
            return 0
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    # By-workspace mode
    if args.by_workspace:
        try:
            results = get_tool_tags_by_workspace(
                db_path=db,
                prefix=prefix,
                limit=args.limit,
            )
        except FileNotFoundError as e:
            if args.json:
                print("[]")
                return 0
            print(str(e))
            return 1

        if not results:
            if args.json:
                print("[]")
                return 0
            print(f"No tool calls with '{prefix}*' tags found.")
            return 0

        # JSON output for by-workspace mode
        if args.json:
            out = [
                {
                    "workspace": ws_usage.workspace,
                    "total": ws_usage.total,
                    "tags": [
                        {"name": tag.name, "count": tag.count}
                        for tag in ws_usage.tags
                    ],
                }
                for ws_usage in results
            ]
            print(json.dumps(out, indent=2))
            return 0

        for ws_usage in results:
            ws_display = fmt_workspace(ws_usage.workspace)
            print(f"\n{ws_display} ({ws_usage.total} total)")
            for tag in ws_usage.tags:
                # Strip prefix for display
                category = tag.name[len(prefix):] if tag.name.startswith(prefix) else tag.name
                print(f"  {category}: {tag.count}")

        return 0

    # Default: summary mode
    try:
        tags = get_tool_tag_summary(db_path=db, prefix=prefix)
    except FileNotFoundError as e:
        if args.json:
            print("[]")
            return 0
        print(str(e))
        return 1

    if not tags:
        if args.json:
            print("[]")
            return 0
        print(f"No tool calls with '{prefix}*' tags found.")
        print("Run 'siftd backfill --shell-tags' to categorize shell commands.")
        return 0

    # JSON output for summary mode
    if args.json:
        total = sum(t.count for t in tags)
        out = [
            {
                "name": tag.name,
                "count": tag.count,
                "percentage": round((tag.count / total) * 100, 1) if total > 0 else 0,
            }
            for tag in tags
        ]
        print(json.dumps(out, indent=2))
        return 0

    total = sum(t.count for t in tags)
    print(f"Tool call tags ({prefix}*): {total} total\n")

    for tag in tags:
        # Strip prefix for display
        category = tag.name[len(prefix):] if tag.name.startswith(prefix) else tag.name
        pct = (tag.count / total) * 100 if total > 0 else 0
        print(f"  {category}: {tag.count} ({pct:.1f}%)")

    return 0



def _query_detail(args) -> int:
    """Show conversation detail timeline."""
    from siftd.api import get_conversation
    from siftd.cli_common import fidelity_from_args, tool_chars_from_args

    # Validate --exchanges
    exchanges_n = getattr(args, "exchanges", None)
    if exchanges_n is not None and exchanges_n < 1:
        print("Error: --exchanges must be at least 1")
        return 1

    db = Path(args.db) if args.db else None
    effective_db = db or resolve_db(args)

    fidelity = fidelity_from_args(args)
    tool_chars = tool_chars_from_args(args, fidelity)

    include_thinking = fidelity.shows("thinking")
    include_tool_content = fidelity.shows("tools")
    tools_flag = getattr(args, "tools", None)
    tool_filter = None
    if tools_flag is not None and tools_flag != "all":
        tool_filter = tools_flag

    # For --json output, delegate to serve if available (avoids cold-open
    # entirely — server returns the canonical JSON shape directly)
    if getattr(args, "json", False) and not getattr(args, "summary", False):
        try:
            from siftd.serve.delegation import try_delegate

            result = try_delegate("/v1/query", {"id": args.conversation_id}, db=effective_db)
            if result is not None and "conversation" in result:
                import json

                print(json.dumps(result["conversation"], indent=2))
                return 0
        except Exception:
            pass

    try:
        detail = get_conversation(
            args.conversation_id,
            db_path=db,
            include_thinking=include_thinking,
            include_tool_content=include_tool_content,
            tool_filter=tool_filter,
        )
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'siftd ingest' to create it.")
        return 1

    if not detail:
        print(f"Conversation not found: {args.conversation_id}")
        return 1

    # Summary mode: just metadata, no exchanges
    if getattr(args, "summary", False):
        ws_name = fmt_workspace(detail.workspace_path)
        started = fmt_timestamp(detail.started_at)
        total_tokens = detail.total_input_tokens + detail.total_output_tokens

        print(f"Conversation: {detail.id}")
        if ws_name:
            print(f"Workspace: {ws_name}")
        print(f"Started: {started}")
        print(f"Model: {detail.model or 'unknown'}")
        print(f"Tokens: {fmt_tokens(total_tokens)} (input: {fmt_tokens(detail.total_input_tokens)} / output: {fmt_tokens(detail.total_output_tokens)})")
        if detail.tags:
            print(f"Tags: {', '.join(detail.tags)}")
        print(f"Turns: {len(detail.turns)}")
        return 0

    # Determine which turns to show
    show_turns = detail.turns
    if exchanges_n is not None:
        show_turns = show_turns[-exchanges_n:] if exchanges_n < len(show_turns) else show_turns

    from siftd.output.format_registry import select_format

    fmt = select_format(
        json_mode=getattr(args, "json", False),
        is_tty=sys.stdout.isatty(),
    )
    result = fmt.render_detail(
        show_turns, fidelity, detail=detail, tool_chars=tool_chars,
    )
    emit_output(result)
    return 0


def _query_sql(args) -> int:
    """List or run .sql query files (formerly 'queries' command)."""
    from siftd.api import QueryError, list_query_files, run_query_file

    # List mode: no name provided
    if not args.sql_name:
        query_files = list_query_files()
        if not query_files:
            print(f"No queries found in {queries_dir()}")
            return 0
        for qf in query_files:
            suffix = f"  (vars: {', '.join(qf.variables)})" if qf.variables else "  (no vars)"
            print(f"{qf.name}{suffix}")
        return 0

    # Run mode: parse variables
    variables = None
    if args.var:
        variables = {}
        for v in args.var:
            if "=" not in v:
                print(f"Invalid --var format (expected key=value): {v}")
                return 1
            key, value = v.split("=", 1)
            variables[key] = value

    db = Path(args.db) if args.db else None

    try:
        result = run_query_file(args.sql_name, variables, db_path=db)
    except FileNotFoundError as e:
        if "Query file not found" in str(e):
            print(f"Query not found: {e}")
            print("Available queries:")
            for qf in list_query_files():
                print(f"  {qf.name}")
            return 1
        print(str(e))
        print("Run 'siftd ingest' to create it.")
        return 1
    except QueryError as e:
        if "Missing variables" in str(e):
            # Extract missing vars for usage hint
            import re
            match = re.search(r"Missing variables: (.+)", str(e))
            missing = match.group(1).split(", ") if match else []
            print(f"Query '{args.sql_name}' requires variables not provided: {', '.join(missing)}")
            print(f"Usage: siftd query sql {args.sql_name} " + " ".join(f"--var {v}=<value>" for v in missing))
        else:
            print(str(e))
        return 1

    # Format output
    if result.rows:
        str_rows = [
            [str(v) if v is not None else "" for v in row]
            for row in result.rows
        ]
        print_table(result.columns, str_rows)
    else:
        print("OK (no results)")

    return 0


def cmd_query(args) -> int:
    """List conversations with composable filters."""
    from siftd.config import get_query_defaults

    query_defaults = get_query_defaults()
    apply_config_defaults(
        args,
        lambda: {k: v for k, v in query_defaults.items() if k in {"limit", "chars", "tool_chars"}},
        {"limit": 10, "tool_chars": 120},
    )

    # Dispatch to sql subcommand if conversation_id is "sql"
    if args.conversation_id == "sql":
        return _query_sql(args)

    # Dispatch to detail view if conversation ID provided
    if args.conversation_id:
        return _query_detail(args)

    from siftd.api import list_conversations
    from siftd.api.conversations import ConversationSummary

    db = Path(args.db) if args.db else None
    effective_db = db or resolve_db(args)

    conversations = None

    # Try serve delegation (avoids cold-open on large DBs)
    try:
        from siftd.serve.delegation import try_delegate

        params: dict[str, object] = {
            "workspace": args.workspace,
            "model": args.model,
            "since": args.since,
            "before": args.before,
            "tool": args.tool,
            "tag": args.tag,
            "all_tags": getattr(args, "all_tags", None),
            "no_tag": getattr(args, "no_tag", None),
            "tool_tag": getattr(args, "tool_tag", None),
            "n": args.limit,
            "oldest": args.oldest,
        }
        params = {k: v for k, v in params.items() if v is not None and v is not False}

        result = try_delegate("/v1/query", params=params, db=effective_db)
        if result is not None and "conversations" in result:
            conversations = [
                ConversationSummary(
                    id=c["id"],
                    workspace_path=c.get("workspace"),
                    model=c.get("model"),
                    started_at=c.get("started_at"),
                    prompt_count=c.get("prompts", 0),
                    response_count=c.get("responses", 0),
                    total_tokens=c.get("tokens", 0),
                    cost=c.get("cost"),
                    tags=c.get("tags", []),
                )
                for c in result["conversations"]
            ]
    except Exception:
        pass

    if conversations is None:
        try:
            conversations = list_conversations(
                db_path=db,
                workspace=args.workspace,
                model=args.model,
                since=args.since,
                before=args.before,
                tool=args.tool,
                tags=args.tag,
                all_tags=getattr(args, "all_tags", None),
                exclude_tags=getattr(args, "no_tag", None),
                tool_tag=getattr(args, "tool_tag", None),
                limit=args.limit,
                oldest_first=args.oldest,
            )
        except FileNotFoundError as e:
            print(str(e))
            print("Run 'siftd ingest' to create it.")
            return 1
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if "no such table" in err_msg and "fts" in err_msg:
                print("FTS index not found. Run 'siftd ingest' first.", file=sys.stderr)
            elif "fts5" in err_msg or "syntax" in err_msg:
                print(f"Invalid search query: {e}", file=sys.stderr)
                print("Tip: Check your search query for syntax errors.", file=sys.stderr)
            else:
                print(f"Database error: {e}", file=sys.stderr)
                print("Tip: Run 'siftd doctor' to check database health.", file=sys.stderr)
            return 1

    if not conversations:
        if args.json:
            print("[]")
        else:
            print("No conversations found.")
            # Provide helpful hints based on filters used
            has_filters = any([
                args.workspace, args.model, args.since, args.before,
                args.tool, args.tag,
                getattr(args, "all_tags", None),
                getattr(args, "no_tag", None),
                getattr(args, "tool_tag", None),
            ])
            if args.workspace:
                print("\nTip: Try 'siftd peek' for active sessions not yet ingested.", file=sys.stderr)
            elif has_filters:
                print(
                    "\nTip: No matches for current filters. Try broadening your search or run 'siftd query' without filters.",
                    file=sys.stderr,
                )
            else:
                print(
                    "\nTip: Run 'siftd ingest' to import recent sessions, or 'siftd peek' to check live sessions.",
                    file=sys.stderr,
                )
        return 0

    # Render list via formatter
    from siftd.output.format_registry import select_format

    fidelity = fidelity_from_args(args)
    if args.verbose:
        fidelity = fidelity.with_depth(3)

    fmt = select_format(json_mode=args.json, is_tty=sys.stdout.isatty())
    output = fmt.render_list(conversations, fidelity)
    emit_output(output)

    # Stats summary (shown after list when --stats flag is set)
    if args.stats:
        total_convs = len(conversations)
        total_prompts = sum(c.prompt_count for c in conversations)
        total_responses = sum(c.response_count for c in conversations)
        total_tokens = sum(c.total_tokens for c in conversations)
        print()
        print("--- Stats ---")
        print(f"Conversations: {total_convs}")
        print(f"Total prompts: {total_prompts}")
        print(f"Total responses: {total_responses}")
        print(f"Total tokens: {fmt_tokens(total_tokens)}")

    return 0


def build_query_parser(subparsers) -> None:
    """Add 'query' and 'tools' subparsers."""
    # tools
    p_tools = subparsers.add_parser(
        "tools",
        help="Summarize tool usage by category",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd tools                    # shell command categories summary
  siftd tools --by-workspace     # breakdown by workspace
  siftd tools --prefix shell:    # filter by tag prefix""",
    )
    p_tools.add_argument("--by-workspace", action="store_true", help="Show breakdown by workspace")
    p_tools.add_argument("--prefix", metavar="PREFIX", help="Tag prefix to filter (default: shell:)")
    p_tools.add_argument("-n", "--limit", type=int, default=None, help="Max workspaces for --by-workspace (default: 20)")
    p_tools.add_argument("--json", action="store_true", help="Output as JSON")
    p_tools.set_defaults(func=cmd_tools)

    # query
    p_query = subparsers.add_parser(
        "query",
        help="List and filter conversations by metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""List and filter conversations by metadata (workspace, model, date, tags).
For semantic content search, use: siftd search <query>

examples:
  siftd query                         # list recent conversations
  siftd query -n 20                   # list 20 conversations
  siftd query -w myproject            # filter by workspace
  siftd query -l research:auth        # conversations tagged research:auth
  siftd query -l research: -l useful: # OR — any research: or useful: tag
  siftd query --all-tags important --all-tags reviewed  # AND — must have both
  siftd query -l research: --no-tag archived            # combine OR + NOT
  siftd query --tool-tag shell:test   # conversations with test commands
  siftd query <id>                    # show conversation detail
  siftd query <id> --summary          # metadata only, no exchanges
  siftd query <id> --exchanges 5      # last 5 exchanges
  siftd query <id> --brief            # compact detail view (80 char truncation)
  siftd query <id> -b                 # short alias for --brief
  siftd query <id> --full             # full text, no truncation
  siftd query <id> -F                 # short alias for --full
  siftd query sql                     # list available .sql files
  siftd query sql cost                # run the 'cost' query
  siftd query sql cost --var ws=proj  # run with variable substitution""",
    )

    # Positional arguments
    p_query.add_argument("conversation_id", nargs="?", help="Conversation ID for detail view, or 'sql' for SQL query mode")
    p_query.add_argument("sql_name", nargs="?", help="SQL query name (when using 'sql' subcommand)")

    # Filtering options
    from siftd.cli_filters import add_filter_args

    add_filter_args(p_query, include_tool=True, include_tool_tag=True)

    # Output options
    output_group = p_query.add_argument_group("output")
    output_group.add_argument("-n", "--limit", type=int, default=None, help="Number of conversations to show (0=all, default: 10)")
    output_group.add_argument("-v", "--verbose", action="store_true", help="Full table with all columns")
    output_group.add_argument("--oldest", action="store_true", help="Sort by oldest first (default: newest first)")
    output_group.add_argument("--json", action="store_true", help="Output as JSON array")
    output_group.add_argument("--stats", action="store_true", help="Show summary totals after list")

    # Detail view options (when conversation_id is provided)
    detail_group = p_query.add_argument_group("detail view")
    detail_group.add_argument("--exchanges", type=int, metavar="N", help="Number of turns to show (default: all)")
    detail_group.add_argument("-b", "--brief", action="store_true", help="Compact detail view (80 char truncation)")
    detail_group.add_argument("--summary", action="store_true", help="Summary only (metadata, no turns)")
    detail_group.add_argument("-F", "--full", action="store_true", help="Full text (no truncation)")
    detail_group.add_argument("--chars", type=int, metavar="N", help="Truncate text at N characters (default: no truncation)")
    detail_group.add_argument("--thinking", action="store_true", help="Show model thinking/reasoning blocks")
    detail_group.add_argument("--tools", nargs="?", const="all", metavar="FILTER",
        help="Show tool inputs/results (optional filter: tool name prefix or 'errors')")
    detail_group.add_argument("--tool-chars", type=int, metavar="N", default=None,
        help="Truncate tool input/result at N characters (default: 120)")

    # SQL query options
    sql_group = p_query.add_argument_group("sql queries")
    sql_group.add_argument("--var", action="append", metavar="KEY=VALUE", help="Substitute $KEY with VALUE in SQL")

    p_query.set_defaults(func=cmd_query)
