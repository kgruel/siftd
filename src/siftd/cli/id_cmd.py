"""CLI handler for the 'id' command - classify and display ULID information."""

import argparse
import json as _json
import sys

from siftd.cli._common import resolve_db
from siftd.output import fmt_timestamp, fmt_workspace


def cmd_id(args) -> int:
    """Classify a ULID and show its type and context."""
    from siftd.api import open_database

    db = resolve_db(args)
    if not db or not db.exists():
        print(f"Error: Database not found at {db}", file=sys.stderr)
        return 1

    try:
        conn = open_database(db, read_only=True)
    except Exception as e:
        print(f"Error: Failed to open database: {e}", file=sys.stderr)
        return 1

    try:
        classified = _resolve_and_classify(conn, args.ulid)
    except Exception:
        conn.close()
        print("Error: Failed to resolve ID", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if classified is None:
        print(f"Error: ID not found: {args.ulid}", file=sys.stderr)
        return 1

    kind, full_id, context = classified

    # Text output
    if not args.json:
        if kind == "conversation":
            ws_name = fmt_workspace(context.get("workspace")) if context.get("workspace") else None
            started = fmt_timestamp(context.get("started_at")) if context.get("started_at") else None
            ws_str = f" (workspace: {ws_name}" if ws_name else " (workspace: unknown"
            started_str = f", started {started}" if started else ""
            print(f"conversation {full_id[:8]}... {ws_str}{started_str})")
            print(f"view:  siftd query {full_id}")
        elif kind == "event":
            conv_id = context.get("conversation_id", "")
            print(f"event {full_id[:8]}... (conversation: {conv_id[:8]}...)")
            print(f"view:  siftd query {full_id}")
        return 0

    # JSON output
    out = {
        "kind": kind,
        "id": full_id,
        "context": context,
    }
    print(_json.dumps(out, indent=2))
    return 0


def _resolve_and_classify(conn, raw_id: str) -> tuple[str, str, dict] | None:
    """Classify the ID as conversation or event and gather context.

    Returns ('conversation', full_id, context_dict) or ('event', full_id, context_dict) or None.
    Conversation match wins when both resolve (rare, since ULIDs are globally unique).
    """
    from siftd.api import get_conversation_metadata, resolve_entity_id
    from siftd.api.events import resolve_event_row

    # Try conversation match first (it wins if both match)
    conv_full = resolve_entity_id(conn, "conversation", raw_id)
    if conv_full:
        conv_data = get_conversation_metadata(conn, raw_id)
        context = {
            "workspace": conv_data.get("workspace") if conv_data else None,
            "started_at": conv_data.get("started_at") if conv_data else None,
        }
        return ("conversation", conv_full, context)

    # Try event match
    row = resolve_event_row(conn, raw_id)
    if row and row["kind"] in ("prompt", "response", "tool_call"):
        full_event_id = row["id"]
        context = {
            "conversation_id": row["conversation_id"],
        }
        return ("event", full_event_id, context)

    return None


def build_id_parser(subparsers) -> None:
    """Add 'id' subparser."""
    p_id = subparsers.add_parser(
        "id",
        help="Classify a ULID and show its type and context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd id 01HX4G7K9                   # identify a conversation or event
  siftd id 01HX4G7K9 --json            # structured classification""",
    )
    p_id.add_argument(
        "ulid",
        help="ULID or ULID prefix to classify",
    )
    p_id.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    p_id.set_defaults(func=cmd_id)
