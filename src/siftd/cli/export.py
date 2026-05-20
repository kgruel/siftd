"""CLI handler for export command (export conversations as markdown or JSON)."""

import argparse
import sqlite3
import sys
from pathlib import Path

from siftd.api.conversations import AmbiguousPrefix as _AmbiguousPrefix
from siftd.cli._common import print_ambiguous_error as _print_ambiguous_error
from siftd.cli._common import resolve_db


def cmd_export(args) -> int:
    """Export conversations as readable markdown or structured JSON."""
    from siftd.api.dispatch import Operation, execute, from_wire
    from siftd.api.export import export_document
    from siftd.cli._common import fidelity_from_args
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    db = resolve_db(args)

    conversation_ids = [args.conversation_id] if args.conversation_id else None
    last = args.last

    # Default: if no ID and no --last specified, export last 1
    if not conversation_ids and last is None:
        last = 1

    fidelity = fidelity_from_args(args)
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
        },
        # "export-artifact" picks the ExportArtifact deserializer in from_wire.
        # The local path doesn't use render_method (it calls op.fn directly via
        # execute()), so this only affects the delegated response path.
        render_method="export-artifact",
        fidelity=fidelity,
        db=db or Path(),
    )

    # Delegate to serve when configured; from_wire reconstructs the
    # ExportArtifact so the rendering code below is shape-identical
    # regardless of which path produced the artifact. Deserializers return
    # None on schema mismatch (e.g. older server returning the legacy
    # `{"conversations": [...]}` shape) — the fallback below covers that.
    artifact = None
    try:
        delegated = try_serve(op)
    except ServeRequest4xx as e:
        print_serve_4xx(e)
        return 1
    if delegated is not None and isinstance(delegated, dict):
        artifact = from_wire(op, delegated)

    if artifact is None:
        try:
            artifact = execute(op)
        except _AmbiguousPrefix as exc:
            _print_ambiguous_error(exc)
            return 2
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
        "-n", "--last", "--latest", type=int, nargs="?", const=1, metavar="N",
        help="Export N most recent sessions (default: 1 if no ID given)",
    )

    from siftd.cli._common import add_fidelity_args, add_output_args
    from siftd.cli._filters import add_filter_args

    add_filter_args(p, include_model=False, include_search=True, include_all_tags=False)
    add_output_args(p, json=True)
    add_fidelity_args(p, full=True, brief=True, thinking=True)

    # export-specific rendering options
    export_opts = p.add_argument_group("export options")
    export_opts.add_argument(
        "--tools", action="store_true",
        help="Expand tool inputs and results (default: summary)",
    )
    export_opts.add_argument("--no-header", action="store_true", help="Omit session metadata header")
    export_opts.add_argument("-o", "--output", metavar="FILE", help="Write to file instead of stdout")
    p.set_defaults(func=cmd_export)
