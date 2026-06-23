"""CLI handler for the 'id' command - classify and display ULID information."""

import argparse
import json as _json
import sys

from siftd.api.conversations import AmbiguousPrefix as _AmbiguousPrefix
from siftd.cli._common import print_ambiguous_error as _print_ambiguous_error
from siftd.cli._common import resolve_db
from siftd.output import fmt_timestamp, fmt_workspace, status
from siftd.output._id_format import short_id


def cmd_id(args) -> int:
    """Classify a ULID and show its type and context."""
    from siftd.api import open_database

    db = resolve_db(args)
    if not db or not db.exists():
        status.error(f"Database not found at {db}")
        return 1

    try:
        conn = open_database(db, read_only=True)
    except Exception as e:
        status.error(f"Failed to open database: {e}")
        return 1

    try:
        classified = _resolve_and_classify(conn, args.ulid)
    except _AmbiguousPrefix as exc:
        conn.close()
        if args.json:
            out = {"kind": "ambiguous_prefix", "prefix": exc.prefix, "matched_ids": exc.matched_ids, "total": exc.total}
            print(_json.dumps(out, indent=2))
            return 2
        _print_ambiguous_error(exc)
        return 2
    except Exception:
        status.error("Failed to resolve ID")
        return 1
    finally:
        conn.close()

    if classified is None:
        status.error(f"ID not found: {args.ulid}")
        return 1
    if classified["status"] == "ambiguous":
        # conversation vs event ambiguity (not prefix collision — that's caught above)
        if args.json:
            out = {"kind": "ambiguous", "candidates": classified["candidates"]}
            print(_json.dumps(out, indent=2))
            return 2
        # An enumerated-body error: the candidate ids ride a lines() block (a
        # callout's hint flattens newlines and can't carry a list), all to stderr
        # so a piped stdout / --json payload stays clean. Mirrors _common.print_ambiguous_error.
        from painted import print_block

        from siftd.output.common import should_use_ansi
        from siftd.output.listing import lines
        from siftd.output.theme import domain_styles

        ds = domain_styles()
        status.error(
            f"Ambiguous ID prefix: {args.ulid}",
            hint="The prefix matches both a conversation and an event.",
        )
        body = [
            [(f"{candidate['kind']}: ", None), (candidate["id"], ds.identifier)]
            for candidate in classified["candidates"]
        ]
        print_block(lines(body), sys.stderr, use_ansi=should_use_ansi(sys.stderr))
        return 2

    kind = classified["kind"]
    full_id = classified["id"]
    context = classified["context"]

    # Text output
    if not args.json:
        from painted import Style

        from siftd.output.listing import print_definitions
        from siftd.output.theme import domain_styles

        ds = domain_styles()
        if kind == "conversation":
            ws_name = fmt_workspace(context.get("workspace")) if context.get("workspace") else "unknown"
            started = fmt_timestamp(context.get("started_at")) if context.get("started_at") else None
            pairs: list[tuple[str, str | list[tuple[str, Style]]]] = [
                ("conversation", [(short_id(full_id), ds.identifier)]),
                ("workspace", ws_name),
            ]
            if started:
                pairs.append(("started", started))
            pairs.append(("view", [(f"siftd query {full_id}", ds.identifier)]))
            print_definitions(pairs)
        elif kind == "event":
            conv_id = context.get("conversation_id", "")
            turn = context.get("turn")
            pairs = [
                ("event", [(short_id(full_id), ds.identifier)]),
                ("conversation", [(short_id(conv_id), ds.identifier)]),
            ]
            if turn is not None:
                pairs.append(("turn", str(turn)))
            pairs.append(("view", [(f"siftd query {full_id}", ds.identifier)]))
            print_definitions(pairs)
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
    Raises AmbiguousPrefix if the prefix matches multiple conversations.
    """
    from siftd.api import resolve_entity_id
    from siftd.api.events import resolve_event_row

    conv_full = resolve_entity_id(conn, "conversation", raw_id)  # may raise AmbiguousPrefix
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
        from siftd.api import get_conversation_metadata
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
