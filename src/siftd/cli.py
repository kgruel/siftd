"""CLI for siftd - conversation log aggregator."""

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from siftd.adapters.registry import load_all_adapters, wrap_adapter_paths
from siftd.api import (
    apply_tag,
    create_database,
    delete_tag,
    get_or_create_tag,
    list_tags,
    open_database,
    remove_tag,
    rename_tag,
)
from siftd.api.sessions import (
    is_session_registered,
    register_session,
)
from siftd.api.sessions import (
    queue_tag as queue_pending_tag,
)
from siftd.backfill import (
    backfill_derivative_tags,
    backfill_filter_binary,
    backfill_response_attributes,
    backfill_shell_tags,
)
from siftd.cli_install import build_install_parser
from siftd.cli_search import build_search_parser
from siftd.ingestion import IngestStats, ingest_all
from siftd.paths import data_dir, db_path, ensure_dirs, queries_dir, session_id_file
from siftd.storage.fts import rebuild_fts_index


def parse_date(value: str | None) -> str | None:
    """Parse date string to ISO format (YYYY-MM-DD).

    Supports:
    - ISO format: 2024-01-01 (passthrough)
    - Relative days: 7d, 3d (subtract N days from today)
    - Relative weeks: 1w, 2w (subtract N weeks from today)
    - Keywords: yesterday, today

    Raises argparse.ArgumentTypeError for unrecognized formats,
    so this can be used as type= on argparse arguments.
    """
    if not value:
        return None

    value = value.strip().lower()

    # Keywords
    if value == "today":
        return date.today().isoformat()
    if value == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()

    # Relative days: 7d, 3d
    if match := re.fullmatch(r"(\d+)d", value):
        days = int(match.group(1))
        return (date.today() - timedelta(days=days)).isoformat()

    # Relative weeks: 1w, 2w
    if match := re.fullmatch(r"(\d+)w", value):
        weeks = int(match.group(1))
        return (date.today() - timedelta(weeks=weeks)).isoformat()

    # ISO format passthrough (validate format)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value

    raise argparse.ArgumentTypeError(
        f"invalid date format: '{value}' (expected YYYY-MM-DD, Nd, Nw, today, or yesterday)"
    )


def cmd_ingest(args) -> int:
    """Run ingestion from all adapters."""
    ensure_dirs()

    db = Path(args.db) if args.db else db_path()
    is_new = not db.exists()

    if is_new:
        print(f"Creating database: {db}")
    else:
        print(f"Using database: {db}")

    conn = create_database(db)

    # Handle --rebuild-fts flag
    if args.rebuild_fts:
        print("Rebuilding FTS index...")
        rebuild_fts_index(conn)
        print("FTS index rebuilt.")
        conn.close()
        return 0

    def on_file(source, status):
        if args.verbose or status not in ("skipped", "skipped (older)"):
            name = Path(source.location).name
            print(f"  [{status}] {name}")

    plugins = load_all_adapters()
    if args.adapter:
        names = set(args.adapter)
        plugins = [p for p in plugins if p.name in names]
        if not plugins:
            print(f"No adapters matched: {', '.join(args.adapter)}")
            return 1

    # Extract modules for ingestion (wrap with path overrides if needed)
    if args.path:
        adapters = [wrap_adapter_paths(p.module, args.path) for p in plugins]
        print(f"Scanning: {', '.join(args.path)}")
    else:
        adapters = [p.module for p in plugins]

    print("\nIngesting...")
    stats = ingest_all(conn, adapters, on_file=on_file)

    _print_stats(stats)
    conn.close()
    return 0


def cmd_status(args) -> int:
    """Show database status and statistics."""
    from siftd.api import get_stats

    db = Path(args.db) if args.db else None

    try:
        stats = get_stats(db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'siftd ingest' to create it.")
        return 1

    # JSON output
    if args.json:
        import json

        from siftd.embeddings import embeddings_available

        out = {
            "db_path": str(stats.db_path),
            "db_size_bytes": stats.db_size_bytes,
            "counts": {
                "conversations": stats.counts.conversations,
                "prompts": stats.counts.prompts,
                "responses": stats.counts.responses,
                "tool_calls": stats.counts.tool_calls,
                "harnesses": stats.counts.harnesses,
                "workspaces": stats.counts.workspaces,
                "tools": stats.counts.tools,
                "models": stats.counts.models,
                "ingested_files": stats.counts.ingested_files,
            },
            "harnesses": [
                {"name": h.name, "source": h.source, "log_format": h.log_format}
                for h in stats.harnesses
            ],
            "top_workspaces": [
                {"path": w.path, "conversation_count": w.conversation_count}
                for w in stats.top_workspaces
            ],
            "models": stats.models,
            "top_tools": [
                {"name": t.name, "usage_count": t.usage_count}
                for t in stats.top_tools
            ],
            "features": {
                "embeddings": embeddings_available(),
            },
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"Database: {stats.db_path}")
    print(f"Size: {stats.db_size_bytes / 1024:.1f} KB")

    print("\n--- Counts ---")
    print(f"  Conversations: {stats.counts.conversations}")
    print(f"  Prompts: {stats.counts.prompts}")
    print(f"  Responses: {stats.counts.responses}")
    print(f"  Tool calls: {stats.counts.tool_calls}")
    print(f"  Harnesses: {stats.counts.harnesses}")
    print(f"  Workspaces: {stats.counts.workspaces}")
    print(f"  Tools: {stats.counts.tools}")
    print(f"  Models: {stats.counts.models}")
    print(f"  Ingested files: {stats.counts.ingested_files}")

    print("\n--- Harnesses ---")
    for h in stats.harnesses:
        print(f"  {h.name} ({h.source}, {h.log_format})")

    print("\n--- Workspaces (top 10) ---")
    for w in stats.top_workspaces:
        print(f"  {w.path}: {w.conversation_count} conversations")

    print("\n--- Models ---")
    for model in stats.models:
        print(f"  {model}")

    print("\n--- Tools (top 10 by usage) ---")
    for t in stats.top_tools:
        print(f"  {t.name}: {t.usage_count}")

    # Features status
    from siftd.embeddings import embeddings_available

    print("\n--- Features ---")
    if embeddings_available():
        print("  Embeddings: installed")
    else:
        print("  Embeddings: not installed (run: siftd install embed)")

    return 0


def cmd_workspaces(args) -> int:
    """List workspaces with conversation counts."""
    import json as json_mod

    from siftd.storage.queries import fetch_top_workspaces

    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        if args.json:
            print("[]")
            return 0
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db, read_only=True)
    limit = args.limit if args.limit > 0 else 10000
    rows = fetch_top_workspaces(conn, limit=limit)
    conn.close()

    if args.json:
        out = [
            {"path": row["path"], "conversations": row["convs"]}
            for row in rows
        ]
        print(json_mod.dumps(out, indent=2))
        return 0

    if not rows:
        print("No workspaces found.")
        return 0

    for row in rows:
        print(f"{row['path']}  ({row['convs']} conversations)")

    return 0


def cmd_path(args) -> int:
    """Show XDG paths."""
    from siftd.paths import cache_dir, config_dir, db_path

    print(f"Data directory:   {data_dir()}")
    print(f"Config directory: {config_dir()}")
    print(f"Cache directory:  {cache_dir()}")
    print(f"Database:         {db_path()}")
    return 0


def _parse_tag_args(positional: list[str]) -> tuple[str, str, list[str]] | None:
    """Parse positional args for tag command.

    Returns (entity_type, entity_id, tag_names) or None if invalid.
    Supports:
      - <id> <tag> [tag2 ...]                    -> conversation, id, [tags]
      - <entity_type> <id> <tag> [tag2 ...]      -> entity_type, id, [tags]
    """
    if len(positional) >= 2:
        # Check if first arg is an entity type
        if positional[0] in ("conversation", "workspace", "tool_call"):
            if len(positional) < 3:
                return None
            return (positional[0], positional[1], positional[2:])
        # Default: conversation
        return ("conversation", positional[0], positional[1:])
    return None


def cmd_register(args) -> int:
    """Register an active session for live tagging."""
    import os

    db = Path(args.db) if args.db else db_path()
    ensure_dirs()

    # Create database if it doesn't exist
    conn = create_database(db)

    session_id = args.session
    adapter_name = args.adapter
    workspace_path = args.workspace or os.getcwd()

    # Resolve workspace to absolute path
    workspace_path = str(Path(workspace_path).resolve())

    # Register the session
    register_session(conn, session_id, adapter_name, workspace_path, commit=True)

    # Write session ID to XDG state dir
    sid_file = session_id_file(workspace_path)
    sid_file.parent.mkdir(parents=True, exist_ok=True)
    sid_file.write_text(session_id)

    conn.close()
    print(f"Registered session {session_id[:8]}... for {adapter_name}")
    return 0


def cmd_session_id(args) -> int:
    """Print the session ID for the current workspace."""
    import os

    workspace_path = args.workspace or os.getcwd()
    workspace_path = str(Path(workspace_path).resolve())

    sid_file = session_id_file(workspace_path)
    if sid_file.exists():
        session_id = sid_file.read_text().strip()
        if session_id:
            print(session_id)
            return 0

    # Fallback: query active_sessions table
    db = Path(args.db) if args.db else db_path()
    if db.exists():
        from siftd.storage.sessions import find_active_session

        conn = open_database(db, read_only=True)
        try:
            session_id = find_active_session(conn, workspace_path)
            if session_id:
                print(session_id)
                return 0
        finally:
            conn.close()

    # Exit silently with non-zero for scripting
    return 1


def _tag_session(args, db: Path, session_id: str) -> int:
    """Queue pending tags for a session (--session mode)."""
    ensure_dirs()

    # Create database if it doesn't exist
    conn = create_database(db)

    # Check if --remove was specified (not supported for --session)
    if args.remove:
        print("Error: --remove not supported with --session")
        print("Use 'siftd doctor fix --pending-tags' to clear pending tags")
        conn.close()
        return 1

    # Parse tag names from positional args
    tag_names = args.positional or []
    if not tag_names:
        print("Usage: siftd tag --session <id> <tag> [tag2 ...]")
        print("       siftd tag --session <id> --exchange <index> <tag> [tag2 ...]")
        conn.close()
        return 1

    # Check if session is registered (warn but proceed)
    if not is_session_registered(conn, session_id):
        print(f"Warning: Session {session_id[:8]}... not registered", file=sys.stderr)

    # Determine entity type and exchange index
    exchange_index = getattr(args, "exchange", None)
    entity_type = "exchange" if exchange_index is not None else "conversation"

    # Queue each tag
    queued = 0
    for tag_name in tag_names:
        result = queue_pending_tag(
            conn,
            session_id,
            tag_name,
            entity_type=entity_type,
            exchange_index=exchange_index,
            commit=False,
        )
        if result:
            queued += 1
            if exchange_index is not None:
                print(f"Queued tag '{tag_name}' for exchange {exchange_index}")
            else:
                print(f"Queued tag '{tag_name}' for session {session_id[:8]}...")
        else:
            print(f"Tag '{tag_name}' already queued")

    conn.commit()
    conn.close()
    return 0


def cmd_tag(args) -> int:
    """Apply or remove a tag on a conversation, workspace, or tool_call."""
    db = Path(args.db) if args.db else db_path()

    # Warn about silently ignored flag combinations
    session_id = getattr(args, "session", None)
    exchange_index = getattr(args, "exchange", None)
    if exchange_index is not None and not session_id:
        print("Note: --exchange ignored without --session", file=sys.stderr)
    if args.last is not None and session_id:
        print("Note: --last ignored with --session", file=sys.stderr)

    # Handle --session mode (queue pending tags)
    if session_id:
        return _tag_session(args, db, session_id)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)
    removing = args.remove

    # Handle --last mode
    if args.last is not None:
        if not args.positional or len(args.positional) != 1:
            print("Usage: siftd tag --last N <tag>")
            conn.close()
            return 1

        tag_name = args.positional[0]
        n = args.last
        if n < 1:
            print("--last requires a positive number")
            conn.close()
            return 1

        # Get N most recent conversations
        rows = conn.execute(
            "SELECT id FROM conversations ORDER BY started_at DESC LIMIT ?",
            (n,),
        ).fetchall()

        if not rows:
            print("No conversations found.")
            conn.close()
            return 1

        if removing:
            # Look up existing tag (don't create on remove)
            tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
            if not tag_row:
                print(f"Tag '{tag_name}' not found")
                conn.close()
                return 1
            tag_id = tag_row["id"]

            removed = 0
            for row in rows:
                if remove_tag(conn, "conversation", row["id"], tag_id, commit=False):
                    removed += 1
            conn.commit()

            if removed:
                print(f"Removed tag '{tag_name}' from {removed} conversation(s)")
            else:
                print(f"Tag '{tag_name}' not applied to any of {len(rows)} conversation(s)")
        else:
            tag_id = get_or_create_tag(conn, tag_name)
            tagged = 0
            for row in rows:
                if apply_tag(conn, "conversation", row["id"], tag_id, commit=False):
                    tagged += 1
            conn.commit()

            if tagged:
                print(f"Applied tag '{tag_name}' to {tagged} conversation(s)")
            else:
                print(f"Tag '{tag_name}' already applied to all {len(rows)} conversation(s)")

        conn.close()
        return 0

    # Parse positional args
    parsed = _parse_tag_args(args.positional or [])
    if not parsed:
        print("Usage: siftd tag <id> <tag> [tag2 ...]")
        print("       siftd tag <entity_type> <id> <tag> [tag2 ...]")
        print("       siftd tag --last <tag>")
        print("       siftd tag --remove <id> <tag> [tag2 ...]")
        print("\nEntity types: conversation (default), workspace, tool_call")
        conn.close()
        return 1

    entity_type, entity_id, tag_names = parsed

    # Validate entity exists (support prefix match for conversations)
    if entity_type == "conversation":
        row = conn.execute(
            "SELECT id FROM conversations WHERE id = ? OR id LIKE ?",
            (entity_id, f"{entity_id}%"),
        ).fetchone()
    elif entity_type == "workspace":
        row = conn.execute("SELECT id FROM workspaces WHERE id = ?", (entity_id,)).fetchone()
    elif entity_type == "tool_call":
        row = conn.execute("SELECT id FROM tool_calls WHERE id = ?", (entity_id,)).fetchone()
    else:
        print(f"Unsupported entity type: {entity_type}")
        print("Supported: conversation, workspace, tool_call")
        conn.close()
        return 1

    if not row:
        print(f"{entity_type} not found: {entity_id}")
        conn.close()
        return 1

    # Use resolved ID (for prefix match)
    resolved_id = row["id"]

    if removing:
        removed = 0
        for tag_name in tag_names:
            tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
            if not tag_row:
                print(f"Tag '{tag_name}' not found")
                continue
            if remove_tag(conn, entity_type, resolved_id, tag_row["id"], commit=False):
                print(f"Removed tag '{tag_name}' from {entity_type} {resolved_id[:12]}")
                removed += 1
            else:
                print(f"Tag '{tag_name}' not applied to {entity_type} {resolved_id[:12]}")
        conn.commit()
    else:
        applied = 0
        for tag_name in tag_names:
            tag_id = get_or_create_tag(conn, tag_name)
            if apply_tag(conn, entity_type, resolved_id, tag_id, commit=False):
                print(f"Applied tag '{tag_name}' to {entity_type} {resolved_id[:12]}")
                applied += 1
            else:
                print(f"Tag '{tag_name}' already applied to {entity_type} {resolved_id[:12]}")
        conn.commit()

    conn.close()
    return 0


def cmd_tags(args) -> int:
    """List, rename, or delete tags."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)

    # Handle --rename OLD NEW
    if args.rename:
        old_name, new_name = args.rename
        try:
            if rename_tag(conn, old_name, new_name, commit=True):
                print(f"Renamed '{old_name}' \u2192 '{new_name}'")
            else:
                print(f"Tag not found: {old_name}")
                conn.close()
                return 1
        except ValueError as e:
            print(f"Error: {e}")
            conn.close()
            return 1
        conn.close()
        return 0

    # Handle --delete NAME
    if args.delete:
        tag_name = args.delete

        # Check associations first
        tags = list_tags(conn=conn)
        tag_info = next((t for t in tags if t.name == tag_name), None)
        if not tag_info:
            print(f"Tag not found: {tag_name}")
            conn.close()
            return 1

        total_associations = (
            tag_info.conversation_count
            + tag_info.workspace_count
            + tag_info.tool_call_count
            + tag_info.prompt_count
        )

        if total_associations > 0 and not args.force:
            parts = []
            if tag_info.conversation_count:
                parts.append(f"{tag_info.conversation_count} conversations")
            if tag_info.workspace_count:
                parts.append(f"{tag_info.workspace_count} workspaces")
            if tag_info.tool_call_count:
                parts.append(f"{tag_info.tool_call_count} tool_calls")
            if tag_info.prompt_count:
                parts.append(f"{tag_info.prompt_count} prompts")
            print(f"Tag '{tag_name}' is applied to {', '.join(parts)}. Use --force to delete.")
            conn.close()
            return 1

        delete_tag(conn, tag_name, commit=True)
        parts = []
        if tag_info.conversation_count:
            parts.append(f"{tag_info.conversation_count} conversations")
        if tag_info.workspace_count:
            parts.append(f"{tag_info.workspace_count} workspaces")
        if tag_info.tool_call_count:
            parts.append(f"{tag_info.tool_call_count} tool_calls")
        if tag_info.prompt_count:
            parts.append(f"{tag_info.prompt_count} prompts")
        if parts:
            print(f"Deleted tag '{tag_name}' (was applied to {', '.join(parts)})")
        else:
            print(f"Deleted tag '{tag_name}'")
        conn.close()
        return 0

    # Drill-down: show conversations with a given tag
    if getattr(args, "name", None):
        from siftd.api import list_conversations

        tag_name = args.name
        conn.close()

        try:
            conversations = list_conversations(db_path=db, tags=[tag_name], limit=args.limit)
        except FileNotFoundError as e:
            print(str(e))
            return 1

        if not conversations:
            print(f"No conversations found for tag: {tag_name}")
            return 0

        from siftd.output import fmt_timestamp, fmt_tokens, fmt_workspace

        print(f"Conversations tagged '{tag_name}' (showing {len(conversations)}):")
        for c in conversations:
            cid = c.id[:12] if c.id else ""
            ws = fmt_workspace(c.workspace_path)
            model = c.model or ""
            started = fmt_timestamp(c.started_at)
            tokens = fmt_tokens(c.total_tokens)
            tag_str = f"  [{', '.join(c.tags)}]" if c.tags else ""
            print(f"{cid}  {started}  {ws}  {model}  {c.prompt_count}p/{c.response_count}r  {tokens} tok{tag_str}")

        if args.limit > 0 and len(conversations) >= args.limit:
            print(f"\nTip: show more with `siftd query -l {tag_name} -n 0`", file=sys.stderr)
        return 0

    # Default: list tags
    tags = list_tags(conn=conn)

    if not tags:
        print("No tags defined.")
        conn.close()
        return 0

    prefix = getattr(args, "prefix", None)
    if prefix:
        tags = [t for t in tags if t.name.startswith(prefix)]
        if not tags:
            print(f"No tags found with prefix: {prefix}")
            conn.close()
            return 0

    for tag in tags:
        counts = []
        if tag.conversation_count:
            counts.append(f"{tag.conversation_count} conversations")
        if tag.workspace_count:
            counts.append(f"{tag.workspace_count} workspaces")
        if tag.tool_call_count:
            counts.append(f"{tag.tool_call_count} tool_calls")
        count_str = f" ({', '.join(counts)})" if counts else ""
        desc = f" - {tag.description}" if tag.description else ""
        print(f"  {tag.name}{desc}{count_str}")

    conn.close()
    return 0


def cmd_tools(args) -> int:
    """Show tool usage summary by category."""
    from siftd.api import get_tool_tag_summary, get_tool_tags_by_workspace

    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        if args.json:
            print("[]")
            return 0
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    prefix = args.prefix or "shell:"

    # By-workspace mode
    if args.by_workspace:
        try:
            results = get_tool_tags_by_workspace(
                db_path=db,
                prefix=prefix,
                limit=args.limit,
            )
        except FileNotFoundError as e:
            if args.json:
                print("[]")
                return 0
            print(str(e))
            return 1

        if not results:
            if args.json:
                print("[]")
                return 0
            print(f"No tool calls with '{prefix}*' tags found.")
            return 0

        # JSON output for by-workspace mode
        if args.json:
            import json

            out = [
                {
                    "workspace": ws_usage.workspace,
                    "total": ws_usage.total,
                    "tags": [
                        {"name": tag.name, "count": tag.count}
                        for tag in ws_usage.tags
                    ],
                }
                for ws_usage in results
            ]
            print(json.dumps(out, indent=2))
            return 0

        for ws_usage in results:
            ws_display = Path(ws_usage.workspace).name if ws_usage.workspace != "(no workspace)" else ws_usage.workspace
            print(f"\n{ws_display} ({ws_usage.total} total)")
            for tag in ws_usage.tags:
                # Strip prefix for display
                category = tag.name[len(prefix):] if tag.name.startswith(prefix) else tag.name
                print(f"  {category}: {tag.count}")

        return 0

    # Default: summary mode
    try:
        tags = get_tool_tag_summary(db_path=db, prefix=prefix)
    except FileNotFoundError as e:
        if args.json:
            print("[]")
            return 0
        print(str(e))
        return 1

    if not tags:
        if args.json:
            print("[]")
            return 0
        print(f"No tool calls with '{prefix}*' tags found.")
        print("Run 'siftd backfill --shell-tags' to categorize shell commands.")
        return 0

    # JSON output for summary mode
    if args.json:
        import json

        total = sum(t.count for t in tags)
        out = [
            {
                "name": tag.name,
                "count": tag.count,
                "percentage": round((tag.count / total) * 100, 1) if total > 0 else 0,
            }
            for tag in tags
        ]
        print(json.dumps(out, indent=2))
        return 0

    total = sum(t.count for t in tags)
    print(f"Tool call tags ({prefix}*): {total} total\n")

    for tag in tags:
        # Strip prefix for display
        category = tag.name[len(prefix):] if tag.name.startswith(prefix) else tag.name
        pct = (tag.count / total) * 100 if total > 0 else 0
        print(f"  {category}: {tag.count} ({pct:.1f}%)")

    return 0


def _query_detail(args) -> int:
    """Show conversation detail timeline."""
    from siftd.api import get_conversation
    from siftd.output import fmt_timestamp, fmt_tokens, fmt_workspace, truncate_text

    # Validate --exchanges
    exchanges_n = getattr(args, "exchanges", None)
    if exchanges_n is not None and exchanges_n < 1:
        print("Error: --exchanges must be at least 1")
        return 1

    db = Path(args.db) if args.db else None

    try:
        detail = get_conversation(args.conversation_id, db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'siftd ingest' to create it.")
        return 1

    if not detail:
        print(f"Conversation not found: {args.conversation_id}")
        return 1

    # Determine truncation limit
    chars_limit = 200  # default
    if getattr(args, "brief", False):
        chars_limit = 80
    elif getattr(args, "full", False):
        chars_limit = 0  # no truncation
    elif getattr(args, "chars", None) is not None:
        chars_limit = args.chars

    # Header
    ws_name = fmt_workspace(detail.workspace_path)
    started = fmt_timestamp(detail.started_at)
    total_tokens = detail.total_input_tokens + detail.total_output_tokens

    print(f"Conversation: {detail.id}")
    if ws_name:
        print(f"Workspace: {ws_name}")
    print(f"Started: {started}")
    print(f"Model: {detail.model or 'unknown'}")
    print(f"Tokens: {fmt_tokens(total_tokens)} (input: {fmt_tokens(detail.total_input_tokens)} / output: {fmt_tokens(detail.total_output_tokens)})")
    if detail.tags:
        print(f"Tags: {', '.join(detail.tags)}")

    # Summary mode: just metadata, no exchanges
    if getattr(args, "summary", False):
        print(f"Exchanges: {len(detail.exchanges)}")
        return 0

    print()

    # Determine which exchanges to show
    exchanges = detail.exchanges
    if exchanges_n is not None:
        # Show last N exchanges
        exchanges = exchanges[-exchanges_n:] if exchanges_n < len(exchanges) else exchanges

    # Timeline
    for ex in exchanges:
        ts = fmt_timestamp(ex.timestamp, time_only=True)

        # Prompt
        if ex.prompt_text:
            text = truncate_text(ex.prompt_text, chars_limit)
            print(f"[prompt] {ts}")
            print(f"  {text}")
            print()

        # Response
        if ex.response_text is not None or ex.tool_calls:
            print(f"[response] {ts} ({fmt_tokens(ex.input_tokens)} in / {fmt_tokens(ex.output_tokens)} out)")
            if ex.response_text:
                text = truncate_text(ex.response_text, chars_limit)
                print(f"  {text}")
            for tc in ex.tool_calls:
                if tc.count > 1:
                    print(f"  \u2192 {tc.tool_name} \u00d7{tc.count} ({tc.status})")
                else:
                    print(f"  \u2192 {tc.tool_name} ({tc.status})")
            print()

    return 0


def _query_sql(args) -> int:
    """List or run .sql query files (formerly 'queries' command)."""
    from siftd.api import QueryError, list_query_files, run_query_file

    # List mode: no name provided
    if not args.sql_name:
        query_files = list_query_files()
        if not query_files:
            print(f"No queries found in {queries_dir()}")
            return 0
        for qf in query_files:
            suffix = f"  (vars: {', '.join(qf.variables)})" if qf.variables else "  (no vars)"
            print(f"{qf.name}{suffix}")
        return 0

    # Run mode: parse variables
    variables = None
    if args.var:
        variables = {}
        for v in args.var:
            if "=" not in v:
                print(f"Invalid --var format (expected key=value): {v}")
                return 1
            key, value = v.split("=", 1)
            variables[key] = value

    db = Path(args.db) if args.db else None

    try:
        result = run_query_file(args.sql_name, variables, db_path=db)
    except FileNotFoundError as e:
        if "Query file not found" in str(e):
            print(f"Query not found: {e}")
            print("Available queries:")
            for qf in list_query_files():
                print(f"  {qf.name}")
            return 1
        print(str(e))
        print("Run 'siftd ingest' to create it.")
        return 1
    except QueryError as e:
        if "Missing variables" in str(e):
            # Extract missing vars for usage hint
            import re
            match = re.search(r"Missing variables: (.+)", str(e))
            missing = match.group(1).split(", ") if match else []
            print(f"Query '{args.sql_name}' requires variables not provided: {', '.join(missing)}")
            print(f"Usage: siftd query sql {args.sql_name} " + " ".join(f"--var {v}=<value>" for v in missing))
        else:
            print(str(e))
        return 1

    # Format output
    if result.rows:
        columns = result.columns
        widths = [len(c) for c in columns]
        str_rows = []
        for row in result.rows:
            str_row = [str(v) if v is not None else "" for v in row]
            str_rows.append(str_row)
            for i, val in enumerate(str_row):
                widths[i] = max(widths[i], len(val))
        header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
        print(header)
        print("  ".join("-" * w for w in widths))
        for str_row in str_rows:
            print("  ".join(val.ljust(widths[i]) for i, val in enumerate(str_row)))
    else:
        print("OK (no results)")

    return 0


def cmd_query(args) -> int:
    """List conversations with composable filters."""
    # Dispatch to sql subcommand if conversation_id is "sql"
    if args.conversation_id == "sql":
        return _query_sql(args)

    # Dispatch to detail view if conversation ID provided
    if args.conversation_id:
        return _query_detail(args)

    from siftd.api import list_conversations

    db = Path(args.db) if args.db else None

    try:
        conversations = list_conversations(
            db_path=db,
            workspace=args.workspace,
            model=args.model,
            since=parse_date(args.since),
            before=parse_date(args.before),
            tool=args.tool,
            tags=args.tag,
            all_tags=getattr(args, "all_tags", None),
            exclude_tags=getattr(args, "no_tag", None),
            tool_tag=getattr(args, "tool_tag", None),
            limit=args.limit,
            oldest_first=args.oldest,
        )
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'siftd ingest' to create it.")
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
        if args.json:
            print("[]")
        else:
            print("No conversations found.")
            # Provide helpful hints based on filters used
            has_filters = any([
                args.workspace, args.model, args.since, args.before,
                args.tool, args.tag,
                getattr(args, "all_tags", None),
                getattr(args, "no_tag", None),
                getattr(args, "tool_tag", None),
            ])
            if args.workspace:
                print(
                    "\nTip: Try 'siftd peek' for active sessions not yet ingested.",
                    file=sys.stderr,
                )
            elif has_filters:
                print(
                    "\nTip: No matches for current filters. Try broadening your search or run 'siftd query' without filters.",
                    file=sys.stderr,
                )
            else:
                print(
                    "\nTip: Run 'siftd ingest' to import recent sessions.",
                    file=sys.stderr,
                )
        return 0

    # JSON output
    if args.json:
        import json
        out = [
            {
                "id": c.id,
                "workspace": c.workspace_path,
                "model": c.model,
                "started_at": c.started_at,
                "prompts": c.prompt_count,
                "responses": c.response_count,
                "tokens": c.total_tokens,
                "cost": c.cost,
                "tags": c.tags,
            }
            for c in conversations
        ]
        print(json.dumps(out, indent=2))
        return 0

    from siftd.output import fmt_timestamp, fmt_tokens, fmt_workspace

    # Verbose mode: full table with all columns
    if args.verbose:
        columns = ["id", "workspace", "model", "started_at", "prompts", "responses", "tokens", "cost", "tags"]
        str_rows = []
        for c in conversations:
            cid = c.id[:12] if c.id else ""
            ws = fmt_workspace(c.workspace_path)
            model = c.model or ""
            started = fmt_timestamp(c.started_at)
            prompts = str(c.prompt_count)
            responses = str(c.response_count)
            tokens = str(c.total_tokens)
            cost = f"${c.cost:.4f}" if c.cost else "$0.0000"
            tags = ", ".join(c.tags) if c.tags else ""
            str_rows.append([cid, ws, model, started, prompts, responses, tokens, cost, tags])

        # Compute column widths and print table
        widths = [len(col) for col in columns]
        for str_row in str_rows:
            for i, val in enumerate(str_row):
                widths[i] = max(widths[i], len(val))

        header = "  ".join(col.ljust(widths[i]) for i, col in enumerate(columns))
        print(header)
        print("  ".join("-" * w for w in widths))
        for str_row in str_rows:
            print("  ".join(val.ljust(widths[i]) for i, val in enumerate(str_row)))
        return 0

    # Default: short mode — one dense line per conversation with truncated ID
    for c in conversations:
        cid = c.id[:12] if c.id else ""
        ws = fmt_workspace(c.workspace_path)
        model = c.model or ""
        started = fmt_timestamp(c.started_at)
        tokens = fmt_tokens(c.total_tokens)
        tag_str = f"  [{', '.join(c.tags)}]" if c.tags else ""
        print(f"{cid}  {started}  {ws}  {model}  {c.prompt_count}p/{c.response_count}r  {tokens} tok{tag_str}")

    # Stats summary (shown after list when --stats flag is set)
    if args.stats:
        total_convs = len(conversations)
        total_prompts = sum(c.prompt_count for c in conversations)
        total_responses = sum(c.response_count for c in conversations)
        total_tokens = sum(c.total_tokens for c in conversations)
        print()
        print("--- Stats ---")
        print(f"Conversations: {total_convs}")
        print(f"Total prompts: {total_prompts}")
        print(f"Total responses: {total_responses}")
        print(f"Total tokens: {fmt_tokens(total_tokens)}")

    return 0


def cmd_backfill(args) -> int:
    """Backfill derived data from existing records."""
    db = Path(args.db) if args.db else db_path()

    # Warn about --dry-run without --filter-binary
    if getattr(args, "dry_run", False) and not getattr(args, "filter_binary", False):
        print("Note: --dry-run ignored without --filter-binary", file=sys.stderr)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)

    if args.shell_tags:
        print("Backfilling shell command tags...")
        counts = backfill_shell_tags(conn)
        total = sum(counts.values())
        if counts:
            print(f"Tagged {total} tool calls:")
            for category, count in sorted(counts.items(), key=lambda x: -x[1]):
                print(f"  shell:{category}: {count}")
        else:
            print("No untagged shell commands found.")
    elif args.derivative_tags:
        print("Backfilling derivative conversation tags...")
        count = backfill_derivative_tags(conn)
        if count:
            print(f"Tagged {count} conversations as siftd:derivative.")
        else:
            print("No untagged derivative conversations found.")
    elif args.filter_binary:
        dry_run = getattr(args, "dry_run", False)
        if dry_run:
            print("Scanning for binary content (dry run)...")
        else:
            print("Filtering binary content from existing blobs...")
        stats = backfill_filter_binary(conn, dry_run=dry_run)
        print(f"  Filtered: {stats['filtered']}")
        print(f"  Skipped (no change): {stats['skipped']}")
        if stats['errors']:
            print(f"  Errors: {stats['errors']}")
        if dry_run and stats['filtered']:
            print("\nRun without --dry-run to apply changes.")
    else:
        # Default: backfill response attributes (original behavior)
        print("Backfilling response attributes (cache tokens)...")
        count = backfill_response_attributes(conn)
        print(f"Done. Inserted {count} attributes.")

    conn.close()
    return 0


def cmd_migrate(args) -> int:
    """Run data migrations."""
    from siftd.storage.migrate_workspaces import (
        backfill_git_remotes,
        merge_duplicate_workspaces,
        verify_workspace_identity,
    )

    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)

    if args.merge_workspaces:
        # Step 1: Backfill git remotes
        print("Step 1: Backfilling git remote URLs for existing workspaces...")

        def on_backfill_progress(msg):
            if args.verbose:
                print(msg)

        stats = backfill_git_remotes(conn, on_progress=on_backfill_progress, dry_run=args.dry_run)
        print(f"  Checked: {stats['checked']}")
        print(f"  Updated: {stats['updated']}")
        print(f"  Skipped (path missing): {stats['skipped_missing']}")
        print(f"  Skipped (no git remote): {stats['skipped_no_git']}")

        # Step 2: Find and optionally merge duplicates
        print("\nStep 2: Finding duplicate workspaces...")
        status = verify_workspace_identity(conn)

        if status["duplicate_groups"] == 0:
            print("  No duplicate workspaces found.")
            conn.close()
            return 0

        print(f"  Found {status['duplicate_groups']} groups with {status['duplicate_workspaces']} workspaces sharing git remotes.")

        if args.dry_run:
            print("\n[Dry run] Would merge the following workspaces:")

        def on_merge_progress(msg):
            print(msg)

        merge_stats = merge_duplicate_workspaces(
            conn, on_progress=on_merge_progress, dry_run=args.dry_run
        )

        if args.dry_run:
            print(f"\n[Dry run] Would merge {merge_stats['workspaces_merged']} workspaces.")
            print("Run without --dry-run to apply changes.")
        else:
            print(f"\nMerged {merge_stats['workspaces_merged']} workspaces.")
            print(f"Moved {merge_stats['conversations_moved']} conversations.")
    else:
        # Show current status
        status = verify_workspace_identity(conn)
        print("Workspace identity status:")
        print(f"  Total workspaces: {status['total']}")
        print(f"  With git remote: {status['with_remote']}")
        print(f"  Without git remote: {status['without_remote']}")
        if status["duplicate_groups"] > 0:
            print(f"  Duplicate groups: {status['duplicate_groups']} ({status['duplicate_workspaces']} workspaces)")
            print("\nRun 'siftd migrate --merge-workspaces' to merge duplicates.")

    conn.close()
    return 0


def cmd_config(args) -> int:
    """View or modify config settings."""
    from siftd.config import get_config, set_config
    from siftd.paths import config_file

    # siftd config path
    if args.action == "path":
        print(config_file())
        return 0

    # siftd config get <key>
    if args.action == "get":
        if not args.key:
            print("Usage: siftd config get <key>")
            print("Example: siftd config get search.formatter")
            return 1
        value = get_config(args.key)
        if value is None:
            print(f"Key not set: {args.key}")
            return 1
        print(value)
        return 0

    # siftd config set <key> <value>
    if args.action == "set":
        if not args.key or not args.value:
            print("Usage: siftd config set <key> <value>")
            print("Example: siftd config set search.formatter verbose")
            return 1
        set_config(args.key, args.value)
        print(f"Set {args.key} = {args.value}")
        return 0

    # siftd config (show all)
    path = config_file()
    if not path.exists():
        print("No config file found.")
        print(f"Create one at: {path}")
        return 0

    print(path.read_text().strip())
    return 0


def cmd_adapters(args) -> int:
    """List discovered adapters."""
    from siftd.api import list_adapters

    adapters = list_adapters()

    if not adapters:
        if args.json:
            print("[]")
        else:
            print("No adapters found.")
        return 0

    # JSON output
    if args.json:
        import json

        out = [
            {
                "name": a.name,
                "origin": a.origin,
                "locations": a.locations,
                "source_path": a.source_path,
                "entrypoint": a.entrypoint,
            }
            for a in adapters
        ]
        print(json.dumps(out, indent=2))
        return 0

    # Compute column widths
    name_width = max(len(a.name) for a in adapters)
    origin_width = max(len(a.origin) for a in adapters)

    # Header
    print(f"{'NAME':<{name_width}}  {'ORIGIN':<{origin_width}}  LOCATIONS")

    for a in adapters:
        locations = ", ".join(a.locations) if a.locations else "-"
        print(f"{a.name:<{name_width}}  {a.origin:<{origin_width}}  {locations}")

    return 0


def cmd_copy(args) -> int:
    """Copy built-in resources to config directory for customization."""
    from siftd.api import (
        CopyError,
        copy_adapter,
        copy_query,
        list_builtin_adapters,
        list_builtin_queries,
    )

    resource_type = args.resource_type
    name = args.name
    force = args.force
    copy_all = args.all

    if resource_type == "adapter":
        if copy_all:
            # Copy all built-in adapters
            names = list_builtin_adapters()
            if not names:
                print("No built-in adapters available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_adapter(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    print(f"Error copying {n}: {e}")
            if copied:
                print("Copied adapters:")
                for n, dest in copied:
                    print(f"  {n} → {dest}")
            return 0

        if not name:
            print("Usage: siftd copy adapter <name> [--force]")
            print("       siftd copy adapter --all [--force]")
            print("\nAvailable adapters:")
            for n in list_builtin_adapters():
                print(f"  {n}")
            return 1

        try:
            dest = copy_adapter(name, force=force)
            print(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            print(f"Error: {e}")
            return 1

    elif resource_type == "query":
        if copy_all:
            names = list_builtin_queries()
            if not names:
                print("No built-in queries available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_query(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    print(f"Error copying {n}: {e}")
            if copied:
                print("Copied queries:")
                for n, dest in copied:
                    print(f"  {n} → {dest}")
            return 0

        if not name:
            available = list_builtin_queries()
            print("Usage: siftd copy query <name> [--force]")
            print("       siftd copy query --all [--force]")
            if available:
                print("\nAvailable queries:")
                for n in available:
                    print(f"  {n}")
            else:
                print("\nNo built-in queries available.")
            return 1

        try:
            dest = copy_query(name, force=force)
            print(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            print(f"Error: {e}")
            return 1

    else:
        print(f"Unknown resource type: {resource_type}")
        print("Supported: adapter, query")
        return 1


def _doctor_fix_pending_tags(args) -> int:
    """Clean up stale sessions and orphaned pending tags."""
    from siftd.api.sessions import cleanup_stale_sessions

    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)

    sessions_deleted, tags_deleted = cleanup_stale_sessions(conn, max_age_hours=48, commit=True)

    if args.json:
        import json
        out = {
            "sessions_deleted": sessions_deleted,
            "tags_deleted": tags_deleted,
        }
        print(json.dumps(out, indent=2))
    else:
        if sessions_deleted or tags_deleted:
            print(f"Cleaned up {sessions_deleted} stale session(s) and {tags_deleted} orphaned tag(s)")
        else:
            print("No stale sessions or orphaned tags to clean up")

    conn.close()
    return 0


def _doctor_list(args) -> int:
    """List available doctor checks."""
    from siftd.api import list_checks

    checks = list_checks()
    if args.json:
        import json

        out = [
            {"name": c.name, "description": c.description, "has_fix": c.has_fix}
            for c in checks
        ]
        print(json.dumps(out, indent=2))
        return 0
    print("Available checks:")
    for check in checks:
        fix_marker = " [fix]" if check.has_fix else ""
        print(f"  {check.name}{fix_marker}")
        print(f"    {check.description}")
    return 0


def _doctor_run(args, check_names: list[str] | None = None, show_fixes: bool = False) -> int:
    """Run doctor checks and display findings."""
    from siftd.api import run_checks

    db = Path(args.db) if args.db else None

    try:
        findings = run_checks(checks=check_names or None, db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    # JSON output
    if args.json:
        import json

        # Sort same as text mode: severity descending, then check name
        severity_order = {"error": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: (severity_order.get(f.severity, 3), f.check))

        error_count = sum(1 for f in findings if f.severity == "error")
        warning_count = sum(1 for f in findings if f.severity == "warning")
        out = {
            "findings": [
                {
                    "check": f.check,
                    "severity": f.severity,
                    "message": f.message,
                    "fix_available": f.fix_available,
                    "fix_command": f.fix_command,
                    "context": f.context,
                }
                for f in findings
            ],
            "summary": {
                "total": len(findings),
                "error": error_count,
                "warning": warning_count,
                "info": sum(1 for f in findings if f.severity == "info"),
            },
        }
        print(json.dumps(out, indent=2))
        fail_count = error_count + warning_count if args.strict else error_count
        return 1 if fail_count > 0 else 0

    if not findings:
        print("No issues found.")
        return 0

    # Display findings grouped by severity
    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 3), f.check))

    icons = {"info": "i", "warning": "!", "error": "x"}

    for finding in findings:
        icon = icons.get(finding.severity, "?")
        print(f"[{icon}] {finding.check}: {finding.message}")
        if finding.fix_command and not show_fixes:
            print(f"    Fix: {finding.fix_command}")

    # Summary
    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")

    print()
    print(f"Found {len(findings)} issue(s): {error_count} error, {warning_count} warning, {info_count} info")

    # Show consolidated fix commands
    if show_fixes:
        fixable = [f for f in findings if f.fix_available and f.fix_command]
        if fixable:
            print("\nTo fix these issues, run:")
            seen_commands = set()
            for f in fixable:
                if f.fix_command not in seen_commands:
                    print(f"  {f.fix_command}")
                    seen_commands.add(f.fix_command)

    fail_count = error_count + warning_count if args.strict else error_count
    return 1 if fail_count > 0 else 0


def cmd_doctor(args) -> int:
    """Run health checks and report findings."""
    subcommand_args = args.subcommand or []
    action = subcommand_args[0] if subcommand_args else None

    # Warn about --pending-tags without fix subcommand
    if getattr(args, "pending_tags", False) and action != "fix":
        print("Note: --pending-tags ignored without 'fix' subcommand", file=sys.stderr)

    # New subcommands: list, run, fix
    if action == "list":
        return _doctor_list(args)

    if action == "run":
        # doctor run [check1] [check2] ...
        check_names = subcommand_args[1:] if len(subcommand_args) > 1 else None
        return _doctor_run(args, check_names=check_names)

    if action == "fix":
        # doctor fix --pending-tags — clean up stale sessions and orphaned pending tags
        if getattr(args, "pending_tags", False):
            return _doctor_fix_pending_tags(args)
        # doctor fix — run all checks and show fixes
        return _doctor_run(args, show_fixes=True)

    # Legacy: siftd doctor checks
    if action == "checks":
        return _doctor_list(args)

    # Legacy: siftd doctor fixes
    if action == "fixes":
        return _doctor_run(args, show_fixes=True)

    # Legacy: siftd doctor <check-name> (single check)
    if action:
        return _doctor_run(args, check_names=[action])

    # Default: siftd doctor (run all checks)
    return _doctor_run(args)


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


def cmd_export(args) -> int:
    """Export conversations for PR review."""
    from siftd.api import ExportOptions, export_conversations, format_export

    db = Path(args.db) if args.db else db_path()

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


def _print_stats(stats: IngestStats) -> None:
    """Print ingestion statistics."""
    print(f"\n{'='*50}")
    print("SUMMARY")
    print(f"{'='*50}")
    print(f"Files found:    {stats.files_found}")
    print(f"Files ingested: {stats.files_ingested}")
    print(f"Files replaced: {stats.files_replaced}")
    print(f"Files skipped:  {stats.files_skipped}")
    print(f"\nConversations: {stats.conversations}")
    print(f"Prompts:       {stats.prompts}")
    print(f"Responses:     {stats.responses}")
    print(f"Tool calls:    {stats.tool_calls}")

    if stats.by_harness:
        print("\n--- By Harness ---")
        for harness, h_stats in stats.by_harness.items():
            print(f"\n{harness}:")
            for key, value in h_stats.items():
                print(f"  {key}: {value}")


def _get_version() -> str:
    """Get package version from metadata."""
    try:
        from importlib.metadata import version
        return version("siftd")
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="siftd",
        description="Aggregate and query LLM conversation logs",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"siftd {_get_version()}",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"Database path (default: {db_path()})",
    )

    subparsers = parser.add_subparsers(dest="command")

    # ingest
    p_ingest = subparsers.add_parser(
        "ingest",
        help="Ingest logs from all sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd ingest                      # ingest from all adapters
  siftd ingest -v                   # show all files including skipped
  siftd ingest -a claude_code       # only run claude_code adapter
  siftd ingest -p ~/logs -p /tmp    # scan additional directories
  siftd ingest --rebuild-fts        # rebuild FTS index from scratch""",
    )
    p_ingest.add_argument("-v", "--verbose", action="store_true", help="Show all files including skipped")
    p_ingest.add_argument("-p", "--path", action="append", metavar="DIR", help="Additional directories to scan (can be repeated)")
    p_ingest.add_argument("-a", "--adapter", action="append", metavar="NAME", help="Only run specific adapter(s) (can be repeated)")
    p_ingest.add_argument("--rebuild-fts", action="store_true", help="Rebuild FTS index from existing data (skips ingestion)")
    p_ingest.set_defaults(func=cmd_ingest)

    # status
    p_status = subparsers.add_parser("status", help="Show database statistics")
    p_status.add_argument("--json", action="store_true", help="Output as JSON")
    p_status.set_defaults(func=cmd_status)

    # workspaces
    p_workspaces = subparsers.add_parser(
        "workspaces",
        help="List workspaces with conversation counts",
    )
    p_workspaces.add_argument("--json", action="store_true", help="Output as JSON")
    p_workspaces.add_argument(
        "-n", "--limit", type=int, default=0, help="Max workspaces (0 = all)"
    )
    p_workspaces.set_defaults(func=cmd_workspaces)

    # search (semantic search) — defined in cli_search.py
    build_search_parser(subparsers)

    # install (optional dependencies) — defined in cli_install.py
    build_install_parser(subparsers)

    # register
    p_register = subparsers.add_parser(
        "register",
        help="Register an active session for live tagging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd register --session abc123 --adapter claude_code
  siftd register --session abc123 --adapter claude_code --workspace /path/to/project""",
    )
    p_register.add_argument("--session", "-s", required=True, metavar="ID", help="Harness session ID")
    p_register.add_argument("--adapter", "-a", required=True, metavar="NAME", help="Adapter name (e.g., claude_code)")
    p_register.add_argument("--workspace", "-w", metavar="PATH", help="Workspace path (default: current directory)")
    p_register.set_defaults(func=cmd_register)

    # session-id
    p_session_id = subparsers.add_parser(
        "session-id",
        help="Print the session ID for the current workspace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd session-id                    # print session ID for current directory
  siftd session-id --workspace /path  # print session ID for specific workspace

Exits with code 1 if no session ID found (for scripting).""",
    )
    p_session_id.add_argument("--workspace", "-w", metavar="PATH", help="Workspace path (default: current directory)")
    p_session_id.set_defaults(func=cmd_session_id)

    # tag
    p_tag = subparsers.add_parser(
        "tag",
        help="Apply or remove a tag on a conversation (or other entity)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd tag 01HX... important              # tag conversation (default)
  siftd tag 01HX... important review       # apply multiple tags at once
  siftd tag --last 1 important             # tag most recent conversation
  siftd tag --last 3 review                # tag 3 most recent conversations
  siftd tag workspace 01HY... proj         # explicit entity type
  siftd tag tool_call 01HZ... slow         # tag a tool call
  siftd tag --remove 01HX... important     # remove tag from conversation
  siftd tag --remove --last 1 important    # remove from most recent
  siftd tag -r workspace 01HY... proj      # remove from workspace

live session tagging:
  siftd tag --session abc123 decision:auth       # queue tag for session
  siftd tag --session abc123 --exchange 5 key    # queue tag for exchange 5""",
    )
    p_tag.add_argument("positional", nargs="*", help="[entity_type] entity_id tag [tag2 ...]")
    p_tag.add_argument("-n", "--last", type=int, metavar="N", help="Tag N most recent conversations")
    p_tag.add_argument("-r", "--remove", action="store_true", help="Remove tag instead of applying")
    p_tag.add_argument("--session", metavar="ID", help="Queue tag for a live session (applied at ingest)")
    p_tag.add_argument("--exchange", type=int, metavar="INDEX", help="Tag specific exchange (0-based, requires --session)")
    p_tag.set_defaults(func=cmd_tag)

    # tags
    p_tags = subparsers.add_parser(
        "tags",
        help="List, inspect, rename, or delete tags",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd tags                                      # list all tags
  siftd tags --prefix research:                   # list tags by prefix
  siftd tags research:auth                        # show conversations with a tag
  siftd tags --rename important review:important   # rename tag
  siftd tags --delete old-tag                      # delete tag (refuses if applied)
  siftd tags --delete old-tag --force              # delete tag and all associations""",
    )
    p_tags.add_argument("name", nargs="?", help="Tag name to drill into (shows conversations)")
    p_tags.add_argument("--prefix", metavar="PREFIX", help="Filter tag list by prefix (list view only)")
    p_tags.add_argument("-n", "--limit", type=int, default=10, help="Max conversations to show in drill-down (default: 10)")
    p_tags.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"), help="Rename a tag")
    p_tags.add_argument("--delete", metavar="NAME", help="Delete a tag and all associations")
    p_tags.add_argument("--force", action="store_true", help="Force delete even if tag has associations")
    p_tags.set_defaults(func=cmd_tags)

    # tools
    p_tools = subparsers.add_parser(
        "tools",
        help="Summarize tool usage by category",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd tools                    # shell command categories summary
  siftd tools --by-workspace     # breakdown by workspace
  siftd tools --prefix shell:    # filter by tag prefix""",
    )
    p_tools.add_argument("--by-workspace", action="store_true", help="Show breakdown by workspace")
    p_tools.add_argument("--prefix", metavar="PREFIX", help="Tag prefix to filter (default: shell:)")
    p_tools.add_argument("-n", "--limit", type=int, default=20, help="Max workspaces for --by-workspace (default: 20)")
    p_tools.add_argument("--json", action="store_true", help="Output as JSON")
    p_tools.set_defaults(func=cmd_tools)

    # query
    p_query = subparsers.add_parser(
        "query",
        help="List and filter conversations by metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""List and filter conversations by metadata (workspace, model, date, tags).
For semantic content search, use: siftd search <query>

examples:
  siftd query                         # list recent conversations
  siftd query -n 20                   # list 20 conversations
  siftd query -w myproject            # filter by workspace
  siftd query -l research:auth        # conversations tagged research:auth
  siftd query -l research: -l useful: # OR — any research: or useful: tag
  siftd query --all-tags important --all-tags reviewed  # AND — must have both
  siftd query -l research: --no-tag archived            # combine OR + NOT
  siftd query --tool-tag shell:test   # conversations with test commands
  siftd query <id>                    # show conversation detail
  siftd query <id> --summary          # metadata only, no exchanges
  siftd query <id> --exchanges 5      # last 5 exchanges
  siftd query <id> --brief            # brief output (80 char truncation)
  siftd query <id> --full             # full text, no truncation
  siftd query sql                     # list available .sql files
  siftd query sql cost                # run the 'cost' query
  siftd query sql cost --var ws=proj  # run with variable substitution""",
    )

    # Positional arguments
    p_query.add_argument("conversation_id", nargs="?", help="Conversation ID for detail view, or 'sql' for SQL query mode")
    p_query.add_argument("sql_name", nargs="?", help="SQL query name (when using 'sql' subcommand)")

    # Filtering options
    filter_group = p_query.add_argument_group("filtering")
    filter_group.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    filter_group.add_argument("-m", "--model", metavar="NAME", help="Filter by model name")
    filter_group.add_argument("--since", metavar="DATE", type=parse_date, help="Conversations started after this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    filter_group.add_argument("--before", metavar="DATE", type=parse_date, help="Conversations started before this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    filter_group.add_argument("-t", "--tool", metavar="NAME", help="Filter by canonical tool name (e.g. shell.execute)")

    # Tag filtering options
    tag_group = p_query.add_argument_group("tag filtering")
    tag_group.add_argument("-l", "--tag", action="append", metavar="NAME", help="Filter by tag (repeatable, OR logic)")
    tag_group.add_argument("--all-tags", action="append", metavar="NAME", help="Require all specified tags (AND logic)")
    tag_group.add_argument("--no-tag", action="append", metavar="NAME", help="Exclude conversations with this tag (NOT logic)")
    tag_group.add_argument("--tool-tag", metavar="NAME", help="Filter by tool call tag (e.g. shell:test)")

    # Output options
    output_group = p_query.add_argument_group("output")
    output_group.add_argument("-n", "--limit", type=int, default=10, help="Number of conversations to show (0=all, default: 10)")
    output_group.add_argument("-v", "--verbose", action="store_true", help="Full table with all columns")
    output_group.add_argument("--oldest", action="store_true", help="Sort by oldest first (default: newest first)")
    output_group.add_argument("--json", action="store_true", help="Output as JSON array")
    output_group.add_argument("--stats", action="store_true", help="Show summary totals after list")

    # Detail view options (when conversation_id is provided)
    detail_group = p_query.add_argument_group("detail view")
    detail_group.add_argument("--exchanges", type=int, metavar="N", help="Number of exchanges to show (default: all)")
    detail_group.add_argument("--brief", action="store_true", help="Brief output (80 char truncation)")
    detail_group.add_argument("--summary", action="store_true", help="Summary only (metadata, no exchanges)")
    detail_group.add_argument("--full", action="store_true", help="Full text (no truncation)")
    detail_group.add_argument("--chars", type=int, metavar="N", help="Truncate text at N characters (default: 200)")

    # SQL query options
    sql_group = p_query.add_argument_group("sql queries")
    sql_group.add_argument("--var", action="append", metavar="KEY=VALUE", help="Substitute $KEY with VALUE in SQL")

    p_query.set_defaults(func=cmd_query)

    # backfill
    p_backfill = subparsers.add_parser(
        "backfill",
        help="Backfill derived data from existing records",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd backfill                    # backfill response attributes (cache tokens)
  siftd backfill --shell-tags       # categorize shell commands as shell:git, shell:test, etc.
  siftd backfill --derivative-tags  # mark siftd-generated conversations
  siftd backfill --filter-binary    # filter binary content from existing blobs
  siftd backfill --filter-binary --dry-run  # preview what would be filtered""",
    )
    p_backfill.add_argument("--shell-tags", action="store_true", help="Tag shell.execute calls with shell:* categories")
    p_backfill.add_argument("--derivative-tags", action="store_true", help="Tag conversations containing siftd search/query as siftd:derivative")
    p_backfill.add_argument("--filter-binary", action="store_true", help="Filter binary content (images, base64) from existing blobs")
    p_backfill.add_argument("--dry-run", action="store_true", help="Preview changes without applying (use with --filter-binary)")
    p_backfill.set_defaults(func=cmd_backfill)

    # migrate
    p_migrate = subparsers.add_parser(
        "migrate",
        help="Run data migrations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd migrate                              # show workspace identity status
  siftd migrate --merge-workspaces           # backfill git remotes and merge duplicates
  siftd migrate --merge-workspaces --dry-run # preview what would be merged
  siftd migrate --merge-workspaces -v        # verbose output""",
    )
    p_migrate.add_argument(
        "--merge-workspaces",
        action="store_true",
        help="Backfill git remote URLs and merge duplicate workspaces"
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    p_migrate.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    p_migrate.set_defaults(func=cmd_migrate)

    # path
    p_path = subparsers.add_parser("path", help="Show XDG paths")
    p_path.set_defaults(func=cmd_path)

    # config
    p_config = subparsers.add_parser(
        "config",
        help="View or modify config settings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd config                        # show all config
  siftd config path                   # show config file path
  siftd config get search.formatter      # get specific value
  siftd config set search.formatter verbose  # set value""",
    )
    p_config.add_argument("action", nargs="?", choices=["get", "set", "path"], help="Action to perform")
    p_config.add_argument("key", nargs="?", help="Config key (dotted path, e.g., search.formatter)")
    p_config.add_argument("value", nargs="?", help="Value to set (for 'set' action)")
    p_config.set_defaults(func=cmd_config)

    # adapters
    p_adapters = subparsers.add_parser("adapters", help="List discovered adapters")
    p_adapters.add_argument("--json", action="store_true", help="Output as JSON")
    p_adapters.set_defaults(func=cmd_adapters)

    # copy
    p_copy = subparsers.add_parser(
        "copy",
        help="Copy built-in resources for customization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd copy adapter claude_code    # copy adapter to ~/.config/siftd/adapters/
  siftd copy adapter --all          # copy all built-in adapters
  siftd copy query cost             # copy query to ~/.config/siftd/queries/""",
    )
    p_copy.add_argument("resource_type", choices=["adapter", "query"], help="Resource type to copy")
    p_copy.add_argument("name", nargs="?", help="Resource name")
    p_copy.add_argument("--all", action="store_true", help="Copy all resources of this type")
    p_copy.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_copy.set_defaults(func=cmd_copy)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Run health checks and maintenance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd doctor                          # run all checks
  siftd doctor list                     # list available checks
  siftd doctor run                      # run all checks (explicit)
  siftd doctor run ingest-pending       # run specific check
  siftd doctor run check1 check2        # run multiple checks
  siftd doctor fix                      # show fix commands for issues
  siftd doctor fix --pending-tags       # clean up stale sessions/tags
  siftd doctor --json                   # output as JSON
  siftd doctor --strict                 # exit 1 on warnings (for CI)

legacy (still supported):
  siftd doctor checks                   # same as 'list'
  siftd doctor fixes                    # same as 'fix'
  siftd doctor ingest-pending           # same as 'run ingest-pending'

exit codes:
  0  no errors (or no warnings with --strict)
  1  errors found (or warnings with --strict)""",
    )
    p_doctor.add_argument("subcommand", nargs="*", help="list | run [checks...] | fix | <check-name>")
    p_doctor.add_argument("--json", action="store_true", help="Output as JSON")
    p_doctor.add_argument("--strict", action="store_true", help="Exit 1 on warnings (not just errors). Useful for CI.")
    p_doctor.add_argument("--pending-tags", action="store_true", help="Clean up stale sessions and orphaned pending tags (use with 'fix')")
    p_doctor.set_defaults(func=cmd_doctor)

    # peek
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

    # export
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
    p_export.add_argument("-f", "--format", choices=["prompts", "exchanges", "json"], default="prompts",
                          help="Output format: prompts (default), exchanges, json")
    p_export.add_argument("--prompts-only", action="store_true", help="Omit response text and tool calls")
    p_export.add_argument("--no-header", action="store_true", help="Omit session metadata header")
    p_export.add_argument("-o", "--output", metavar="FILE", help="Write to file instead of stdout")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    if not hasattr(args, "func") or args.func is None:
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Exit cleanly on Ctrl+C (130 = 128 + SIGINT)
        return 130


if __name__ == "__main__":
    sys.exit(main())
