"""CLI for tbd - conversation log aggregator."""

import argparse
import sys
from pathlib import Path

from adapters import claude_code, gemini_cli
from ingestion import ingest_all, IngestStats
from paths import db_path, ensure_dirs, data_dir, queries_dir
from storage.sqlite import create_database, open_database, rebuild_fts_index, search_content

# Available adapters
ADAPTERS = [claude_code, gemini_cli]


class _AdapterWithPaths:
    """Wrapper that overrides an adapter's DEFAULT_LOCATIONS."""

    def __init__(self, adapter, paths: list[str]):
        self._adapter = adapter
        self._paths = paths

    def __getattr__(self, name):
        if name == "DEFAULT_LOCATIONS":
            return self._paths
        return getattr(self._adapter, name)

    def discover(self):
        """Discover using overridden paths."""
        from domain import Source
        for location in self._paths:
            base = Path(location).expanduser()
            if not base.exists():
                continue
            # Use the adapter's glob pattern logic
            if self._adapter.NAME == "claude_code":
                for f in base.glob("**/*.jsonl"):
                    yield Source(kind="file", location=f)
            elif self._adapter.NAME == "gemini_cli":
                for f in base.glob("*/chats/*.json"):
                    yield Source(kind="file", location=f)


def _adapter_with_paths(adapter, paths: list[str]):
    """Create an adapter wrapper with custom paths."""
    return _AdapterWithPaths(adapter, paths)


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

    def on_file(source, status):
        if args.verbose or status not in ("skipped", "skipped (older)"):
            name = Path(source.location).name
            print(f"  [{status}] {name}")

    # Override adapter locations if --path specified
    adapters = ADAPTERS
    if args.path:
        from copy import copy
        adapters = []
        for adapter in ADAPTERS:
            # Create a wrapper that overrides DEFAULT_LOCATIONS
            adapters.append(_adapter_with_paths(adapter, args.path))
        print(f"Scanning: {', '.join(args.path)}")

    print("\nIngesting...")
    stats = ingest_all(conn, adapters, on_file=on_file)

    _print_stats(stats)
    conn.close()
    return 0


def cmd_status(args) -> int:
    """Show database status and statistics."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print(f"Run 'tbd ingest' to create it.")
        return 1

    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    print(f"Database: {db}")
    print(f"Size: {db.stat().st_size / 1024:.1f} KB")

    print("\n--- Counts ---")
    tables = [
        ("conversations", "Conversations"),
        ("prompts", "Prompts"),
        ("responses", "Responses"),
        ("tool_calls", "Tool calls"),
        ("harnesses", "Harnesses"),
        ("workspaces", "Workspaces"),
        ("tools", "Tools"),
        ("models", "Models"),
        ("ingested_files", "Ingested files"),
    ]
    for table, label in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {label}: {count}")

    print("\n--- Harnesses ---")
    for row in conn.execute("SELECT name, source, log_format FROM harnesses"):
        print(f"  {row['name']} ({row['source']}, {row['log_format']})")

    print("\n--- Workspaces (top 10) ---")
    for row in conn.execute("""
        SELECT w.path, COUNT(c.id) as convs
        FROM workspaces w
        LEFT JOIN conversations c ON c.workspace_id = w.id
        GROUP BY w.id
        ORDER BY convs DESC
        LIMIT 10
    """):
        print(f"  {row['path']}: {row['convs']} conversations")

    print("\n--- Models ---")
    for row in conn.execute("SELECT raw_name FROM models"):
        print(f"  {row['raw_name']}")

    print("\n--- Tools (top 10 by usage) ---")
    for row in conn.execute("""
        SELECT t.name, COUNT(tc.id) as uses
        FROM tools t
        JOIN tool_calls tc ON tc.tool_id = t.id
        GROUP BY t.id
        ORDER BY uses DESC
        LIMIT 10
    """):
        print(f"  {row['name']}: {row['uses']}")

    conn.close()
    return 0


def cmd_path(args) -> int:
    """Show XDG paths."""
    from paths import data_dir, config_dir, cache_dir, db_path

    print(f"Data directory:   {data_dir()}")
    print(f"Config directory: {config_dir()}")
    print(f"Cache directory:  {cache_dir()}")
    print(f"Database:         {db_path()}")
    return 0


def cmd_search(args) -> int:
    """Search conversation content using FTS5."""
    db = db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print(f"Run 'tbd ingest' to create it.")
        return 1

    conn = open_database(db)

    if args.rebuild:
        print("Rebuilding FTS index...")
        rebuild_fts_index(conn)
        print("Done.")

    query = " ".join(args.query)
    if not query:
        conn.close()
        return 0

    results = search_content(conn, query, limit=args.limit)

    if not results:
        print(f"No results for: {query}")
        conn.close()
        return 0

    print(f"Found {len(results)} result(s) for: {query}\n")
    for r in results:
        side_label = "PROMPT" if r["side"] == "prompt" else "RESPONSE"
        print(f"  [{side_label}] conversation={r['conversation_id']}")
        print(f"    {r['snippet']}")
        print()

    conn.close()
    return 0


def cmd_queries(args) -> int:
    """List or run .sql query files.

    TODO: Revisit help UX — unsubstituted $vars produce confusing SQLite errors
    ("Incorrect number of bindings"). Consider: showing required vars in listing,
    defaulting $limit to a sensible value, or better error messages on missing vars.
    """
    from string import Template

    qdir = queries_dir()

    # List mode
    if not args.name:
        files = sorted(qdir.glob("*.sql"))
        if not files:
            print(f"No queries found in {qdir}")
            return 0
        for f in files:
            print(f.stem)
        return 0

    # Run mode
    sql_file = qdir / f"{args.name}.sql"
    if not sql_file.exists():
        print(f"Query not found: {sql_file}")
        print(f"Available queries:")
        for f in sorted(qdir.glob("*.sql")):
            print(f"  {f.stem}")
        return 1

    sql = sql_file.read_text()

    # Variable substitution
    if args.var:
        variables = {}
        for v in args.var:
            if "=" not in v:
                print(f"Invalid --var format (expected key=value): {v}")
                return 1
            key, value = v.split("=", 1)
            variables[key] = value
        sql = Template(sql).safe_substitute(variables)
    else:
        sql = Template(sql).safe_substitute()

    # Execute
    db = db_path()
    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        last_rows = None
        for stmt in statements:
            cursor = conn.execute(stmt)
            if cursor.description:
                last_rows = (cursor.description, cursor.fetchall())

        if last_rows:
            desc, rows = last_rows
            columns = [d[0] for d in desc]
            # Compute column widths
            widths = [len(c) for c in columns]
            str_rows = []
            for row in rows:
                str_row = [str(v) if v is not None else "" for v in row]
                str_rows.append(str_row)
                for i, val in enumerate(str_row):
                    widths[i] = max(widths[i], len(val))
            # Print header
            header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
            print(header)
            print("  ".join("-" * w for w in widths))
            for str_row in str_rows:
                print("  ".join(val.ljust(widths[i]) for i, val in enumerate(str_row)))
        else:
            print("OK (no results)")
    except sqlite3.Error as e:
        print(f"SQL error: {e}")
        return 1
    finally:
        conn.close()

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tbd",
        description="Aggregate and query LLM conversation logs",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"Database path (default: {db_path()})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest logs from all sources")
    p_ingest.add_argument("-v", "--verbose", action="store_true", help="Show all files including skipped")
    p_ingest.add_argument("-p", "--path", action="append", metavar="DIR", help="Additional directories to scan (can be repeated)")
    p_ingest.set_defaults(func=cmd_ingest)

    # status
    p_status = subparsers.add_parser("status", help="Show database statistics")
    p_status.set_defaults(func=cmd_status)

    # search
    p_search = subparsers.add_parser("search", help="Full-text search conversation content")
    p_search.add_argument("query", nargs="*", help="Search query (FTS5 syntax)")
    p_search.add_argument("-n", "--limit", type=int, default=20, help="Max results (default: 20)")
    p_search.add_argument("--rebuild", action="store_true", help="Rebuild FTS index before searching")
    p_search.set_defaults(func=cmd_search)

    # queries
    p_queries = subparsers.add_parser("queries", help="List or run .sql query files")
    p_queries.add_argument("name", nargs="?", help="Query name to run (without .sql extension)")
    p_queries.add_argument("--var", action="append", metavar="KEY=VALUE", help="Substitute $KEY with VALUE in SQL (repeatable)")
    p_queries.set_defaults(func=cmd_queries)

    # path
    p_path = subparsers.add_parser("path", help="Show XDG paths")
    p_path.set_defaults(func=cmd_path)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
