"""CLI handlers for meta commands (status, workspaces, path, config, adapters)."""

import argparse
import json
from pathlib import Path

from siftd.api import open_database
from siftd.paths import cache_dir, config_dir, config_file, data_dir, db_path
from siftd.storage.queries import fetch_top_workspaces


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
        print(json.dumps(out, indent=2))
        return 0

    if not rows:
        print("No workspaces found.")
        return 0

    for row in rows:
        print(f"{row['path']}  ({row['convs']} conversations)")

    return 0


def cmd_path(args) -> int:
    """Show XDG paths."""
    print(f"Data directory:   {data_dir()}")
    print(f"Config directory: {config_dir()}")
    print(f"Cache directory:  {cache_dir()}")
    print(f"Database:         {db_path()}")
    return 0


def cmd_config(args) -> int:
    """View or modify config settings."""
    from siftd.config import get_config, set_config

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


def build_meta_parser(subparsers) -> None:
    """Add 'status', 'workspaces', 'path', 'config', 'adapters' subparsers."""
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
