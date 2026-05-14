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
        print("Error: Failed to resolve ID", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if classified is None:
        print(f"Error: ID not found: {args.ulid}", file=sys.stderr)
        return 1
    if classified["status"] == "ambiguous":
        if args.json:
            out = {"kind": "ambiguous", "candidates": classified["candidates"]}
            print(_json.dumps(out, indent=2))
            return 2
        print(f"Error: Ambiguous ID prefix: {args.ulid}", file=sys.stderr)
        print("Candidates:", file=sys.stderr)
        for candidate in classified["candidates"]:
            print(f"  {candidate['kind']}: {candidate['id']}", file=sys.stderr)
        return 2

    kind = classified["kind"]
    full_id = classified["id"]
    context = classified["context"]

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
            turn = context.get("turn")
            turn_str = f", turn {turn}" if turn is not None else ""
            print(f"event {full_id[:8]}... (conversation: {conv_id[:8]}...{turn_str})")
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


def _resolve_and_classify(conn, raw_id: str) -> dict | None:
    """Classify the ID as conversation or event and gather context.

    Returns a resolved classification dict, an ambiguous result dict, or None.
    """
    from siftd.api import get_conversation_metadata, resolve_entity_id
    from siftd.api.events import resolve_event_row

    conv_full = resolve_entity_id(conn, "conversation", raw_id)
    row = resolve_event_row(conn, raw_id)
    event_full = row["id"] if row and row["kind"] in ("prompt", "response", "tool_call") else None
    event_conversation_id = row["conversation_id"] if event_full and row else None

    if conv_full and event_full:
        return {
            "status": "ambiguous",
            "candidates": [
                {"kind": "conversation", "id": conv_full},
                {"kind": "event", "id": event_full},
            ],
        }
    if conv_full:
        conv_data = get_conversation_metadata(conn, conv_full)
        return {
            "status": "ok",
            "kind": "conversation",
            "id": conv_full,
            "context": {
                "workspace": conv_data.get("workspace") if conv_data else None,
                "started_at": conv_data.get("started_at") if conv_data else None,
            },
        }
    if event_full:
        turn = _event_turn_number(conn, row)
        return {
            "status": "ok",
            "kind": "event",
            "id": event_full,
            "context": {"conversation_id": event_conversation_id, "turn": turn},
        }

    return None


def _event_turn_number(conn, event_row) -> int | None:
    """Compute 1-based turn index for an event by walking parent chain to prompt anchor."""
    if not event_row:
        return None

    # Walk parent chain until we reach a prompt
    current_row = event_row
    while current_row["kind"] != "prompt":
        parent_id = current_row["parent_id"]
        if not parent_id:
            return None

        current_row = conn.execute(
            "SELECT id, kind, parent_id, timestamp, conversation_id FROM events WHERE id = ?",
            (parent_id,),
        ).fetchone()
        if not current_row:
            return None

    prompt_row = current_row

    count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM events"
        " WHERE conversation_id = ? AND kind = 'prompt'"
        " AND (timestamp < ? OR (timestamp = ? AND id <= ?))",
        (prompt_row["conversation_id"], prompt_row["timestamp"], prompt_row["timestamp"], prompt_row["id"]),
    ).fetchone()
    return int(count_row["n"]) if count_row else None


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
