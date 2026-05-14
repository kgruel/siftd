"""CLI handler for peek command (inspect live sessions from disk)."""

import argparse
import sys


def cmd_peek(args) -> int:
    """Inspect live sessions directly from disk."""
    import json as _json

    from siftd.api import (
        find_session_file,
        list_active_sessions,
        read_session_detail,
        tail_session,
    )
    from siftd.cli._common import fidelity_from_args, tool_chars_from_args
    from siftd.output.painted_bridge import emit_output, render_follow_event_block, render_peek_detail_block
    from siftd.output.painted_bridge import print_block as print_painted_block
    from siftd.peek import AmbiguousSessionError

    # Extract flags
    last_response = getattr(args, "last_response", False)
    last_prompt = getattr(args, "last_prompt", False)
    follow = getattr(args, "follow", False)
    is_full = getattr(args, "full", False)
    include_thinking = getattr(args, "thinking", False) or is_full

    # Validate mutual exclusivity
    if last_response and last_prompt:
        print("Error: --last-response and --last-prompt are mutually exclusive")
        return 1

    # --follow is mutually exclusive with --tail, --last-response, --last-prompt
    if follow and (getattr(args, "tail", False) or last_response or last_prompt):
        conflicting = []
        if getattr(args, "tail", False):
            conflicting.append("--tail")
        if last_response:
            conflicting.append("--last-response")
        if last_prompt:
            conflicting.append("--last-prompt")
        print(f"Error: --follow is mutually exclusive with {', '.join(conflicting)}")
        return 1

    # --last-response/--last-prompt are mutually exclusive with formatting flags
    if (last_response or last_prompt) and (args.json or getattr(args, "tail", False)):
        conflicting = "--json" if args.json else "--tail"
        flag = "--last-response" if last_response else "--last-prompt"
        print(f"Error: {flag} is mutually exclusive with {conflicting}")
        return 1

    # Validate --limit
    if args.limit is not None and args.limit < 1:
        print("Error: --limit must be at least 1")
        return 1

    # Validate --exchanges
    exchanges_n = getattr(args, "exchanges", None)

    if exchanges_n is not None and exchanges_n < 1:
        print("Error: --exchanges must be at least 1")
        return 1

    fidelity = fidelity_from_args(args)
    tool_chars = tool_chars_from_args(args, fidelity)

    # --last-response / --last-prompt mode: extract single text, output raw
    if last_response or last_prompt:
        # Resolve session: use provided ID or default to most recent active
        if args.session_id:
            try:
                path = find_session_file(args.session_id)
            except AmbiguousSessionError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            if path is None:
                print(f"Session not found: {args.session_id}", file=sys.stderr)
                return 1
        else:
            # Default to most recent active session
            sessions = list_active_sessions(
                limit=1,
                workspace=args.workspace,
                branch=getattr(args, "branch", None),
            )
            if not sessions:
                print("No active sessions found.", file=sys.stderr)
                print("Tip: Use 'siftd query' to search ingested conversations.", file=sys.stderr)
                return 1
            path = sessions[0].file_path

        # Read just the last exchange
        detail = read_session_detail(path, last_n=1, include_thinking=include_thinking)
        if detail is None:
            print(f"Could not read session: {path}", file=sys.stderr)
            return 1

        if not detail.exchanges:
            print("No exchanges found in session.", file=sys.stderr)
            return 1

        last_exchange = detail.exchanges[-1]
        if last_response:
            text = last_exchange.response_text
            if not text:
                print("No response text found in last exchange.", file=sys.stderr)
                return 1
        else:  # last_prompt
            text = last_exchange.prompt_text
            if not text:
                print("No prompt text found in last exchange.", file=sys.stderr)
                return 1

        # Output raw text (no formatting, suitable for piping)
        print(text)
        return 0

    # Follow mode
    if follow:
        from siftd.peek import follow_session, read_session_detail
        from siftd.peek.follow import FollowEvent, event_to_json

        timeout = getattr(args, "timeout", None)

        # Resolve session: use provided ID or default to most recent active
        if args.session_id:
            try:
                path = find_session_file(args.session_id)
            except AmbiguousSessionError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            if path is None:
                print(f"Session not found: {args.session_id}", file=sys.stderr)
                return 1
        else:
            sessions = list_active_sessions(
                limit=1,
                workspace=args.workspace,
                branch=getattr(args, "branch", None),
            )
            if not sessions:
                print("No active sessions found.", file=sys.stderr)
                return 1
            path = sessions[0].file_path

        initial_n = exchanges_n if exchanges_n is not None else 3

        if not args.json:
            detail = read_session_detail(
                path,
                last_n=initial_n,
                include_thinking=include_thinking,
            )
            if detail is not None:
                initial_block = render_peek_detail_block(
                    detail,
                    exchanges=detail.exchanges,
                    fidelity=fidelity,
                    tool_chars=tool_chars,
                )
                print_painted_block(initial_block)
                print()

            print("--- following ---", file=sys.stderr)

            def _render_follow_event(event: FollowEvent) -> None:
                block = render_follow_event_block(
                    event,
                    fidelity=fidelity,
                    tool_chars=tool_chars,
                )
                print_painted_block(block)
                print()
                sys.stdout.flush()

            follow_session(
                path,
                json_mode=False,
                render=_render_follow_event,
                include_thinking=include_thinking,
                timeout=timeout,
            )
        else:
            # JSON mode: initial context as NDJSON
            detail = read_session_detail(
                path,
                last_n=initial_n,
                include_thinking=include_thinking,
            )
            if detail is not None:
                for ex in detail.exchanges:
                    if ex.prompt_text:
                        user_ev = FollowEvent(timestamp=ex.timestamp, text=ex.prompt_text, is_user=True)
                        print(_json.dumps(event_to_json(user_ev), separators=(",", ":")))
                    if ex.response_text or ex.tool_calls or ex.narrative:
                        tc = [(n, c, []) for n, c in ex.tool_calls]
                        asst_ev = FollowEvent(
                            timestamp=ex.timestamp, text=ex.response_text,
                            tool_calls=tc, narrative=ex.narrative, input_tokens=ex.input_tokens,
                            output_tokens=ex.output_tokens,
                        )
                        print(_json.dumps(event_to_json(asst_ev), separators=(",", ":")))

            follow_session(path, json_mode=True, include_thinking=include_thinking, timeout=timeout)

        return 0

    # Detail mode: session ID provided
    if args.session_id:
        try:
            path = find_session_file(args.session_id)
        except AmbiguousSessionError as e:
            print(f"Error: {e}")
            return 1

        if path is None:
            print(f"Session not found: {args.session_id}")
            return 1

        # Tail mode
        if args.tail:
            tail_lines = getattr(args, "tail_lines", 20)
            # Use raw=True for line-oriented output (one JSON per line)
            lines = tail_session(path, lines=tail_lines, raw=True)
            if args.json:
                # Wrap in JSON array
                records = []
                for line in lines:
                    try:
                        records.append(_json.loads(line))
                    except (ValueError, _json.JSONDecodeError):
                        records.append(line)
                print(_json.dumps(records, indent=2))
            else:
                # Raw JSONL output (one per line)
                for line in lines:
                    print(line)
            return 0

        # Detail mode
        # Use --exchanges if provided, otherwise default to 5
        last_n = exchanges_n if exchanges_n is not None else 5
        detail = read_session_detail(
            path,
            last_n=last_n,
            include_thinking=include_thinking,
        )
        if detail is None:
            print(f"Could not read session: {path}")
            return 1

        if args.json:
            out = {
                "session_id": detail.info.session_id,
                "file_path": str(detail.info.file_path),
                "workspace_path": detail.info.workspace_path,
                "workspace_name": detail.info.workspace_name,
                "branch": detail.info.branch,
                "model": detail.info.model,
                "started_at": detail.started_at,
                "exchange_count": detail.info.exchange_count,
                "adapter": detail.info.adapter_name,
                "parent_session_id": detail.info.parent_session_id,
                "exchanges": [
                    {
                        "timestamp": ex.timestamp,
                        "prompt_text": ex.prompt_text,
                        "response_text": ex.response_text,
                        "tool_calls": [{"name": n, "count": c} for n, c in ex.tool_calls],
                        "input_tokens": ex.input_tokens,
                        "output_tokens": ex.output_tokens,
                    }
                    for ex in detail.exchanges
                ],
            }
            print(_json.dumps(out, indent=2))
            return 0

        block = render_peek_detail_block(
            detail,
            exchanges=detail.exchanges,
            fidelity=fidelity,
            tool_chars=tool_chars,
        )
        print_painted_block(block)
        return 0

    # List mode
    # Warn about detail-only flags that are silently ignored in list mode
    ignored = []
    if getattr(args, "tail", False):
        ignored.append("--tail")
    if getattr(args, "tail_lines", 20) != 20:
        ignored.append("--tail-lines")
    if exchanges_n is not None:
        ignored.append("--exchanges")
    if getattr(args, "tools", False):
        ignored.append("--tools")
    if ignored:
        print(f"Note: {', '.join(ignored)} ignored in list mode (requires session ID)", file=sys.stderr)

    # Use --limit if provided, otherwise default to 10
    limit = args.limit if args.limit is not None else 10
    sessions = list_active_sessions(
        workspace=args.workspace,
        branch=getattr(args, "branch", None),
        include_inactive=args.all,
        limit=limit,
    )

    # Apply --main-only filter
    if getattr(args, "main_only", False):
        sessions = [s for s in sessions if s.parent_session_id is None]

    # Apply --children filter (show only children of specified parent)
    children_filter = getattr(args, "children", None)
    if children_filter:
        sessions = [s for s in sessions if s.parent_session_id and s.parent_session_id.startswith(children_filter)]

    if not sessions:
        if args.json:
            print("[]")
        else:
            print("No active sessions found.")
            print("Tip: Use 'siftd query' to search ingested conversations.", file=sys.stderr)
        return 0

    if args.json:
        out = [
            {
                "session_id": s.session_id,
                "file_path": str(s.file_path),
                "workspace_path": s.workspace_path,
                "workspace_name": s.workspace_name,
                "branch": s.branch,
                "model": s.model,
                "last_activity": s.last_activity,
                "exchange_count": s.exchange_count,
                "adapter": s.adapter_name,
                "preview_available": s.preview_available,
                "parent_session_id": s.parent_session_id,
            }
            for s in sessions
        ]
        print(_json.dumps(out, indent=2))
        return 0

    # Build parent->children mapping for grouping display
    children_by_parent: dict[str, list] = {}
    for s in sessions:
        if s.parent_session_id:
            children_by_parent.setdefault(s.parent_session_id, []).append(s)

    # Track which parent session IDs are actually in our result set
    session_ids_in_results = {s.session_id for s in sessions}

    # Filter to displayable rows: skip children whose parent is visible
    display_sessions = [
        s for s in sessions
        if not s.parent_session_id or s.parent_session_id not in session_ids_in_results
    ]

    from siftd.output.painted_bridge import render_peek_list_block

    block = render_peek_list_block(display_sessions, children_by_parent)
    emit_output(block)

    return 0


def build_peek_parser(subparsers) -> None:
    """Add the 'peek' subparser to the CLI."""
    p_peek = subparsers.add_parser(
        "peek",
        help="Inspect live sessions from disk (bypasses SQLite)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd peek                    # list latest 10 sessions
  siftd peek -n 5               # list latest 5 sessions
  siftd peek --all              # list all sessions (no time limit)
  siftd peek --all -n 50        # list all, but only first 50
  siftd peek -w myproject       # filter by workspace name
  siftd peek c520f862           # detail view for session (last 5 exchanges)
  siftd peek c520 --exchanges 10  # show last 10 exchanges
  siftd peek c520 --thinking    # show thinking blocks inline
  siftd peek c520 --tools       # show tool inputs/results inline when available
  siftd peek c520 --brief       # compact detail view (80 char truncation)
  siftd peek c520 -b            # short alias for --brief
  siftd peek c520 --full        # show full text (no truncation)
  siftd peek c520 -F            # short alias for --full
  siftd peek c520 --tail        # raw JSONL tail
  siftd peek c520 --tail --json # tail as JSON array
  siftd peek --main-only        # exclude subagent sessions
  siftd peek --children abc123  # show children of parent session
  siftd peek --last-response    # output last assistant response (raw text)
  siftd peek --last-prompt      # output last user prompt (raw text)
  siftd peek c520 --last-response  # last response from specific session
  siftd peek c520 --follow      # follow a live session in real time
  siftd peek --follow            # follow most recent active session
  siftd peek --follow --json    # follow as NDJSON (one object per line)

NOTE: Session content may contain sensitive information (API keys, credentials, etc.).""",
    )
    from siftd.cli._common import add_fidelity_args, add_output_args

    p_peek.add_argument("session_id", nargs="?", help="Session ID prefix for detail view")

    add_output_args(p_peek, json=True, limit=True, limit_default=None)
    add_fidelity_args(p_peek, full=True, brief=True, chars=True, thinking=True)

    # peek-specific session filters
    session_group = p_peek.add_argument_group("session filters")
    session_group.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace name substring")
    session_group.add_argument("--branch", metavar="SUBSTR", help="Filter by worktree branch substring")
    session_group.add_argument("--all", action="store_true", help="Include inactive sessions (not just last 2 hours)")
    session_group.add_argument("--main-only", action="store_true", help="Only show main sessions (exclude subagents)")
    session_group.add_argument("--children", metavar="ID", help="Show only children of the specified parent session")

    # peek-specific detail/follow controls
    detail_group = p_peek.add_argument_group("detail and follow")
    detail_group.add_argument("--exchanges", type=int, metavar="N", help="Detail mode: number of exchanges to show (default: 5)")
    detail_group.add_argument("--tools", action="store_true", help="Show tool inputs/results inline when available")
    detail_group.add_argument("-f", "--follow", action="store_true", help="Follow a live session in real time (like tail -f)")
    detail_group.add_argument("--timeout", type=float, metavar="SECONDS", help="Exit after SECONDS of wall-clock time (for use with --follow)")
    detail_group.add_argument("--tail", action="store_true", help="Raw JSONL tail (last 20 records)")
    detail_group.add_argument("--tail-lines", type=int, default=20, metavar="N", dest="tail_lines", help="Number of records for --tail (default: 20)")
    detail_group.add_argument("--last-response", action="store_true", help="Output only the last assistant response (raw text, no formatting)")
    detail_group.add_argument("--last-prompt", action="store_true", help="Output only the last user prompt (raw text, no formatting)")

    p_peek.set_defaults(func=cmd_peek)
