"""CLI handlers for 'siftd db' namespace — container-level operations.

Commands that operate on the database container itself:
info, stats, workspaces, path, vacuum, backup, restore, slice, merge,
remote (add/list/remove), push.

Distinct from top-level commands (query, search, export, tag, peek)
which are user workflows selecting conversations.
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from siftd.cli_common import parse_date, resolve_db


def cmd_db_info(args) -> int:
    """Show database file metadata and schema information."""
    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    from siftd.api import open_database

    conn = open_database(db, read_only=True)
    try:
        size_bytes = db.stat().st_size
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # Check for FTS5 table
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchone() is not None

        print(f"Path:           {db}")
        print(f"Size:           {size_bytes / 1024:.1f} KB ({size_bytes:,} bytes)")
        print(f"Page size:      {page_size:,} bytes")
        print(f"Page count:     {page_count:,}")
        print(f"Journal mode:   {journal_mode}")
        print(f"Schema version: {user_version}")
        print(f"FTS5 index:     {'yes' if fts_exists else 'no'}")
    finally:
        conn.close()

    return 0


def cmd_db_stats(args) -> int:
    """Show database statistics (delegates to status implementation)."""
    from siftd.cli_meta import cmd_status

    return cmd_status(args)


def cmd_db_workspaces(args) -> int:
    """List workspaces (delegates to workspaces implementation)."""
    from siftd.cli_meta import cmd_workspaces

    return cmd_workspaces(args)


def cmd_db_path(args) -> int:
    """Show XDG paths (delegates to path implementation)."""
    from siftd.cli_meta import cmd_path

    return cmd_path(args)


def cmd_db_vacuum(args) -> int:
    """Compact the database and optimize indexes."""
    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        return 1

    size_before = db.stat().st_size

    from siftd.api import open_database

    conn = open_database(db)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()

    size_after = db.stat().st_size
    saved = size_before - size_after
    print(f"Before: {size_before / 1024:.1f} KB")
    print(f"After:  {size_after / 1024:.1f} KB")
    if saved > 0:
        print(f"Saved:  {saved / 1024:.1f} KB ({saved / size_before * 100:.1f}%)")
    else:
        print("No space reclaimed (database already compact).")

    return 0


def cmd_db_backup(args) -> int:
    """Create a consistent backup of the database."""
    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        return 1

    target = Path(args.output)
    if target.exists() and not args.force:
        print(f"Target already exists: {target}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        return 1

    if target.exists():
        target.unlink()

    from siftd.api.database import backup_database

    backup_database(db, target)

    size = target.stat().st_size
    print(f"Backed up to: {target} ({size / 1024:.1f} KB)")
    return 0


def cmd_db_restore(args) -> int:
    """Restore the database from a backup file."""
    source = Path(args.input)

    if not source.exists():
        print(f"Backup file not found: {source}", file=sys.stderr)
        return 1

    # Validate SQLite magic bytes
    with open(source, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3\x00"):
        print(f"Not a valid SQLite database: {source}", file=sys.stderr)
        return 1

    db = resolve_db(args)

    if db.exists() and not args.force:
        print(f"Database already exists: {db}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        return 1

    import shutil

    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, db)
    size = db.stat().st_size
    print(f"Restored to: {db} ({size / 1024:.1f} KB)")
    return 0


def cmd_db_slice(args) -> int:
    """Export a filtered subset of conversations into a standalone SQLite database."""
    from siftd.api.slice import slice_database
    from siftd.cli_filters import extract_filter_args

    db = resolve_db(args)
    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    target = Path(args.output)
    if target.exists() and not args.force:
        print(f"Target already exists: {target}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        return 1

    if target.exists():
        target.unlink()

    filters = extract_filter_args(args)
    rebuild_fts = not args.no_fts

    try:
        result = slice_database(
            source_db=db,
            target_path=target,
            workspace=filters.workspace,
            model=filters.model,
            since=filters.since,
            before=filters.before,
            tags=filters.tags,
            all_tags=filters.all_tags,
            exclude_tags=filters.exclude_tags,
            tool=filters.tool,
            tool_tag=filters.tool_tag,
            search=filters.search,
            rebuild_fts=rebuild_fts,
        )
    except FileNotFoundError as e:
        print(str(e))
        return 1

    count = result["conversations"]
    size = result["size_bytes"]
    print(f"Sliced {count} conversation(s) to: {target} ({size / 1024:.1f} KB)")
    return 0


def cmd_db_merge(args) -> int:
    """Merge an external database (slice) into the main database."""
    source = Path(args.input)

    if not source.exists():
        print(f"Source file not found: {source}", file=sys.stderr)
        return 1

    # Validate SQLite magic bytes
    with open(source, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3\x00"):
        print(f"Not a valid SQLite database: {source}", file=sys.stderr)
        return 1

    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    from siftd.api.merge import merge_database

    rebuild_fts = not args.no_fts
    dry_run = args.dry_run
    replace = not args.no_replace

    try:
        result = merge_database(
            target_db=db,
            source_path=source,
            rebuild_fts=rebuild_fts,
            dry_run=dry_run,
            replace=replace,
        )
    except RuntimeError as e:
        print(f"Merge failed: {e}", file=sys.stderr)
        return 1

    prefix = "[dry run] " if dry_run else ""
    print(f"{prefix}Merged from: {source}")
    conv_parts = [f"{result['conversations']} new"]
    if result["replaced_conversations"]:
        conv_parts.append(f"{result['replaced_conversations']} replaced")
    conv_parts.append(f"{result['skipped_conversations']} skipped")
    print(f"  Conversations: {', '.join(conv_parts)}")
    print(f"  Prompts:       {result['prompts']}")
    print(f"  Responses:     {result['responses']}")
    print(f"  Tool calls:    {result['tool_calls']}")
    print(f"  Content blobs: {result['content_blobs']}")
    if result["tags"]:
        print(f"  Tags:          {result['tags']} new")
    if result["workspaces_matched"]:
        print(f"  Workspaces:    {result['workspaces_matched']} matched by git remote")

    return 0


def cmd_db_receive(args) -> int:
    """Receive a database from stdin and create-or-merge into the local database.

    Designed to be called via SSH pipe:
        ssh host siftd --db /path db receive < slice.db
    """
    db = resolve_db(args)

    if sys.stdin.isatty():
        print(
            json.dumps({"error": "No data on stdin. Pipe a database file."}),
            file=sys.stderr,
        )
        return 1

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="siftd-receive-", suffix=".db", delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            shutil.copyfileobj(sys.stdin.buffer, tmp)

        if tmp_path.stat().st_size == 0:
            print(
                json.dumps({"error": "Empty input on stdin."}),
                file=sys.stderr,
            )
            return 1

        from siftd.api.receive import receive_database

        rebuild_fts = not args.no_fts
        result = receive_database(tmp_path, db, rebuild_fts=rebuild_fts)
        print(json.dumps(result))
        return 0

    except ValueError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except sqlite3.OperationalError as e:
        msg = str(e)
        error_type = "database_locked" if "locked" in msg else "sqlite_error"
        print(
            json.dumps({"error": msg, "error_type": error_type}),
            file=sys.stderr,
        )
        return 1
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def cmd_db_remote_add(args) -> int:
    """Register a sync remote."""
    from siftd.config import set_sync_remote

    name = args.name
    target = args.target

    # Parse host:path vs local path
    # Heuristic: if it contains ':' and the part before ':' doesn't look like
    # a drive letter (Windows) or start with '/', treat as host:path.
    if ":" in target and not target.startswith("/"):
        parts = target.split(":", 1)
        host = parts[0]
        path = parts[1]
    else:
        host = None
        path = target

    set_sync_remote(name, host, path)

    if host:
        print(f"Added remote '{name}': {host}:{path}")
    else:
        print(f"Added remote '{name}': {path} (local)")
    return 0


def cmd_db_remote_list(args) -> int:
    """List sync remotes."""
    from siftd.config import get_sync_remotes

    remotes = get_sync_remotes()
    if not remotes:
        print("No remotes configured.")
        print("Add one with: siftd db remote add <name> <host:path>")
        return 0

    for r in remotes:
        location = f"{r['host']}:{r['path']}" if r["host"] else f"{r['path']} (local)"
        print(f"{r['name']:20s} {location}")
        if r["last_push"]:
            print(f"{'':20s} last push: {r['last_push']}")
    return 0


def cmd_db_remote_remove(args) -> int:
    """Unregister a sync remote."""
    from siftd.config import remove_sync_remote

    if remove_sync_remote(args.name):
        print(f"Removed remote '{args.name}'.")
        return 0
    else:
        print(f"Remote '{args.name}' not found.", file=sys.stderr)
        return 1


def cmd_db_push(args) -> int:
    """Push conversations to a sync remote."""
    from siftd.api.sync import SyncError, SyncRemote, sync_push
    from siftd.config import get_sync_remote

    remote_cfg = get_sync_remote(args.name)
    if remote_cfg is None:
        print(f"Remote '{args.name}' not found.", file=sys.stderr)
        print("Run 'siftd db remote list' to see configured remotes.", file=sys.stderr)
        return 1

    remote = SyncRemote(**remote_cfg)

    db = resolve_db(args)
    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    dry_run = getattr(args, "dry_run", False)
    location = f"{remote.host}:{remote.path}" if remote.host else remote.path

    if not dry_run:
        print(f"Pushing to {args.name} ({location})...", file=sys.stderr)

    try:
        result = sync_push(
            db_path=db,
            remote=remote,
            since=getattr(args, "since", None),
            push_all=getattr(args, "push_all", False),
            workspace=getattr(args, "workspace", None),
            dry_run=dry_run,
        )
    except SyncError as e:
        print(f"Push failed: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(str(e))
        return 1

    if result.conversations == 0:
        print(f"Nothing new to push to {args.name}.")
        return 0

    size_kb = result.size_bytes / 1024
    suffix = " (new remote database)" if not result.remote_existed else ""

    if result.dry_run:
        print(f"Would push {result.conversations} conversations to {args.name} ({size_kb:.1f} KB)")
    else:
        print(f"Pushed {result.conversations} conversations ({size_kb:.1f} KB){suffix}")
    return 0


def build_db_parser(subparsers) -> None:
    """Add the 'db' subparser with nested subcommands."""
    p_db = subparsers.add_parser(
        "db",
        help="Database operations (info, backup, restore, vacuum, slice, merge, receive, remote, push)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Container-level operations on the siftd database.

examples:
  siftd db info                          # database file metadata
  siftd db stats                         # full statistics
  siftd db workspaces                    # list workspaces
  siftd db path                          # show XDG paths
  siftd db vacuum                        # compact database
  siftd db backup /tmp/siftd.db          # online backup
  siftd db restore /tmp/siftd.db         # restore from backup
  siftd db slice out.db -w project       # export filtered subset
  siftd db merge laptop-slice.db         # merge slice into main DB
  siftd db receive < slice.db            # receive via stdin (SSH pipe)
  siftd db remote add alcove host:path   # register sync remote
  siftd db push alcove                   # push delta to remote""",
    )
    db_sub = p_db.add_subparsers(dest="db_command")

    # info
    p_info = db_sub.add_parser("info", help="Show database file metadata and schema info")
    p_info.set_defaults(func=cmd_db_info)

    # stats (absorbs status)
    p_stats = db_sub.add_parser("stats", help="Show database statistics")
    p_stats.add_argument("--json", action="store_true", help="Output as JSON")
    p_stats.set_defaults(func=cmd_db_stats)

    # workspaces (absorbs workspaces)
    p_workspaces = db_sub.add_parser("workspaces", help="List workspaces with conversation counts")
    p_workspaces.add_argument("--json", action="store_true", help="Output as JSON")
    p_workspaces.add_argument("-n", "--limit", type=int, default=0, help="Max workspaces (0 = all)")
    p_workspaces.set_defaults(func=cmd_db_workspaces)

    # path (absorbs path)
    p_path = db_sub.add_parser("path", help="Show XDG paths")
    p_path.set_defaults(func=cmd_db_path)

    # vacuum
    p_vacuum = db_sub.add_parser("vacuum", help="Compact database and optimize indexes")
    p_vacuum.set_defaults(func=cmd_db_vacuum)

    # backup
    p_backup = db_sub.add_parser("backup", help="Create a consistent online backup")
    p_backup.add_argument("output", help="Output file path")
    p_backup.add_argument("--force", action="store_true", help="Overwrite existing file")
    p_backup.set_defaults(func=cmd_db_backup)

    # restore
    p_restore = db_sub.add_parser("restore", help="Restore database from a backup file")
    p_restore.add_argument("input", help="Backup file path")
    p_restore.add_argument("--force", action="store_true", help="Overwrite existing database")
    p_restore.set_defaults(func=cmd_db_restore)

    # slice
    p_slice = db_sub.add_parser(
        "slice",
        help="Export filtered conversations into a standalone database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db slice out.db                          # copy all conversations
  siftd db slice out.db -w project --since 7d    # filter by workspace and date
  siftd db slice out.db -l research:auth         # filter by tag
  siftd db slice out.db --no-fts                 # skip FTS5 rebuild""",
    )
    p_slice.add_argument("output", help="Output database path")
    p_slice.add_argument("--force", action="store_true", help="Overwrite existing file")
    p_slice.add_argument("--no-fts", action="store_true", help="Skip FTS5 index rebuild in output")

    from siftd.cli_filters import add_filter_args

    add_filter_args(p_slice)
    p_slice.set_defaults(func=cmd_db_slice)

    # merge
    p_merge = db_sub.add_parser(
        "merge",
        help="Merge an external database (slice) into the main database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db merge laptop-slice.db              # merge slice into main DB
  siftd db merge laptop-slice.db --dry-run    # preview what would be merged
  siftd db merge laptop-slice.db --no-fts     # skip FTS5 rebuild""",
    )
    p_merge.add_argument("input", help="Source database path to merge in")
    p_merge.add_argument("--dry-run", action="store_true", help="Preview merge without modifying database")
    p_merge.add_argument("--no-fts", action="store_true", help="Skip FTS5 index rebuild")
    p_merge.add_argument("--no-replace", action="store_true",
                         help="Keep existing conversations instead of replacing with newer versions")
    p_merge.set_defaults(func=cmd_db_merge)

    # receive
    p_receive = db_sub.add_parser(
        "receive",
        help="Receive a database from stdin and create-or-merge into the local database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Designed for SSH pipe usage — reads a slice DB from stdin and
creates or merges it into the target database.

examples:
  ssh host siftd --db /path/team.db db receive < slice.db
  ssh host siftd --db /path/team.db db receive --no-fts < slice.db""",
    )
    p_receive.add_argument("--no-fts", action="store_true", help="Skip FTS5 index rebuild")
    p_receive.set_defaults(func=cmd_db_receive)

    # remote (sub-namespace)
    p_remote = db_sub.add_parser(
        "remote",
        help="Manage sync remotes (add, list, remove)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db remote add alcove alcove:/data/team.db   # SSH remote
  siftd db remote add nas /mnt/nas/siftd/team.db    # local path
  siftd db remote list                               # show all remotes
  siftd db remote remove alcove                      # unregister""",
    )
    remote_sub = p_remote.add_subparsers(dest="remote_command")

    p_remote_add = remote_sub.add_parser("add", help="Register a sync remote")
    p_remote_add.add_argument("name", help="Remote name")
    p_remote_add.add_argument("target", help="host:path (SSH) or /local/path")
    p_remote_add.set_defaults(func=cmd_db_remote_add)

    p_remote_list = remote_sub.add_parser("list", help="List sync remotes")
    p_remote_list.set_defaults(func=cmd_db_remote_list)

    p_remote_remove = remote_sub.add_parser("remove", help="Unregister a sync remote")
    p_remote_remove.add_argument("name", help="Remote name to remove")
    p_remote_remove.set_defaults(func=cmd_db_remote_remove)

    p_remote.set_defaults(func=lambda args: (p_remote.print_help(), 0)[1])

    # push
    p_push = db_sub.add_parser(
        "push",
        help="Push conversations to a sync remote",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db push alcove                       # push delta since last push
  siftd db push alcove --since 7d            # push last 7 days
  siftd db push alcove --all                 # push everything
  siftd db push alcove --dry-run             # preview what would push
  siftd db push alcove -w project            # filter by workspace""",
    )
    p_push.add_argument("name", help="Remote name to push to")
    p_push.add_argument("--since", metavar="DATE", type=parse_date,
                        help="Push conversations after this date (YYYY-MM-DD, 7d, 1w, yesterday, today)")
    p_push.add_argument("--all", action="store_true", dest="push_all",
                        help="Push all conversations (ignore last_push)")
    p_push.add_argument("--dry-run", action="store_true",
                        help="Preview what would be pushed without transferring")
    p_push.add_argument("-w", "--workspace", metavar="SUBSTR",
                        help="Filter by workspace path substring")
    p_push.set_defaults(func=cmd_db_push)

    # bare 'siftd db' prints help
    p_db.set_defaults(func=lambda args: (p_db.print_help(), 0)[1])
