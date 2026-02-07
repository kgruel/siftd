"""CLI handlers for tag commands (tag, tags)."""

import argparse
import sys
from pathlib import Path

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
from siftd.api.sessions import is_session_registered
from siftd.api.sessions import queue_tag as queue_pending_tag
from siftd.paths import db_path, ensure_dirs


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
                print(f"Renamed '{old_name}' → '{new_name}'")
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


def build_tags_parser(subparsers) -> None:
    """Add 'tag' and 'tags' subparsers."""
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
