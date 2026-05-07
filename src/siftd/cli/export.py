"""CLI handler for export command (export conversations as markdown or JSON)."""

import argparse
import sqlite3
import sys
from pathlib import Path

from siftd.cli._common import resolve_db


def cmd_export(args) -> int:
    """Export conversations as readable markdown or structured JSON."""
    from siftd.api.dispatch import Operation, execute
    from siftd.api.export import export_document
    from siftd.cli._common import fidelity_from_args

    db = resolve_db(args)

    conversation_ids = [args.conversation_id] if args.conversation_id else None
    last = args.last

    # Default: if no ID and no --last specified, export last 1
    if not conversation_ids and last is None:
        last = 1

    fidelity = fidelity_from_args(args)
    include_tools = fidelity.shows("tools")
    fmt = "json" if getattr(args, "json", False) else "md"

    op = Operation(
        path="/api/v1/export",
        method="GET",
        fn=export_document,
        params={
            "format": fmt,
            "fidelity": fidelity,
            "no_header": args.no_header,
            "id": conversation_ids,
            "last": last,
            "workspace": args.workspace,
            "tag": args.tag,
            "no_tag": getattr(args, "no_tag", None),
            "tag_kind": getattr(args, "tag_kind", None),
            "since": args.since,
            "before": args.before,
            "search": args.search,
            "db_path": db,
            "include_thinking": True,
            "include_tool_content": include_tools,
        },
        render_method="raw",
        fidelity=fidelity,
        db=db or Path(),
    )

    try:
        artifact = execute(op)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except sqlite3.OperationalError as e:
        err_msg = str(e).lower()
        if "no such table" in err_msg and "fts" in err_msg:
            print("FTS index not found. Run 'siftd ingest' first.", file=sys.stderr)
        elif "fts5" in err_msg or "syntax" in err_msg:
            print(f"Invalid search query: {e}", file=sys.stderr)
        else:
            print(f"Database error: {e}", file=sys.stderr)
        return 1

    if artifact.count == 0:
        print("No conversations found matching criteria.")
        return 1

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(artifact.content)
        print(f"Exported {artifact.count} session(s) to {output_path}")
    else:
        print(artifact.content)

    return 0


def build_export_parser(subparsers) -> None:
    """Add the 'export' subparser to the CLI."""
    p = subparsers.add_parser(
        "export",
        help="Export conversations as markdown or JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd export --last                   # export most recent session
  siftd export --last 3                 # export last 3 sessions
  siftd export 01HX4G7K                 # export specific session (prefix match)
  siftd export --last --thinking        # include thinking blocks
  siftd export --last --tools           # include tool inputs/results
  siftd export --last --full            # everything: thinking + tools
  siftd export --last --brief           # condensed output
  siftd export --last --json            # structured JSON output
  siftd export --last -o context.md     # write to file""",
    )
    p.add_argument("conversation_id", nargs="?", help="Conversation ID (prefix match)")
    p.add_argument(
        "-n", "--last", type=int, nargs="?", const=1, metavar="N",
        help="Export N most recent sessions (default: 1 if no ID given)",
    )

    from siftd.cli._filters import add_filter_args

    add_filter_args(p, include_model=False, include_search=True, include_all_tags=False)

    rendering = p.add_argument_group("rendering")
    rendering.add_argument(
        "--thinking", action="store_true",
        help="Expand thinking/reasoning blocks (default: placeholder)",
    )
    rendering.add_argument(
        "--tools", action="store_true",
        help="Expand tool inputs and results (default: summary)",
    )
    rendering.add_argument(
        "-b", "--brief", action="store_true",
        help="Condensed output (truncate long text)",
    )
    rendering.add_argument(
        "-F", "--full", action="store_true",
        help="Full output: thinking + tools, no truncation",
    )
    rendering.add_argument(
        "--json", action="store_true",
        help="Structured JSON output",
    )
    rendering.add_argument("--no-header", action="store_true", help="Omit session metadata header")
    rendering.add_argument("-o", "--output", metavar="FILE", help="Write to file instead of stdout")
    p.set_defaults(func=cmd_export)
