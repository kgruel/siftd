"""CLI handlers for tag command (apply, remove, list, rename, delete)."""

import argparse
import sys
from pathlib import Path

from siftd.api import (
    AmbiguousPrefix,
    apply_tags,
    create_database,
    delete_tag_safe,
    list_tags,
    open_database,
    rename_tag_safe,
)
from siftd.api.sessions import is_session_registered
from siftd.api.sessions import queue_tag as queue_pending_tag
from siftd.cli._common import print_ambiguous_error, resolve_db
from siftd.output import fmt_count, status
from siftd.output._id_format import short_id
from siftd.paths import ensure_dirs, session_id_file

# Subcommand names that can never collide with ULIDs (26-char base32).
_TAG_SUBCOMMANDS = frozenset({"apply", "remove", "list", "rename", "delete"})


def _detect_current_session() -> str | None:
    """Return the session ID for the current workspace, or None.

    Checks two sources:
    1. Session ID file (~/.local/state/siftd/sessions/<hash>/session-id)
    2. Active sessions table in the database (fallback)
    """
    import os

    workspace_path = str(Path(os.getcwd()).resolve())

    # Primary: check session-id file (written by session-start hook)
    sid_file = session_id_file(workspace_path)
    if sid_file.exists():
        session_id = sid_file.read_text().strip()
        if session_id:
            return session_id

    # Fallback: check active_sessions table
    try:
        from siftd.api import open_database
        from siftd.api.sessions import find_active_session
        from siftd.paths import db_path

        db = db_path()
        if db.exists():
            conn = open_database(db, read_only=True)
            try:
                session_id = find_active_session(conn, workspace_path)
                if session_id:
                    return session_id
            finally:
                conn.close()
    except Exception:
        pass

    return None


def _parse_tag_args(positional: list[str]) -> tuple[str, str, list[str]] | None:
    """Parse positional args for tag command.

    Returns (entity_type, entity_id, tag_names) or None if invalid.
    Supports:
      - <id> <tag> [tag2 ...]                    -> conversation, id, [tags]
      - <entity_type> <id> <tag> [tag2 ...]      -> entity_type, id, [tags]
    """
    if len(positional) >= 2:
        # Check if first arg is an entity type
        if positional[0] in ("conversation", "workspace", "tool_call", "prompt", "response", "exchange"):
            if len(positional) < 3:
                return None
            return (positional[0], positional[1], positional[2:])
        # Default: conversation
        return ("conversation", positional[0], positional[1:])
    return None


def _tag_session(args, db: Path, session_id: str) -> int:
    """Queue pending tags for a session (--session mode)."""
    ensure_dirs()

    # Create database if it doesn't exist
    conn = create_database(db)

    # Check if --remove was specified (not supported for --session)
    if args.remove:
        status.error(
            "--remove not supported with --session",
            hint="Use 'siftd doctor fix --pending-tags' to clear pending tags",
        )
        conn.close()
        return 1

    # Parse tag names from positional args
    tag_names = args.positional or []
    if not tag_names:
        print("Usage: siftd tag --session <id> <tag> [tag2 ...]")
        print("       siftd tag --session <id> --exchange <index> <tag> [tag2 ...]")
        print("       siftd tag --session <id> --last-response <tag> [tag2 ...]")
        conn.close()
        return 1

    # Check if session is registered (warn but proceed)
    if not is_session_registered(conn, session_id):
        status.warning(f"Session {short_id(session_id)}... not registered")

    # Argparse's mutually-exclusive group already enforced "at most one of
    # --exchange / --last-*"; this just maps the flag to (entity_type,
    # exchange_index, last_marker).
    entity_type, exchange_index, last_marker = _session_targeting(args)

    # Queue each tag
    queued = 0
    for tag_name in tag_names:
        try:
            result = queue_pending_tag(
                conn,
                session_id,
                tag_name,
                entity_type=entity_type,
                exchange_index=exchange_index,
                last_marker=last_marker,
                commit=False,
            )
        except ValueError as e:
            status.error(str(e))
            conn.close()
            return 1
        if result:
            queued += 1
            if last_marker:
                pretty = last_marker.replace("last_", "last ")
                status.confirm(f"Queued tag '{tag_name}' for {pretty} of session {short_id(session_id)}...")
            elif exchange_index is not None:
                status.confirm(f"Queued tag '{tag_name}' for exchange {exchange_index}")
            else:
                status.confirm(f"Queued tag '{tag_name}' for session {short_id(session_id)}...")
        else:
            status.info(f"Tag '{tag_name}' already queued")

    conn.commit()
    conn.close()
    return 0


# entity_type mirrors the kind being targeted; the resolver uses last_marker
# to pick the actual event at ingest time.
_LAST_FLAG_TO_KIND: dict[str, str] = {
    "last_prompt": "prompt",
    "last_response": "response",
    "last_exchange": "exchange",
    "last_tool_call": "tool_call",
}


def _session_targeting(args) -> tuple[str, int | None, str | None]:
    """Map argparse flags to (entity_type, exchange_index, last_marker).

    Mutual exclusion is enforced by argparse's mutually-exclusive group;
    this is a pure dispatch.
    """
    for flag, kind in _LAST_FLAG_TO_KIND.items():
        if getattr(args, flag, False):
            return (kind, None, flag)
    exchange_index = getattr(args, "exchange", None)
    if exchange_index is not None:
        return ("exchange", exchange_index, None)
    return ("conversation", None, None)


def _cmd_tag_list(args, db: Path) -> int:
    """List tags, or drill down into a specific tag's conversations."""
    if not db.exists():
        status.db_missing(db)
        return 1

    conn = open_database(db)

    # positional after "list": optional tag name for drill-down
    positional = args.positional or []
    # positional[0] is "list", rest is the drill-down name
    name = positional[1] if len(positional) > 1 else None

    # Drill-down: show conversations with a given tag
    if name:
        from dataclasses import asdict

        from siftd.api import list_conversations
        from siftd.cli._common import fidelity_from_args
        from siftd.cli._filters import extract_filter_args

        tag_name = name
        conn.close()

        filters = extract_filter_args(args)
        # Merge the drill-down tag with any -l tags from filter args
        filter_tags = filters.tag or []
        if tag_name not in filter_tags:
            filter_tags = [tag_name] + filter_tags
        filters.tag = filter_tags

        limit = getattr(args, "limit", None) or 10
        fidelity = fidelity_from_args(args)

        try:
            filter_kwargs = asdict(filters)
            filter_kwargs.pop("tag")  # pass explicitly below
            conversations = list_conversations(
                fidelity=fidelity, db_path=db, tag=filters.tag, n=limit, **filter_kwargs,
            )
        except FileNotFoundError as e:
            status.error(str(e))
            return 1

        if not conversations:
            status.info(f"No conversations found for tag: {tag_name}")
            return 0

        from siftd.output.format_registry import select_format

        print(f"Conversations tagged '{tag_name}' (showing {fmt_count(len(conversations))}):")
        fmt = select_format(
            json_mode=getattr(args, "json", False),
            is_tty=sys.stdout.isatty(),
        )
        output = fmt.render_list(conversations, fidelity)
        from siftd.output.painted_bridge import emit_output

        emit_output(output)

        if limit > 0 and len(conversations) >= limit:
            status.info(f"show more with `siftd query -l {tag_name} -n 0`")
        return 0

    # --by-workspace: group tag counts by workspace
    if getattr(args, "by_workspace", False):
        return _cmd_tag_list_by_workspace(args, db)

    # Default: list tags
    since = getattr(args, "since", None)
    before = getattr(args, "before", None)

    from painted import Fidelity

    from siftd.api.dispatch import Operation, execute
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    op = Operation(
        path="/api/v1/tags",
        method="GET",
        fn=list_tags,
        params={"db_path": db, "since": since, "before": before},
        render_method="raw",
        fidelity=Fidelity(),
        db=db,
    )

    tags = None

    # Try serve delegation
    try:
        result = try_serve(op)
    except ServeRequest4xx as e:
        conn.close()
        print_serve_4xx(e)
        return 1
    if result is not None and isinstance(result, dict) and "tags" in result:
        from siftd.api.tags import tag_info_list_from_dict

        conn.close()
        tags = tag_info_list_from_dict(result["tags"])

    if tags is None:
        conn.close()
        tags = execute(op)

    if not tags:
        if since or before:
            status.info("No tags found in the specified time range.")
        else:
            status.info("No tags defined.")
        return 0

    # When filtering temporally, hide tags with zero counts in the window
    if since or before:
        tags = [t for t in tags if (
            t.conversation_count or t.workspace_count or t.tool_call_count
            or t.exchange_count or t.prompt_count or t.response_count
        )]
        if not tags:
            status.info("No tags found in the specified time range.")
            return 0

    prefix = getattr(args, "prefix", None)
    if prefix:
        tags = [t for t in tags if t.name.startswith(prefix)]
        if not tags:
            status.info(f"No tags found with prefix: {prefix}")
            return 0

    # The tag name takes the teal `tag` role (the same colour the detail views
    # give it), the description reads plain, the counts recede to muted — a
    # parenthetical sentence, not a clean metric cell. Plain text is unchanged
    # from the former raw print() so a piped/NO_COLOR listing reads identically.
    from painted import current_palette, join_vertical, print_block

    from siftd.output.common import should_use_ansi
    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    p = current_palette()
    rows = []
    for tag in tags:
        counts = []
        if tag.conversation_count:
            counts.append(f"{fmt_count(tag.conversation_count)} conversations")
        if tag.workspace_count:
            counts.append(f"{fmt_count(tag.workspace_count)} workspaces")
        if tag.tool_call_count:
            counts.append(f"{fmt_count(tag.tool_call_count)} tool_calls")
        if tag.exchange_count:
            counts.append(f"{fmt_count(tag.exchange_count)} exchanges")
        if tag.prompt_count:
            counts.append(f"{fmt_count(tag.prompt_count)} prompts")
        if tag.response_count:
            counts.append(f"{fmt_count(tag.response_count)} responses")
        count_str = f" ({', '.join(counts)})" if counts else ""
        desc = f" - {tag.description}" if tag.description else ""
        line = row_line(
            [(tag.name, ds.tag), (desc, None), (count_str, p.muted)],
            indent="  ",
        )
        rows.append(line.to_block(line.width))

    if rows:
        print_block(join_vertical(*rows), use_ansi=should_use_ansi())
    return 0


_BY_WORKSPACE_REJECTED_FLAGS = (
    ("since", "--since"),
    ("before", "--before"),
    ("no_tag", "--no-tag"),
    ("tag", "-l/--tag"),
    ("model", "-m/--model"),
    ("tool", "--tool"),
    ("tool_tag", "--tool-tag"),
)


def _cmd_tag_list_by_workspace(args, db: Path) -> int:
    """List tag counts grouped by workspace."""
    import json as _json

    from siftd.api.tags import list_tags_by_workspace

    rejected = [
        flag for attr, flag in _BY_WORKSPACE_REJECTED_FLAGS
        if getattr(args, attr, None)
    ]
    if rejected:
        status.error(
            f"--by-workspace does not support: {', '.join(rejected)}.",
            hint="Supported filters: --on, --prefix, -w/--workspace, --owner, --all-tags, --limit.",
        )
        return 2

    if not db.exists():
        status.db_missing(db)
        return 1

    conn = open_database(db, read_only=True)
    target_kinds = tuple(getattr(args, "tag_kind", None) or ())
    prefix = getattr(args, "prefix", None)
    workspace_filter = getattr(args, "workspace", None)
    owner = getattr(args, "owner", None)
    all_tags = tuple(getattr(args, "all_tags", None) or ())
    limit_arg = getattr(args, "limit", None)
    limit = limit_arg if limit_arg is not None else 20

    workspaces = list_tags_by_workspace(
        conn,
        target_kinds=target_kinds or None,
        prefix=prefix,
        workspace_filter=workspace_filter,
        owner=owner,
        all_tags=all_tags or None,
        limit=limit,
    )
    conn.close()

    if not workspaces:
        status.info("No tag data found for the given filters.")
        return 0

    if getattr(args, "json", False):
        print(_json.dumps({"workspaces": workspaces}, indent=2))
        return 0

    for ws_data in workspaces:
        print(f"{ws_data['workspace']} ({fmt_count(ws_data['total'])} total)")
        for tag in ws_data["tags"]:
            print(f"  {tag['name']} ({fmt_count(tag['count'])} {tag['target_kind']}s)")
        print()

    return 0


def _cmd_tag_rename(args, db: Path) -> int:
    """Rename a tag: siftd tag rename <old> <new>."""
    positional = args.positional or []
    # positional[0] is "rename", [1] is old, [2] is new
    if len(positional) < 3:
        print("Usage: siftd tag rename <old> <new>")
        return 1

    old_name, new_name = positional[1], positional[2]

    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    op = Operation(
        path="/api/v1/tag",
        method="POST",
        fn=rename_tag_safe,
        params={
            "action": "rename",
            "old_name": old_name,
            "new_name": new_name,
            "db_path": db,
        },
        render_method="raw",
        fidelity=Fidelity(),
        db=db,
    )

    # Try serve delegation
    try:
        result = try_serve(op)
    except ServeRequest4xx as e:
        print_serve_4xx(e)
        return 1
    if result is not None and isinstance(result, dict) and result.get("status") == "renamed":
        status.confirm(f"Renamed '{old_name}' \u2192 '{new_name}'")
        return 0

    if not db.exists():
        status.db_missing(db)
        return 1

    # Local execution
    try:
        rename_tag_safe(db_path=db, old_name=old_name, new_name=new_name)
    except FileNotFoundError:
        status.error(f"Tag not found: {old_name}")
        return 1
    except ValueError as e:
        status.error(str(e))
        return 1

    status.confirm(f"Renamed '{old_name}' \u2192 '{new_name}'")
    return 0


def _cmd_tag_delete(args, db: Path) -> int:
    """Delete a tag: siftd tag delete <name> [--force]."""
    positional = args.positional or []
    # positional[0] is "delete", [1] is name
    if len(positional) < 2:
        print("Usage: siftd tag delete <name> [--force]")
        return 1

    tag_name = positional[1]

    # Try serve delegation (POST /api/v1/tag with action=delete)
    force = getattr(args, "force", False)

    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    op = Operation(
        path="/api/v1/tag",
        method="POST",
        fn=delete_tag_safe,
        params={
            "action": "delete",
            "tag_name": tag_name,
            "db_path": db,
        },
        render_method="raw",
        fidelity=Fidelity(),
        db=db,
    )

    # Only delegate when --force or 0 associations (we can't check
    # association counts over HTTP, so skip delegation without --force
    # to preserve the interactive confirmation guard).
    if force:
        try:
            result = try_serve(op)
        except ServeRequest4xx as e:
            print_serve_4xx(e)
            return 1
        if result is not None and isinstance(result, dict) and result.get("status") == "deleted":
            status.confirm(f"Deleted tag '{tag_name}'")
            return 0

    if not db.exists():
        status.db_missing(db)
        return 1

    conn = open_database(db)

    # Check associations first
    tags = list_tags(conn=conn)
    tag_info = next((t for t in tags if t.name == tag_name), None)
    if not tag_info:
        status.error(f"Tag not found: {tag_name}")
        conn.close()
        return 1

    total_associations = (
        tag_info.conversation_count
        + tag_info.workspace_count
        + tag_info.tool_call_count
        + tag_info.exchange_count
        + tag_info.prompt_count
        + tag_info.response_count
    )

    force = getattr(args, "force", False)
    if total_associations > 0 and not force:
        parts = []
        if tag_info.conversation_count:
            parts.append(f"{fmt_count(tag_info.conversation_count)} conversations")
        if tag_info.workspace_count:
            parts.append(f"{fmt_count(tag_info.workspace_count)} workspaces")
        if tag_info.tool_call_count:
            parts.append(f"{fmt_count(tag_info.tool_call_count)} tool_calls")
        if tag_info.exchange_count:
            parts.append(f"{fmt_count(tag_info.exchange_count)} exchanges")
        if tag_info.prompt_count:
            parts.append(f"{fmt_count(tag_info.prompt_count)} prompts")
        if tag_info.response_count:
            parts.append(f"{fmt_count(tag_info.response_count)} responses")
        status.error(
            f"Tag '{tag_name}' is applied to {', '.join(parts)}.",
            hint="Use --force to delete.",
        )
        conn.close()
        return 1

    conn.close()
    try:
        delete_tag_safe(db_path=db, tag_name=tag_name)
    except FileNotFoundError:
        status.error(f"Tag not found: {tag_name}")
        return 1
    except ValueError as e:
        status.error(str(e))
        return 1
    parts = []
    if tag_info.conversation_count:
        parts.append(f"{fmt_count(tag_info.conversation_count)} conversations")
    if tag_info.workspace_count:
        parts.append(f"{fmt_count(tag_info.workspace_count)} workspaces")
    if tag_info.tool_call_count:
        parts.append(f"{fmt_count(tag_info.tool_call_count)} tool_calls")
    if tag_info.exchange_count:
        parts.append(f"{fmt_count(tag_info.exchange_count)} exchanges")
    if tag_info.prompt_count:
        parts.append(f"{fmt_count(tag_info.prompt_count)} prompts")
    if tag_info.response_count:
        parts.append(f"{fmt_count(tag_info.response_count)} responses")
    if parts:
        status.confirm(f"Deleted tag '{tag_name}' (was applied to {', '.join(parts)})")
    else:
        status.confirm(f"Deleted tag '{tag_name}'")
    return 0


def cmd_tag(args) -> int:
    """Apply, remove, list, rename, or delete tags."""
    db = resolve_db(args)

    # Check for subcommands (apply, remove, list, rename, delete) via positional args.
    # Safe: these words can never be ULIDs (26-char base32).
    positional = args.positional or []
    if positional and positional[0] in _TAG_SUBCOMMANDS:
        subcmd = positional[0]
        if subcmd == "list":
            return _cmd_tag_list(args, db)
        if subcmd == "rename":
            return _cmd_tag_rename(args, db)
        if subcmd == "delete":
            return _cmd_tag_delete(args, db)
        # apply/remove: strip the subcommand and fall through to the legacy logic.
        # args.positional is updated so that _tag_session (which reads it directly)
        # sees the right tag names.
        args.positional = positional[1:]
        positional = args.positional
        if subcmd == "remove":
            args.remove = True

    # --- Original tag apply/remove logic below ---

    # Resolve --current: auto-detect session, fall back to --last
    session_id = getattr(args, "session", None)
    if getattr(args, "current", False) and not session_id:
        session_id = _detect_current_session()
        if session_id:
            status.info(f"Detected session: {session_id[:20]}...")
        else:
            # No active session — fall back to --last 1
            status.info("No active session detected, falling back to --last 1")
            if args.last is None:
                args.last = 1

    # Warn about silently ignored flag combinations
    exchange_index = getattr(args, "exchange", None)
    if exchange_index is not None and not session_id:
        status.info("--exchange ignored without --session")
    if args.last is not None and session_id:
        status.info("--last ignored with --session")
    last_marker_flags = [
        flag for flag in ("last_prompt", "last_response", "last_exchange", "last_tool_call")
        if getattr(args, flag, False)
    ]
    if last_marker_flags and not session_id:
        names = ", ".join("--" + f.replace("_", "-") for f in last_marker_flags)
        status.info(f"{names} ignored without --session/--current")

    # Handle --session mode (queue pending tags)
    if session_id:
        return _tag_session(args, db, session_id)

    if not db.exists():
        status.db_missing(db)
        return 1

    removing = args.remove

    # Colon-path syntax: <conv>:<kind>:<n> <tag> [tag2...]
    if positional and args.last is None:
        from siftd.api.granular_targets import GRANULAR_KINDS, parse_colon_path, resolve_colon_target

        _cp = parse_colon_path(positional[0])
        if _cp is not None:
            _conv_ref, _kind, _n = _cp
            _tag_names = positional[1:]
            if not _tag_names:
                print("Usage: siftd tag <conv>:<kind>:<n> <tag> [tag2 ...]")
                return 1
            if _kind not in GRANULAR_KINDS:
                status.error(f"Unknown target kind {_kind!r}. Valid: {', '.join(sorted(GRANULAR_KINDS))}")
                return 1
            _conn = open_database(db)
            try:
                from siftd.api.conversations import resolve_entity_id
                try:
                    _conv_id = resolve_entity_id(_conn, "conversation", _conv_ref)
                except AmbiguousPrefix as _exc:
                    print_ambiguous_error(_exc)
                    return 2
                if _conv_id is None:
                    status.error(f"conversation not found: {_conv_ref}")
                    return 1
                try:
                    _target_kind, _target_id = resolve_colon_target(_conn, _conv_id, _kind, _n)
                except (ValueError, IndexError) as e:
                    status.error(str(e))
                    return 1
            finally:
                _conn.close()
            try:
                _result = apply_tags(
                    db_path=db,
                    tags=_tag_names,
                    entity_type=_target_kind,
                    entity_id=_target_id,
                    remove=removing,
                )
            except FileNotFoundError:
                status.error(f"{_target_kind} not found: {short_id(_target_id)}")
                return 1
            except ValueError as e:
                status.error(str(e))
                return 1
            _resolved_id = _result.resolved_entity_id or _target_id
            if removing:
                for _row in _result.results:
                    if _row.status == "not_found":
                        status.error(f"Tag '{_row.tag}' not found")
                    elif _row.status == "removed":
                        status.confirm(f"Removed tag '{_row.tag}' from {_target_kind} {short_id(_resolved_id)}")
                    else:
                        status.info(f"Tag '{_row.tag}' not applied to {_target_kind} {short_id(_resolved_id)}")
            else:
                for _row in _result.results:
                    if _row.status == "applied":
                        status.confirm(f"Applied tag '{_row.tag}' to {_target_kind} {short_id(_resolved_id)}")
                    else:
                        status.info(f"Tag '{_row.tag}' already applied to {_target_kind} {short_id(_resolved_id)}")
            return 0

    # Normalize args into a POST body for delegation
    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    body: dict = {"action": "remove" if removing else "apply"}

    if args.last is not None:
        n = args.last
        if isinstance(n, str):
            try:
                n = int(n)
            except ValueError:
                positional = [str(n)] + positional
                n = 1
        body["last"] = n
        body["tags"] = [str(t) for t in positional]
    elif positional:
        parsed = _parse_tag_args(positional)
        if parsed:
            entity_type, entity_id, tag_names_parsed = parsed
            body["entity_type"] = entity_type
            body["entity_id"] = entity_id
            body["tags"] = tag_names_parsed

    if "tags" in body:
        op = Operation(
            path="/api/v1/tag",
            method="POST",
            fn=apply_tags,
            params={**body, "db_path": db},
            render_method="raw",
            fidelity=Fidelity(),
            db=db,
        )
        try:
            result = try_serve(op)
        except ServeRequest4xx as e:
            print_serve_4xx(e)
            return 1
        if result is not None and isinstance(result, dict) and "error" not in result:
            for r in result.get("results", []):
                tag = r["tag"]
                row_status = r["status"]
                count = r.get("count", 0)
                if row_status == "not_found":
                    status.error(f"Tag '{tag}' not found")
                elif row_status == "applied":
                    status.confirm(f"Applied tag '{tag}' to {count} conversation(s)")
                elif row_status == "removed":
                    status.confirm(f"Removed tag '{tag}' from {count} conversation(s)")
            return 0

    # Handle --last mode
    if args.last is not None:
        # Resolve N: const=1 gives int, but nargs="?" may capture a string.
        # If the captured value isn't numeric, treat it as a tag name and default N=1.
        n = args.last
        if isinstance(n, str):
            try:
                n = int(n)
            except ValueError:
                # e.g. `--last brainstorming:x review` → n grabbed "brainstorming:x"
                positional = [n] + positional
                n = 1

        if not positional:
            print("Usage: siftd tag --last [N] <tag> [tag2 ...]")
            return 1

        tag_names: list[str] = [str(t) for t in positional]
        if n < 1:
            status.error("--last requires a positive number")
            return 1

        try:
            result_local = apply_tags(
                db_path=db,
                tags=tag_names,
                last=n,
                remove=removing,
            )
        except FileNotFoundError:
            status.error("No conversations found.")
            return 1
        except ValueError as e:
            status.error(str(e))
            return 1

        errors = 0
        if removing:
            for row in result_local.results:
                tag_name = row.tag
                row_status = row.status
                count = row.count
                if row_status == "not_found":
                    status.error(f"Tag '{tag_name}' not found")
                    errors += 1
                elif row_status == "removed":
                    status.confirm(f"Removed tag '{tag_name}' from {count} conversation(s)")
                else:
                    status.info(f"Tag '{tag_name}' not applied to any of {result_local.target_count} conversation(s)")
        else:
            for row in result_local.results:
                tag_name = row.tag
                row_status = row.status
                count = row.count
                if row_status == "applied":
                    status.confirm(f"Applied tag '{tag_name}' to {count} conversation(s)")
                else:
                    status.info(f"Tag '{tag_name}' already applied to all {result_local.target_count} conversation(s)")

        return 1 if errors == len(tag_names) else 0

    # Parse positional args
    parsed = _parse_tag_args(positional)
    if not parsed:
        print("Usage: siftd tag <id> <tag> [tag2 ...]")
        print("       siftd tag apply <id> <tag> [tag2 ...]")
        print("       siftd tag remove <id> <tag> [tag2 ...]")
        print("       siftd tag <entity_type> <id> <tag> [tag2 ...]")
        print("       siftd tag <conv>:<kind>:<n> <tag> [tag2 ...]")
        print("       siftd tag --last [N] <tag> [tag2 ...]")
        print("       siftd tag --session <id> <tag> [tag2 ...]")
        print("       siftd tag --remove <id> <tag> [tag2 ...]")
        print("       siftd tag list [--prefix PREFIX] [name]")
        print("       siftd tag rename <old> <new>")
        print("       siftd tag delete <name> [--force]")
        print("\nTip: Use --session to queue tags for live sessions before ingest.", file=sys.stderr)
        print("\nEntity types: conversation (default), workspace, tool_call, prompt, response, exchange")
        return 1

    entity_type, entity_id, tag_names = parsed

    try:
        result_local = apply_tags(
            db_path=db,
            tags=tag_names,
            entity_type=entity_type,
            entity_id=entity_id,
            remove=removing,
        )
    except AmbiguousPrefix as exc:
        print_ambiguous_error(exc)
        return 2
    except FileNotFoundError:
        status.error(f"{entity_type} not found: {entity_id}")
        return 1
    except ValueError as e:
        status.error(str(e))
        return 1

    resolved_id = result_local.resolved_entity_id or entity_id

    if removing:
        for row in result_local.results:
            tag_name = row.tag
            row_status = row.status
            if row_status == "not_found":
                status.error(f"Tag '{tag_name}' not found")
            elif row_status == "removed":
                status.confirm(f"Removed tag '{tag_name}' from {entity_type} {short_id(resolved_id)}")
            else:
                status.info(f"Tag '{tag_name}' not applied to {entity_type} {short_id(resolved_id)}")
    else:
        for row in result_local.results:
            tag_name = row.tag
            row_status = row.status
            if row_status == "applied":
                status.confirm(f"Applied tag '{tag_name}' to {entity_type} {short_id(resolved_id)}")
            else:
                status.info(f"Tag '{tag_name}' already applied to {entity_type} {short_id(resolved_id)}")
    return 0


def build_tags_parser(subparsers) -> None:
    """Add 'tag' subparser."""
    # tag — unified entry point
    p_tag = subparsers.add_parser(
        "tag",
        help="Manage tags: apply, remove, list, rename, delete",
        description="Apply, remove, list, rename, or delete tags.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd tag 01HX... important              # tag conversation (default)
  siftd tag 01HX... important review       # apply multiple tags at once
  siftd tag --last important               # tag most recent conversation
  siftd tag --last important review        # multiple tags on most recent
  siftd tag --last 3 review                # tag 3 most recent conversations
  siftd tag workspace 01HY... proj         # explicit entity type
  siftd tag tool_call 01HZ... slow         # tag a tool call
  siftd tag --remove 01HX... important     # remove tag from conversation
  siftd tag --remove --last 1 important    # remove from most recent
  siftd tag -r workspace 01HY... proj      # remove from workspace

subcommands:
  siftd tag apply 01HX... important         # explicit apply (same as positional)
  siftd tag apply --last important          # apply to most recent via subcommand
  siftd tag remove 01HX... important        # explicit remove (same as --remove)
  siftd tag remove --last important         # remove from most recent via subcommand
  siftd tag list                            # list all tags
  siftd tag list --prefix research:         # filter by prefix
  siftd tag list research:auth              # show conversations with tag
  siftd tag rename old-name new-name        # rename a tag
  siftd tag delete old-tag                  # delete (refuses if applied)
  siftd tag delete old-tag --force          # delete with associations

live session tagging:
  siftd tag --current decision:auth              # auto-detect session, queue tag
  siftd tag --session abc123 decision:auth       # queue tag for session
  siftd tag --session abc123 --exchange 5 key    # queue tag for exchange 5""",
    )
    p_tag.add_argument(
        "positional",
        nargs="*",
        help="[entity_type] entity_id tag [tag2 ...] | apply | remove | list | rename | delete",
    )
    p_tag.add_argument(
        "-n",
        "--last",
        "--latest",
        nargs="?",
        const=1,
        default=None,
        metavar="N",
        help="Tag N most recent conversations (default: 1 if flag used without N)",
    )
    p_tag.add_argument("-r", "--remove", action="store_true", help="Remove tag instead of applying")
    p_tag.add_argument("--session", metavar="ID", help="Queue tag for a live session (applied at ingest)")
    p_tag.add_argument("--current", action="store_true", help="Auto-detect current session (falls back to --last)")
    target_group = p_tag.add_mutually_exclusive_group()
    target_group.add_argument(
        "--exchange", type=int, metavar="INDEX",
        help="Tag specific exchange (1-based, requires --session)",
    )
    target_group.add_argument(
        "--last-prompt", action="store_true", dest="last_prompt",
        help="Tag the last prompt of the session (requires --session/--current)",
    )
    target_group.add_argument(
        "--last-response", action="store_true", dest="last_response",
        help="Tag the last response of the session (requires --session/--current)",
    )
    target_group.add_argument(
        "--last-exchange", action="store_true", dest="last_exchange",
        help="Tag the last exchange of the session (requires --session/--current)",
    )
    target_group.add_argument(
        "--last-tool-call", action="store_true", dest="last_tool_call",
        help="Tag the last tool_call of the session (requires --session/--current)",
    )

    # Flags for subcommands (list, delete)
    p_tag.add_argument("--prefix", metavar="PREFIX", help="Filter tag list by prefix (use with 'tag list')")
    p_tag.add_argument("--limit", type=int, default=None, help="Result cap (drill-down: default 10, --by-workspace: default 20 workspaces)")
    p_tag.add_argument("--force", action="store_true", help="Force delete even if tag has associations (use with 'tag delete')")
    p_tag.add_argument(
        "--by-workspace", action="store_true", dest="by_workspace",
        help=(
            "Group tag counts by workspace (use with 'tag list'). "
            "Counts only event-backed tags (tool_call, prompt, response, exchange); "
            "conversation-level tags are excluded. Composes with --on, --prefix, "
            "-w, --owner, --all-tags, --limit only."
        ),
    )
    p_tag.add_argument("--json", action="store_true", help="Output as JSON (use with 'tag list --by-workspace')")

    from siftd.cli._filters import add_filter_args

    add_filter_args(p_tag, include_model=True)

    p_tag.set_defaults(func=cmd_tag)
