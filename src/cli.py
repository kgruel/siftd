"""CLI for tbd - conversation log aggregator."""

import argparse
import sys
from pathlib import Path

from adapters import claude_code, gemini_cli
from ingestion import ingest_all, IngestStats
from paths import db_path, ensure_dirs, data_dir, queries_dir
from storage.sqlite import (
    create_database,
    open_database,
    rebuild_fts_index,
    search_content,
    get_or_create_label,
    apply_label,
    list_labels,
    backfill_response_attributes,
)

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
    """List or run .sql query files."""
    from string import Template

    qdir = queries_dir()

    # List mode
    if not args.name:
        files = sorted(qdir.glob("*.sql"))
        if not files:
            print(f"No queries found in {qdir}")
            return 0
        import re
        var_pattern = re.compile(r"\$\{(\w+)\}|\$(\w+)")
        for f in files:
            matches = var_pattern.findall(f.read_text())
            var_names = sorted(set(m[0] or m[1] for m in matches))
            suffix = f"  (vars: {', '.join(var_names)})" if var_names else "  (no vars)"
            print(f"{f.stem}{suffix}")
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

    # Check for unsubstituted variables
    import re
    remaining = re.findall(r"\$\{(\w+)\}|\$(\w+)", sql)
    if remaining:
        missing = sorted(set(m[0] or m[1] for m in remaining))
        print(f"Query '{args.name}' requires variables not provided: {', '.join(missing)}")
        print(f"Usage: tbd queries {args.name} " + " ".join(f"--var {v}=<value>" for v in missing))
        return 1

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


def cmd_label(args) -> int:
    """Apply a label to a conversation or workspace."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    conn = open_database(db)

    entity_type = args.entity_type
    entity_id = args.entity_id
    label_name = args.label

    # Validate entity exists
    if entity_type == "conversation":
        row = conn.execute("SELECT id FROM conversations WHERE id = ?", (entity_id,)).fetchone()
    elif entity_type == "workspace":
        row = conn.execute("SELECT id FROM workspaces WHERE id = ?", (entity_id,)).fetchone()
    else:
        print(f"Unsupported entity type: {entity_type}")
        print("Supported: conversation, workspace")
        conn.close()
        return 1

    if not row:
        print(f"{entity_type} not found: {entity_id}")
        conn.close()
        return 1

    label_id = get_or_create_label(conn, label_name)
    result = apply_label(conn, entity_type, entity_id, label_id, commit=True)

    if result:
        print(f"Applied label '{label_name}' to {entity_type} {entity_id}")
    else:
        print(f"Label '{label_name}' already applied to {entity_type} {entity_id}")

    conn.close()
    return 0


def cmd_labels(args) -> int:
    """List all labels."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    conn = open_database(db)
    labels = list_labels(conn)

    if not labels:
        print("No labels defined.")
        conn.close()
        return 0

    for label in labels:
        counts = []
        if label["conversation_count"]:
            counts.append(f"{label['conversation_count']} conversations")
        if label["workspace_count"]:
            counts.append(f"{label['workspace_count']} workspaces")
        count_str = f" ({', '.join(counts)})" if counts else ""
        desc = f" - {label['description']}" if label["description"] else ""
        print(f"  {label['name']}{desc}{count_str}")

    conn.close()
    return 0


def cmd_logs(args) -> int:
    """List conversations with composable filters."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # Check if pricing table exists
    has_pricing = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pricing'"
    ).fetchone()[0] > 0

    # Build WHERE clauses
    conditions = []
    params = []

    if args.workspace:
        conditions.append("w.path LIKE ?")
        params.append(f"%{args.workspace}%")

    if args.model:
        conditions.append("(m.raw_name LIKE ? OR m.name LIKE ?)")
        params.append(f"%{args.model}%")
        params.append(f"%{args.model}%")

    if args.since:
        conditions.append("c.started_at >= ?")
        params.append(args.since)

    if args.before:
        conditions.append("c.started_at < ?")
        params.append(args.before)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Sort direction (--oldest overrides --latest default)
    order = "ASC" if args.oldest else "DESC"

    # Limit
    limit_clause = f"LIMIT {args.count}" if args.count > 0 else ""

    cost_expr = """ROUND(SUM(
                COALESCE(r.input_tokens, 0) * COALESCE(pr.input_per_mtok, 0)
                + COALESCE(r.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
            ) / 1000000.0, 4)""" if has_pricing else "NULL"
    pricing_join = "LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id" if has_pricing else ""

    sql = f"""
        SELECT
            w.path AS workspace,
            (SELECT m2.name FROM responses r2
             LEFT JOIN models m2 ON m2.id = r2.model_id
             WHERE r2.conversation_id = c.id
             GROUP BY m2.name
             ORDER BY COUNT(*) DESC
             LIMIT 1) AS model,
            c.started_at,
            (SELECT COUNT(*) FROM prompts WHERE conversation_id = c.id) AS prompts,
            COUNT(DISTINCT r.id) AS responses,
            COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0) AS tokens,
            {cost_expr} AS cost
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        LEFT JOIN responses r ON r.conversation_id = c.id
        LEFT JOIN models m ON m.id = r.model_id
        LEFT JOIN providers pv ON pv.id = r.provider_id
        {pricing_join}
        {where}
        GROUP BY c.id
        ORDER BY c.started_at {order}
        {limit_clause}
    """

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        print(f"SQL error: {e}")
        conn.close()
        return 1

    if not rows:
        print("No conversations found.")
        conn.close()
        return 0

    # Format rows for display
    columns = ["workspace", "model", "started_at", "prompts", "responses", "tokens", "cost"]
    str_rows = []
    for row in rows:
        ws = Path(row["workspace"]).name if row["workspace"] else ""
        model = row["model"] or ""
        # Format started_at: date + time, no seconds
        started = row["started_at"][:16].replace("T", " ") if row["started_at"] else ""
        prompts = str(row["prompts"])
        responses = str(row["responses"])
        tokens = str(row["tokens"])
        cost = f"${row['cost']:.4f}" if row["cost"] else "$0.0000"
        str_rows.append([ws, model, started, prompts, responses, tokens, cost])

    # Compute column widths and print table
    widths = [len(c) for c in columns]
    for str_row in str_rows:
        for i, val in enumerate(str_row):
            widths[i] = max(widths[i], len(val))

    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    print(header)
    print("  ".join("-" * w for w in widths))
    for str_row in str_rows:
        print("  ".join(val.ljust(widths[i]) for i, val in enumerate(str_row)))

    conn.close()
    return 0


def cmd_backfill(args) -> int:
    """Backfill response attributes from raw files."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    conn = open_database(db)
    print("Backfilling response attributes (cache tokens)...")
    count = backfill_response_attributes(conn)
    print(f"Done. Inserted {count} attributes.")
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

    # label
    p_label = subparsers.add_parser("label", help="Apply a label to an entity")
    p_label.add_argument("entity_type", choices=["conversation", "workspace"], help="Entity type")
    p_label.add_argument("entity_id", help="Entity ID (ULID)")
    p_label.add_argument("label", help="Label name")
    p_label.set_defaults(func=cmd_label)

    # labels
    p_labels = subparsers.add_parser("labels", help="List all labels")
    p_labels.set_defaults(func=cmd_labels)

    # logs
    p_logs = subparsers.add_parser("logs", help="List conversations with filters")
    p_logs.add_argument("-n", "--count", type=int, default=10, help="Number of conversations to show (0=all, default: 10)")
    p_logs.add_argument("--latest", action="store_true", default=True, help="Sort by newest first (default)")
    p_logs.add_argument("--oldest", action="store_true", help="Sort by oldest first")
    p_logs.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    p_logs.add_argument("-m", "--model", metavar="NAME", help="Filter by model name")
    p_logs.add_argument("--since", metavar="DATE", help="Conversations started after this date (ISO or YYYY-MM-DD)")
    p_logs.add_argument("--before", metavar="DATE", help="Conversations started before this date")
    p_logs.set_defaults(func=cmd_logs)

    # backfill
    p_backfill = subparsers.add_parser("backfill", help="Backfill response attributes from raw files")
    p_backfill.set_defaults(func=cmd_backfill)

    # path
    p_path = subparsers.add_parser("path", help="Show XDG paths")
    p_path.set_defaults(func=cmd_path)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
