"""CLI handlers for query commands (query, tools)."""

import argparse
import sqlite3
import sys
from pathlib import Path

from siftd.cli_common import parse_date
from siftd.output import fmt_timestamp, fmt_tokens, fmt_workspace, truncate_text
from siftd.paths import db_path, queries_dir


def cmd_tools(args) -> int:
    """Show tool usage summary by category."""
    from siftd.api import get_tool_tag_summary, get_tool_tags_by_workspace

    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        if args.json:
            print("[]")
            return 0
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    prefix = args.prefix or "shell:"

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
            import json

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
            ws_display = Path(ws_usage.workspace).name if ws_usage.workspace != "(no workspace)" else ws_usage.workspace
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
        import json

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

    # Validate --exchanges
    exchanges_n = getattr(args, "exchanges", None)
    if exchanges_n is not None and exchanges_n < 1:
        print("Error: --exchanges must be at least 1")
        return 1

    db = Path(args.db) if args.db else None

    try:
        detail = get_conversation(args.conversation_id, db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'siftd ingest' to create it.")
        return 1

    if not detail:
        print(f"Conversation not found: {args.conversation_id}")
        return 1

    # Determine truncation limit
    chars_limit = 200  # default
    if getattr(args, "brief", False):
        chars_limit = 80
    elif getattr(args, "full", False):
        chars_limit = 0  # no truncation
    elif getattr(args, "chars", None) is not None:
        chars_limit = args.chars

    # Header
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

    # Summary mode: just metadata, no exchanges
    if getattr(args, "summary", False):
        print(f"Exchanges: {len(detail.exchanges)}")
        return 0

    print()

    # Determine which exchanges to show
    exchanges = detail.exchanges
    if exchanges_n is not None:
        # Show last N exchanges
        exchanges = exchanges[-exchanges_n:] if exchanges_n < len(exchanges) else exchanges

    # Timeline
    for ex in exchanges:
        ts = fmt_timestamp(ex.timestamp, time_only=True)

        # Prompt
        if ex.prompt_text:
            text = truncate_text(ex.prompt_text, chars_limit)
            print(f"[prompt] {ts}")
            print(f"  {text}")
            print()

        # Response
        if ex.response_text is not None or ex.tool_calls:
            print(f"[response] {ts} ({fmt_tokens(ex.input_tokens)} in / {fmt_tokens(ex.output_tokens)} out)")
            if ex.response_text:
                text = truncate_text(ex.response_text, chars_limit)
                print(f"  {text}")
            for tc in ex.tool_calls:
                if tc.count > 1:
                    print(f"  → {tc.tool_name} ×{tc.count} ({tc.status})")
                else:
                    print(f"  → {tc.tool_name} ({tc.status})")
            print()

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
        columns = result.columns
        widths = [len(c) for c in columns]
        str_rows = []
        for row in result.rows:
            str_row = [str(v) if v is not None else "" for v in row]
            str_rows.append(str_row)
            for i, val in enumerate(str_row):
                widths[i] = max(widths[i], len(val))
        header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
        print(header)
        print("  ".join("-" * w for w in widths))
        for str_row in str_rows:
            print("  ".join(val.ljust(widths[i]) for i, val in enumerate(str_row)))
    else:
        print("OK (no results)")

    return 0


def cmd_query(args) -> int:
    """List conversations with composable filters."""
    # Dispatch to sql subcommand if conversation_id is "sql"
    if args.conversation_id == "sql":
        return _query_sql(args)

    # Dispatch to detail view if conversation ID provided
    if args.conversation_id:
        return _query_detail(args)

    from siftd.api import list_conversations

    db = Path(args.db) if args.db else None

    try:
        conversations = list_conversations(
            db_path=db,
            workspace=args.workspace,
            model=args.model,
            since=parse_date(args.since),
            before=parse_date(args.before),
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
                print(
                    "\nTip: Try 'siftd peek' for active sessions not yet ingested.",
                    file=sys.stderr,
                )
            elif has_filters:
                print(
                    "\nTip: No matches for current filters. Try broadening your search or run 'siftd query' without filters.",
                    file=sys.stderr,
                )
            else:
                print(
                    "\nTip: Run 'siftd ingest' to import recent sessions.",
                    file=sys.stderr,
                )
        return 0

    # JSON output
    if args.json:
        import json
        out = [
            {
                "id": c.id,
                "workspace": c.workspace_path,
                "model": c.model,
                "started_at": c.started_at,
                "prompts": c.prompt_count,
                "responses": c.response_count,
                "tokens": c.total_tokens,
                "cost": c.cost,
                "tags": c.tags,
            }
            for c in conversations
        ]
        print(json.dumps(out, indent=2))
        return 0

    # Verbose mode: full table with all columns
    if args.verbose:
        columns = ["id", "workspace", "model", "started_at", "prompts", "responses", "tokens", "cost", "tags"]
        str_rows = []
        for c in conversations:
            cid = c.id[:12] if c.id else ""
            ws = fmt_workspace(c.workspace_path)
            model = c.model or ""
            started = fmt_timestamp(c.started_at)
            prompts = str(c.prompt_count)
            responses = str(c.response_count)
            tokens = str(c.total_tokens)
            cost = f"${c.cost:.4f}" if c.cost else "$0.0000"
            tags = ", ".join(c.tags) if c.tags else ""
            str_rows.append([cid, ws, model, started, prompts, responses, tokens, cost, tags])

        # Compute column widths and print table
        widths = [len(col) for col in columns]
        for str_row in str_rows:
            for i, val in enumerate(str_row):
                widths[i] = max(widths[i], len(val))

        header = "  ".join(col.ljust(widths[i]) for i, col in enumerate(columns))
        print(header)
        print("  ".join("-" * w for w in widths))
        for str_row in str_rows:
            print("  ".join(val.ljust(widths[i]) for i, val in enumerate(str_row)))
        return 0

    # Default: short mode — one dense line per conversation with truncated ID
    for c in conversations:
        cid = c.id[:12] if c.id else ""
        ws = fmt_workspace(c.workspace_path)
        model = c.model or ""
        started = fmt_timestamp(c.started_at)
        tokens = fmt_tokens(c.total_tokens)
        tag_str = f"  [{', '.join(c.tags)}]" if c.tags else ""
        print(f"{cid}  {started}  {ws}  {model}  {c.prompt_count}p/{c.response_count}r  {tokens} tok{tag_str}")

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
    p_tools.add_argument("-n", "--limit", type=int, default=20, help="Max workspaces for --by-workspace (default: 20)")
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
  siftd query <id> --brief            # brief output (80 char truncation)
  siftd query <id> --full             # full text, no truncation
  siftd query sql                     # list available .sql files
  siftd query sql cost                # run the 'cost' query
  siftd query sql cost --var ws=proj  # run with variable substitution""",
    )

    # Positional arguments
    p_query.add_argument("conversation_id", nargs="?", help="Conversation ID for detail view, or 'sql' for SQL query mode")
    p_query.add_argument("sql_name", nargs="?", help="SQL query name (when using 'sql' subcommand)")

    # Filtering options
    filter_group = p_query.add_argument_group("filtering")
    filter_group.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    filter_group.add_argument("-m", "--model", metavar="NAME", help="Filter by model name")
    filter_group.add_argument("--since", metavar="DATE", type=parse_date, help="Conversations started after this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    filter_group.add_argument("--before", metavar="DATE", type=parse_date, help="Conversations started before this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    filter_group.add_argument("-t", "--tool", metavar="NAME", help="Filter by canonical tool name (e.g. shell.execute)")

    # Tag filtering options
    tag_group = p_query.add_argument_group("tag filtering")
    tag_group.add_argument("-l", "--tag", action="append", metavar="NAME", help="Filter by tag (repeatable, OR logic)")
    tag_group.add_argument("--all-tags", action="append", metavar="NAME", help="Require all specified tags (AND logic)")
    tag_group.add_argument("--no-tag", action="append", metavar="NAME", help="Exclude conversations with this tag (NOT logic)")
    tag_group.add_argument("--tool-tag", metavar="NAME", help="Filter by tool call tag (e.g. shell:test)")

    # Output options
    output_group = p_query.add_argument_group("output")
    output_group.add_argument("-n", "--limit", type=int, default=10, help="Number of conversations to show (0=all, default: 10)")
    output_group.add_argument("-v", "--verbose", action="store_true", help="Full table with all columns")
    output_group.add_argument("--oldest", action="store_true", help="Sort by oldest first (default: newest first)")
    output_group.add_argument("--json", action="store_true", help="Output as JSON array")
    output_group.add_argument("--stats", action="store_true", help="Show summary totals after list")

    # Detail view options (when conversation_id is provided)
    detail_group = p_query.add_argument_group("detail view")
    detail_group.add_argument("--exchanges", type=int, metavar="N", help="Number of exchanges to show (default: all)")
    detail_group.add_argument("--brief", action="store_true", help="Brief output (80 char truncation)")
    detail_group.add_argument("--summary", action="store_true", help="Summary only (metadata, no exchanges)")
    detail_group.add_argument("--full", action="store_true", help="Full text (no truncation)")
    detail_group.add_argument("--chars", type=int, metavar="N", help="Truncate text at N characters (default: 200)")

    # SQL query options
    sql_group = p_query.add_argument_group("sql queries")
    sql_group.add_argument("--var", action="append", metavar="KEY=VALUE", help="Substitute $KEY with VALUE in SQL")

    p_query.set_defaults(func=cmd_query)
