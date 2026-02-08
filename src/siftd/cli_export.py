"""CLI handler for export command (export conversations for PR review)."""

import argparse
import sqlite3
import sys
from pathlib import Path

from siftd.cli_common import parse_date, resolve_db


def cmd_export(args) -> int:
    """Export conversations for PR review."""
    from siftd.api import ExportOptions, export_conversations, format_export

    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    # Determine what to export
    conversation_ids = [args.conversation_id] if args.conversation_id else None
    last = args.last

    # Default: if no ID and no --last specified, export last 1
    if not conversation_ids and last is None:
        last = 1

    try:
        conversations = export_conversations(
            conversation_ids=conversation_ids,
            last=last,
            workspace=args.workspace,
            tags=args.tag,
            exclude_tags=getattr(args, "no_tag", None),
            since=parse_date(args.since),
            before=parse_date(args.before),
            search=args.search,
            db_path=db,
        )
    except FileNotFoundError as e:
        print(str(e))
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
        print("No conversations found matching criteria.")
        return 1

    # Format output
    options = ExportOptions(
        format=args.format,
        prompts_only=args.prompts_only,
        no_header=args.no_header,
    )

    output = format_export(conversations, options)

    # Write to file or stdout
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output)
        print(f"Exported {len(conversations)} session(s) to {output_path}")
    else:
        print(output)

    return 0


def build_export_parser(subparsers) -> None:
    """Add the 'export' subparser to the CLI."""
    p_export = subparsers.add_parser(
        "export",
        help="Export conversations for PR review workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd export --last                   # export most recent session (prompts)
  siftd export --last 3                 # export last 3 sessions
  siftd export 01HX4G7K                 # export specific session (prefix match)
  siftd export -w myproject --since 2024-01-01  # filter by workspace and date
  siftd export -l decision:auth         # export tagged conversations
  siftd export --last --format json     # structured JSON output
  siftd export --last --format exchanges  # include response summaries
  siftd export --last --prompts-only    # omit tool call details
  siftd export --last --no-tag private  # exclude private sessions
  siftd export --last -o context.md     # write to file""",
    )
    p_export.add_argument("conversation_id", nargs="?", help="Conversation ID to export (prefix match)")
    p_export.add_argument("-n", "--last", type=int, nargs="?", const=1, metavar="N", help="Export N most recent sessions (default: 1 if no ID given)")
    p_export.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    p_export.add_argument("-l", "--tag", action="append", metavar="NAME", help="Filter by tag (repeatable, OR logic)")
    p_export.add_argument("--no-tag", action="append", metavar="NAME", help="Exclude sessions with this tag (repeatable)")
    p_export.add_argument("--since", metavar="DATE", type=parse_date, help="Sessions after this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    p_export.add_argument("--before", metavar="DATE", type=parse_date, help="Sessions before this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    p_export.add_argument("-s", "--search", metavar="QUERY", help="Full-text search filter")
    p_export.add_argument(
        "-f",
        "--format",
        choices=["prompts", "exchanges", "json"],
        default="prompts",
        help="Output format: prompts (default), exchanges, json",
    )
    p_export.add_argument("--prompts-only", action="store_true", help="Omit response text and tool calls")
    p_export.add_argument("--no-header", action="store_true", help="Omit session metadata header")
    p_export.add_argument("-o", "--output", metavar="FILE", help="Write to file instead of stdout")
    p_export.set_defaults(func=cmd_export)
