"""CLI handler for peek command (inspect live sessions from disk)."""

import argparse
import sys


def cmd_peek(args) -> int:
    """Inspect live sessions directly from disk."""
    import json as _json
    import time

    from siftd.api import (
        find_session_file,
        list_active_sessions,
        read_session_detail,
        tail_session,
    )
    from siftd.output import fmt_ago, fmt_model, fmt_timestamp, fmt_tokens, print_indented, truncate_text
    from siftd.peek import AmbiguousSessionError

    # Extract --last-response and --last-prompt flags
    last_response = getattr(args, "last_response", False)
    last_prompt = getattr(args, "last_prompt", False)

    # Validate mutual exclusivity
    if last_response and last_prompt:
        print("Error: --last-response and --last-prompt are mutually exclusive")
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

    # Determine truncation limit
    chars_limit = 200
    if getattr(args, "full", False):
        chars_limit = 0  # No truncation
    elif getattr(args, "chars", None) is not None:
        chars_limit = args.chars

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
                branch=getattr(args, "branch", None),
            )
            if not sessions:
                print("No active sessions found.", file=sys.stderr)
                return 1
            path = sessions[0].file_path

        # Read just the last exchange
        detail = read_session_detail(path, last_n=1)
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
        detail = read_session_detail(path, last_n=last_n)
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

        # Header
        ws = detail.info.workspace_name or ""
        model = detail.info.model or "unknown"
        started = fmt_timestamp(detail.started_at, time_only=True)

        print(detail.info.session_id)
        parts = []
        if ws:
            parts.append(ws)
        parts.append(model)
        if started:
            parts.append(f"started {started}")
        parts.append(f"{detail.info.exchange_count} exchanges")
        print(" \u00b7 ".join(parts))
        # Add file path to detail header
        print(f"file: {detail.info.file_path}")
        print()

        # Exchanges
        for ex in detail.exchanges:
            ts = fmt_timestamp(ex.timestamp, time_only=True)

            # Prompt
            if ex.prompt_text is not None:
                print(f"[{ts}] user")
                text = truncate_text(ex.prompt_text, chars_limit)
                print_indented(text)
                print()

            # Response
            if ex.response_text is not None or ex.tool_calls:
                token_info = f"{fmt_tokens(ex.input_tokens)} in / {fmt_tokens(ex.output_tokens)} out"
                print(f"[{ts}] assistant ({token_info})")
                if ex.response_text:
                    text = truncate_text(ex.response_text, chars_limit)
                    print_indented(text)
                if ex.tool_calls:
                    tool_parts = []
                    for name, count in ex.tool_calls:
                        if count > 1:
                            tool_parts.append(f"{name} \u00d7{count}")
                        else:
                            tool_parts.append(name)
                    print(f"  \u2192 {', '.join(tool_parts)}")
                print()

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

    now = time.time()
    for s in sessions:
        # Skip children only if their parent is visible in results
        # (orphaned children whose parent is filtered out should still show)
        if s.parent_session_id and s.parent_session_id in session_ids_in_results:
            continue

        sid = s.session_id[:8]
        ws = s.workspace_name or ""
        if s.branch:
            ws = f"{ws} [{s.branch}]" if ws else f"[{s.branch}]"
        ago = fmt_ago(now - s.last_activity)
        if s.preview_available:
            exchanges = f"{s.exchange_count} exchanges"
        else:
            exchanges = "(preview unavailable)"
        model = fmt_model(s.model)

        # Add child count suffix if this session has children in results
        child_count = len(children_by_parent.get(s.session_id, []))
        suffix = f" (+{child_count} agents)" if child_count > 0 else ""

        print(f"  {sid}  {ws:<16s} {ago:<12s} {exchanges:<16s} {model}{suffix}")

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
  siftd peek c520 --full        # show full text (no truncation)
  siftd peek c520 --tail        # raw JSONL tail
  siftd peek c520 --tail --json # tail as JSON array
  siftd peek --main-only        # exclude subagent sessions
  siftd peek --children abc123  # show children of parent session
  siftd peek --last-response    # output last assistant response (raw text)
  siftd peek --last-prompt      # output last user prompt (raw text)
  siftd peek c520 --last-response  # last response from specific session

NOTE: Session content may contain sensitive information (API keys, credentials, etc.).""",
    )
    p_peek.add_argument("session_id", nargs="?", help="Session ID prefix for detail view")
    p_peek.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace name substring")
    p_peek.add_argument("--branch", metavar="SUBSTR", help="Filter by worktree branch substring")
    p_peek.add_argument("--all", action="store_true", help="Include inactive sessions (not just last 2 hours)")
    p_peek.add_argument("-n", "--limit", type=int, metavar="N", help="Maximum number of sessions to list (default: 10)")
    p_peek.add_argument("--exchanges", type=int, metavar="N", help="Detail mode: number of exchanges to show (default: 5)")
    p_peek.add_argument("--full", action="store_true", help="Show full text (no truncation)")
    p_peek.add_argument("--chars", type=int, metavar="N", help="Truncate text at N characters (default: 200)")
    p_peek.add_argument("--tail", action="store_true", help="Raw JSONL tail (last 20 records)")
    p_peek.add_argument("--tail-lines", type=int, default=20, metavar="N", dest="tail_lines", help="Number of records for --tail (default: 20)")
    p_peek.add_argument("--json", action="store_true", help="Output as structured JSON")
    p_peek.add_argument("--main-only", action="store_true", help="Only show main sessions (exclude subagents)")
    p_peek.add_argument("--children", metavar="ID", help="Show only children of the specified parent session")
    p_peek.add_argument("--last-response", action="store_true", help="Output only the last assistant response (raw text, no formatting)")
    p_peek.add_argument("--last-prompt", action="store_true", help="Output only the last user prompt (raw text, no formatting)")
    p_peek.set_defaults(func=cmd_peek)
