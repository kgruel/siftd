"""CLI handlers for query commands (query)."""

import argparse
import sqlite3
import sys
from pathlib import Path

from siftd.api.conversations import AmbiguousPrefix as _AmbiguousPrefix
from siftd.cli._common import (
    _parse_turns_range,
    apply_config_defaults,
    fidelity_from_args,
    print_ambiguous_error,
    resolve_db,
)
from siftd.output import fmt_tokens, status
from siftd.output.painted_bridge import emit_output


def _dispatch_detail(args) -> int:
    """Single-pass smart-router for `siftd query <id>`.

    Opens the DB once, classifies the ID, branches to the right detail
    handler. The conversation path closes the probe conn and re-enters
    its existing dispatch flow; the event path threads the conn into
    get_event to avoid a second open.
    """
    from siftd.api import open_database

    db = resolve_db(args)
    if not db or not db.exists():
        return _query_detail(args)
    try:
        probe = open_database(db, read_only=True)
    except Exception:
        return _query_detail(args)
    try:
        classified = _resolve_query_id(probe, args.conversation_id)
    except _AmbiguousPrefix as exc:
        probe.close()
        print_ambiguous_error(exc)
        return 2
    except Exception:
        probe.close()
        return _query_detail(args)

    if classified is None or classified[0] == "conversation":
        probe.close()
        return _query_detail(args)

    try:
        return _query_event_detail(args, conn=probe)
    finally:
        probe.close()


def _resolve_query_id(conn, raw_id: str) -> tuple[str, str] | None:
    """Classify the positional ID as a conversation or event reference.

    Returns ('conversation', full_id) or ('event', full_id) or None.
    Conversation match wins when both branches resolve (rare, since ULIDs
    are globally unique — but cheap to enforce).
    """
    from siftd.api import resolve_entity_id
    from siftd.api.events import resolve_event_row

    conv = resolve_entity_id(conn, "conversation", raw_id)
    if conv:
        return ("conversation", conv)
    row = resolve_event_row(conn, raw_id)
    if row and row["kind"] in ("prompt", "response", "tool_call"):
        return ("event", row["id"])
    return None


def _query_event_detail(args, *, conn=None) -> int:
    """Show event detail. Pass `conn=` to skip a redundant DB open."""
    import json as _json

    from siftd.api.events import get_event

    db = Path(args.db) if args.db else None
    effective_db = db or resolve_db(args)

    include_neighbors = getattr(args, "neighbors", False)

    try:
        detail = get_event(
            args.conversation_id,
            db_path=effective_db,
            conn=conn,
            include_content=True,
            include_neighbors=include_neighbors,
        )
    except FileNotFoundError as e:
        status.error(str(e), hint="Run 'siftd ingest' to create it.")
        return 1

    if not detail:
        status.error(f"Event not found: {args.conversation_id}")
        return 1

    if getattr(args, "json", False):
        print(_json.dumps(detail.to_dict(), indent=2))
        return 0

    # Compact text rendering — keep it minimal; agents will use --json. The themed
    # key:value listing: event/conversation ids ride ds.identifier, the token line
    # rides the amber metric thread, counts ride ds.metric. label_style=ds.temporal
    # matches the conversation-detail header so the two reads are one family.
    from siftd.output.listing import print_definitions
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    pairs: list[tuple[str, list[tuple[str, object]]]] = [
        ("Event:", [(detail.id, ds.identifier)]),
        ("Kind:", [(detail.kind, ds.model)]),
        ("Conversation:", [(detail.conversation_id, ds.identifier)]),
    ]
    if detail.parent_id:
        pairs.append(("Parent:", [(detail.parent_id, ds.identifier)]))
    if detail.external_id:
        pairs.append(("External ID:", [(detail.external_id, ds.identifier)]))
    if detail.timestamp:
        pairs.append(("Timestamp:", [(str(detail.timestamp), ds.temporal)]))
    if detail.tags:
        pairs.append(("Tags:", [(", ".join(detail.tags), ds.tag)]))
    if detail.kind == "response" and detail.kind_specific:
        ks = detail.kind_specific
        if ks.get("model"):
            pairs.append(("Model:", [(ks["model"], ds.model)]))
        in_toks = ks.get("input_tokens") or 0
        out_toks = ks.get("output_tokens") or 0
        if in_toks or out_toks:
            pairs.append((
                "Tokens:",
                [(f"{fmt_tokens(in_toks)} in / {fmt_tokens(out_toks)} out", ds.metric)],
            ))
        children = ks.get("tool_calls") or []
        if children:
            pairs.append(("Tool calls:", [(str(len(children)), ds.metric)]))
    if detail.kind == "tool_call" and detail.kind_specific:
        ks = detail.kind_specific
        if ks.get("tool_name"):
            pairs.append((
                "Tool:",
                [(f"{ks['tool_name']} ({ks.get('status') or 'unknown'})", ds.tool_name)],
            ))
    if include_neighbors and detail.neighbors:
        nb = detail.neighbors
        if nb.get("prev_event_id"):
            pairs.append(("Prev:", [(nb["prev_event_id"], ds.identifier)]))
        if nb.get("next_event_id"):
            pairs.append(("Next:", [(nb["next_event_id"], ds.identifier)]))
    if detail.content_blocks:
        pairs.append(("Content blocks:", [(str(len(detail.content_blocks)), ds.metric)]))

    print_definitions(pairs, indent=0, label_style=ds.temporal)
    return 0


def _query_detail(args) -> int:
    """Show conversation detail timeline."""
    from siftd.api import get_conversation
    from siftd.api.conversations import AnchorNotFound, AnchorOutOfRange, AnchorPhraseInvalid
    from siftd.api.dispatch import Operation, execute
    from siftd.cli._common import fidelity_from_args, tool_chars_from_args
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    exchanges_n = getattr(args, "exchanges", None)
    turns_range = getattr(args, "turns_range", None)

    # Detect which anchor (if any) was specified.
    from_start = getattr(args, "from_start", False)
    from_end = getattr(args, "from_end", False)
    at_turn = getattr(args, "at_turn", None)
    around = getattr(args, "around", None)
    has_anchor = from_start or from_end or (at_turn is not None) or (around is not None)
    has_window = (exchanges_n is not None) or (turns_range is not None)

    # Window without anchor is a hard error (exit 2, argparse convention).
    _ANCHOR_HINT = "use one of: --from-start, --from-end, --at-turn N, --around PHRASE"
    if has_window and not has_anchor:
        flag = "--exchanges" if exchanges_n is not None else "--turns"
        print(
            f"error: {flag} requires an anchor; {_ANCHOR_HINT}",
            file=sys.stderr,
        )
        sys.exit(2)

    if exchanges_n is not None and exchanges_n < 1:
        print("error: --exchanges must be at least 1", file=sys.stderr)
        sys.exit(2)

    # Resolve anchor type and value.
    anchor: str | None = None
    anchor_value: int | str | None = None
    if from_start:
        anchor = "from_start"
    elif from_end:
        anchor = "from_end"
    elif at_turn is not None:
        anchor = "at_turn"
        anchor_value = at_turn
    elif around is not None:
        anchor = "around"
        anchor_value = around

    # Translate --exchanges N to window offsets (direction depends on anchor).
    window_start: int | None = None
    window_end: int | None = None
    if exchanges_n is not None:
        if anchor == "from_end":
            # tail window: N turns back from end
            window_start = -(exchanges_n - 1)
            window_end = 0
        else:
            # forward window: N turns from anchor
            window_start = 0
            window_end = exchanges_n - 1

    # Parse --turns A:B into window offsets.
    if turns_range is not None:
        window_start, window_end = _parse_turns_range(turns_range)

    db = Path(args.db) if args.db else None
    effective_db = db or resolve_db(args)

    fidelity = fidelity_from_args(args)
    tool_chars = tool_chars_from_args(args, fidelity)

    tools_flag = getattr(args, "tools", None)
    tool_filter = None
    if tools_flag is not None and tools_flag != "all":
        tool_filter = tools_flag

    op = Operation(
        path=f"/api/v1/conversations/{args.conversation_id}",
        method="GET",
        fn=get_conversation,
        params={
            "id": args.conversation_id,
            "fidelity": fidelity,
            "db_path": db,
            "tool_filter": tool_filter,
            "anchor": anchor,
            "anchor_value": anchor_value,
            "window_start": window_start,
            "window_end": window_end,
        },
        render_method="detail",
        fidelity=fidelity,
        db=effective_db,
    )

    # Delegate to serve when available, reconstructing ConversationDetail via
    # from_wire so the local renderer below consumes the result identically
    # to a local fetch. See docs/guides/delegation-contract.md.
    detail = None
    delegated_response = None
    if not getattr(args, "summary", False):
        try:
            delegated_response = try_serve(op)
        except ServeRequest4xx as e:
            print_serve_4xx(e)
            return 1
    if delegated_response is not None:
        from siftd.api.dispatch import from_wire
        # Deserializers return None on schema mismatch rather than raising;
        # the local-execute fallback below handles that uniformly.
        detail = from_wire(op, delegated_response)

    # Ambiguous-match pre-pass: when --around is set, check for multiple matches
    # and report them to stderr so the user can pick with --at-turn. This is a
    # local-DB-only UX nicety; on a true thin-client without a local DB it
    # silently degrades to no hint.
    if around is not None:
        try:
            from siftd.api import open_database
            from siftd.api.conversations import resolve_entity_id
            from siftd.api.search import _events_to_turn_indices, phrase_events_in_conversation
            _pre_conn = open_database(effective_db, read_only=True)
            try:
                _conv_id = resolve_entity_id(_pre_conn, "conversation", args.conversation_id)
                if _conv_id:
                    _all_events = phrase_events_in_conversation(_pre_conn, around, conversation_id=_conv_id)
                    if len(_all_events) > 1:
                        _turn_indices = _events_to_turn_indices(_pre_conn, _all_events, _conv_id)
                        first_turn = _turn_indices[0] if _turn_indices else "?"
                        others = sorted(
                            {t for t in _turn_indices if t is not None}
                            - ({first_turn} if first_turn is not None else set())
                        )
                        status.info(
                            f"matched {len(_all_events)} turns; showing first (turn {first_turn}).",
                            hint=f"Use --at-turn <N> for others: {others}",
                        )
            finally:
                _pre_conn.close()
        except Exception:
            pass  # No local DB or other failure — skip the hint silently.

    if detail is None:
        try:
            detail = execute(op)
        except FileNotFoundError as e:
            status.error(str(e), hint="Run 'siftd ingest' to create it.")
            return 1
        except AnchorOutOfRange as e:
            print(f"error: --at-turn {at_turn} is out of range (conversation has {e.turn_count} turns)", file=sys.stderr)
            sys.exit(2)
        except AnchorNotFound as e:
            print(
                f"error: --around {e.phrase!r} not found in conversation\n"
                f"Try 'siftd search \"{e.phrase}\"' to locate conversations containing this phrase, "
                f"or shorten the phrase.",
                file=sys.stderr,
            )
            sys.exit(2)
        except AnchorPhraseInvalid as e:
            print(f"error: --around {e.phrase!r} is not a valid FTS5 phrase", file=sys.stderr)
            sys.exit(2)

    if not detail:
        status.error(f"Conversation not found: {args.conversation_id}")
        return 1

    # Summary mode: just metadata, no turns. Reuse the painted_bridge header so a
    # --summary reads identically to a full detail's top (the single-sourced twin).
    if getattr(args, "summary", False):
        from siftd.output.painted_bridge import render_conversation_summary_block

        emit_output(render_conversation_summary_block(detail, fidelity=fidelity))
        return 0

    from siftd.output.format_registry import select_format

    fmt = select_format(
        json_mode=getattr(args, "json", False),
        is_tty=sys.stdout.isatty(),
    )
    result = fmt.render_detail(
        detail.turns, fidelity, detail=detail, tool_chars=tool_chars,
    )
    emit_output(result)
    return 0


def _query_sql(args) -> int:
    """Deprecated alias for `siftd report`. Routes named-SQL to the report handler.

    Named-SQL composes from neither the conversation list nor the detail view;
    it only looked unified by squatting query's positional slot. Kept working
    (alias-first migration) with a one-time deprecation notice to stderr.
    """
    from siftd.cli._common import deprecation_notice
    from siftd.cli.report import run_report

    deprecation_notice("query sql", "report")
    db = Path(args.db) if args.db else None
    return run_report(args.sql_name, args.var, db)


def _emit_stats_footer(view_convs: int, view_tokens: int, corpus, corpus_tokens: int) -> None:
    """Print the --stats view/corpus comparison as a themed metric listing.

    The command's actual answer (a summary), so → stdout. Single-sources the
    footer the empty-list and populated-list paths both show; the view/corpus
    counts ride the amber ``ds.metric`` thread, consistent with the list table's
    token/turn columns and the detail header.
    """
    from siftd.output.listing import print_definitions
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    print()
    print_definitions(
        [
            (
                "Conversations:",
                [(f"{view_convs:,} / {corpus.total_conversations:,} corpus", ds.metric)],
            ),
            (
                "View tokens:",
                [(f"{fmt_tokens(view_tokens)} / {fmt_tokens(corpus_tokens)} corpus", ds.metric)],
            ),
        ],
        indent=0,
        label_style=ds.temporal,
    )


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

    # Dispatch to detail view: classify the ID once, then route. Pass the
    # probe connection through to the event path so we don't re-open.
    if args.conversation_id:
        return _dispatch_detail(args)

    from dataclasses import asdict

    from siftd.api import list_conversations
    from siftd.api.dispatch import Operation, deserialize_caveats, execute_for_render, from_wire
    from siftd.cli._filters import extract_filter_args
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    db = resolve_db(args)
    filters = extract_filter_args(args)
    fidelity = fidelity_from_args(args)
    # Apply -v before Operation is built so caveat producers' applies_to
    # predicates see the full depth and fire when the user actually wants
    # the verbose surface (cost column, etc.).
    if args.verbose:
        fidelity = fidelity.with_depth(3)

    op = Operation(
        path="/api/v1/conversations",
        method="GET",
        fn=list_conversations,
        params={
            "fidelity": fidelity,
            "db_path": db,
            **{k: v for k, v in asdict(filters).items() if v is not None},
            "n": args.limit,
            "oldest": args.oldest,
        },
        render_method="list",
        fidelity=fidelity,
        db=db,
    )

    caveats: list = []

    # Try serve, fall back to local execution. Use from_wire so the
    # deserialization is canonical (preserves the owner field, applies
    # type coercion uniformly with the rest of the wire-form contract).
    # The list deserializer returns None on schema mismatch (sentinel for
    # fallback) and [] for a legitimately empty list (not a fallback signal).
    conversations = None
    try:
        delegated = try_serve(op)
    except ServeRequest4xx as e:
        print_serve_4xx(e)
        return 1
    if delegated is not None and isinstance(delegated, dict):
        conversations = from_wire(op, delegated)
        if conversations is not None:
            # I5: thread the server's caveats back so the thin client surfaces
            # the same editorial-honesty warnings local execution would.
            caveats = deserialize_caveats(delegated)
    if conversations is None:
        try:
            conversations, caveats = execute_for_render(op)
        except FileNotFoundError as e:
            status.error(str(e), hint="Run 'siftd ingest' to create it.")
            return 1
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if "no such table" in err_msg and "fts" in err_msg:
                status.error("FTS index not found.", hint="Run 'siftd ingest' first.")
            elif "fts5" in err_msg or "syntax" in err_msg:
                status.error(f"Invalid search query: {e}", hint="Check your search query for syntax errors.")
            else:
                status.error(f"Database error: {e}", hint="Run 'siftd doctor' to check database health.")
            return 1

    view_convs = len(conversations)
    view_tokens = sum(c.total_tokens for c in conversations)
    corpus = None
    corpus_tokens = 0
    if args.stats:
        from siftd.api.stats import get_usage_summary

        corpus = get_usage_summary(db_path=db)
        corpus_tokens = corpus.total_input_tokens + corpus.total_output_tokens

    if getattr(args, "no_hints", False):
        caveats = [c for c in caveats if c.severity != "hint"]

    if not conversations:
        if args.json:
            import json as _json
            from dataclasses import asdict
            json_caveats = [asdict(c) for c in caveats if c.channel != "text"]
            print(_json.dumps({"result": [], "caveats": json_caveats}, indent=2))
        else:
            status.info("No conversations found.")
            status.caveats([c for c in caveats if c.channel != "json"])
        if args.stats and corpus is not None:
            _emit_stats_footer(view_convs, view_tokens, corpus, corpus_tokens)
        return 0

    # Render list via formatter (fidelity already includes -v; reuse op.fidelity)
    from siftd.output.format_registry import select_format

    fmt = select_format(json_mode=args.json, is_tty=sys.stdout.isatty())
    output = fmt.render_list(conversations, op.fidelity, caveats=caveats)
    emit_output(output)

    # Stats summary (shown after list when --stats flag is set)
    if args.stats and corpus is not None:
        _emit_stats_footer(view_convs, view_tokens, corpus, corpus_tokens)

    return 0


def build_query_parser(subparsers) -> None:
    """Add 'query' subparser."""
    # query
    p_query = subparsers.add_parser(
        "query",
        help="List and filter conversations by metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""List and filter conversations by metadata (workspace, model, date, tags).
To read one conversation: siftd show <id>.  For content search: siftd search <query>.

Conversation IDs in lists are truncated to 12 characters for display; any unambiguous
prefix works — e.g. 'siftd show 01ABCDEF01AB --summary' resolves without a full 26-character ID.
If a prefix matches multiple conversations, the command exits with code 2 and lists the matched IDs.

examples:
  siftd query                                   # list recent conversations
  siftd query -n 20                             # list 20 conversations
  siftd query -w myproject                      # filter by workspace
  siftd query -l research:auth                  # conversations tagged research:auth
  siftd query -l research: -l useful:           # OR — any research: or useful: tag
  siftd query --all-tags important --all-tags reviewed  # AND — must have both
  siftd query -l research: --no-tag archived    # combine OR + NOT
  siftd query --tool-tag shell:test             # conversations with test commands

read one conversation (canonical verb is 'siftd show'; see 'siftd show --help'):
  siftd show <id>                               # read a conversation in detail
  siftd query <id>                              # alias for 'siftd show <id>'

named SQL reports moved to 'siftd report' ('query sql' still works, deprecated):
  siftd report                                 # list saved SQL reports
  siftd report cost                            # run the 'cost' report
  siftd report cost --var ws=proj              # run with variable substitution""",
    )

    # Positional arguments
    p_query.add_argument("conversation_id", nargs="?", help="Conversation ID for detail view")
    # 'sql_name' supports the deprecated `query sql <name>` alias; use `siftd report`.
    p_query.add_argument("sql_name", nargs="?", help=argparse.SUPPRESS)

    # Filtering options
    from siftd.cli._common import add_anchor_window_args, add_fidelity_args, add_output_args
    from siftd.cli._filters import add_filter_args

    add_filter_args(p_query, include_tool=True, include_tool_tag=True)
    add_output_args(p_query, json=True, limit=True, limit_default=None, no_hints=True)
    add_fidelity_args(p_query, full=True, brief=True, chars=True, thinking=True, tools=True, tool_chars=True)

    # Anchor + window flags for detail view (Slice 1: query <id>; Slice 2: search)
    add_anchor_window_args(p_query)

    # List options
    list_group = p_query.add_argument_group("list options")
    list_group.add_argument("-v", "--verbose", action="store_true", help="Full table with all columns")
    list_group.add_argument("--oldest", action="store_true", help="Sort by oldest first (default: newest first)")
    list_group.add_argument("--stats", action="store_true", help="Show summary totals after list")

    # Detail view options (when conversation_id is provided)
    detail_group = p_query.add_argument_group("detail view")
    detail_group.add_argument("--summary", action="store_true", help="Summary only (metadata, no turns)")
    detail_group.add_argument("--neighbors", action="store_true",
        help="Include prev_event_id/next_event_id in event detail output")

    # Deprecated `query sql <name> --var` alias support (use `siftd report`).
    p_query.add_argument("--var", action="append", metavar="KEY=VALUE", help=argparse.SUPPRESS)

    _SEARCH_HINT_FLAGS = frozenset(["-s", "--search", "--fts", "--semantic"])

    def _query_unknown_hint(unknowns):
        for u in unknowns:
            if u in _SEARCH_HINT_FLAGS:
                return 'Did you mean: siftd search "<query>"?'
        return None

    p_query.set_defaults(func=cmd_query, _unknown_hint=_query_unknown_hint)
