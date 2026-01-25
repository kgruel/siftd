"""CLI for tbd - conversation log aggregator."""

import argparse
import sys
from pathlib import Path

from tbd.adapters.registry import load_all_adapters, wrap_adapter_paths
from tbd.ingestion import IngestStats, ingest_all
from tbd.paths import data_dir, db_path, embeddings_db_path, ensure_dirs, queries_dir
from tbd.storage.sqlite import (
    apply_tag,
    backfill_response_attributes,
    backfill_shell_tags,
    create_database,
    get_or_create_tag,
    list_tags,
    open_database,
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

    def on_file(source, status):
        if args.verbose or status not in ("skipped", "skipped (older)"):
            name = Path(source.location).name
            print(f"  [{status}] {name}")

    # Override adapter locations if --path specified
    adapters = load_all_adapters()
    if args.path:
        adapters = [wrap_adapter_paths(a, args.path) for a in adapters]
        print(f"Scanning: {', '.join(args.path)}")

    print("\nIngesting...")
    stats = ingest_all(conn, adapters, on_file=on_file)

    _print_stats(stats)
    conn.close()
    return 0


def cmd_status(args) -> int:
    """Show database status and statistics."""
    from tbd.api import get_stats

    db = Path(args.db) if args.db else None

    try:
        stats = get_stats(db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'tbd ingest' to create it.")
        return 1

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

    return 0


def cmd_path(args) -> int:
    """Show XDG paths."""
    from tbd.paths import cache_dir, config_dir, db_path

    print(f"Data directory:   {data_dir()}")
    print(f"Config directory: {config_dir()}")
    print(f"Cache directory:  {cache_dir()}")
    print(f"Database:         {db_path()}")
    return 0


def cmd_ask(args) -> int:
    """Semantic search over conversation content using embeddings."""
    import sqlite3 as _sqlite3

    from tbd.storage.embeddings import (
        open_embeddings_db,
        search_similar,
    )

    db = Path(args.db) if args.db else db_path()
    embed_db = Path(args.embed_db) if args.embed_db else embeddings_db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    # Index or rebuild mode
    if args.index or args.rebuild:
        return _ask_build_index(db, embed_db, rebuild=args.rebuild, backend_name=args.backend, verbose=True)

    # Search mode — need a query
    query = " ".join(args.query) if args.query else ""
    if not query:
        print("Usage: tbd ask <query>")
        print("       tbd ask --index     (build/update index)")
        print("       tbd ask --rebuild   (rebuild index from scratch)")
        return 1

    if not embed_db.exists():
        print("No embeddings index found.")
        print("Run 'tbd ask --index' to build it.")
        return 1

    # Resolve backend for query embedding
    from tbd.embeddings import get_backend
    try:
        backend = get_backend(preferred=args.backend, verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Compose filters: get candidate conversation IDs from main DB
    from tbd.search import filter_conversations
    candidate_ids = filter_conversations(
        db,
        workspace=args.workspace,
        model=args.model,
        since=args.since,
        before=args.before,
    )

    # Hybrid recall: FTS5 narrows candidates, embeddings rerank
    if not args.embeddings_only:
        import sqlite3 as _sqlite3_main

        from tbd.storage.sqlite import fts5_recall_conversations

        main_conn = _sqlite3_main.connect(db)
        main_conn.row_factory = _sqlite3_main.Row
        fts5_ids, fts5_mode = fts5_recall_conversations(main_conn, query, limit=args.recall)
        main_conn.close()

        if fts5_ids:
            if candidate_ids is not None:
                intersected = fts5_ids & candidate_ids
                candidate_ids = intersected if intersected else candidate_ids
            else:
                candidate_ids = fts5_ids
        elif fts5_mode == "none":
            print("FTS5 found no matches, falling back to pure embeddings.", file=sys.stderr)

    if candidate_ids is not None and not candidate_ids:
        print("No conversations match the given filters.")
        return 0

    # Role filter: resolve allowed source IDs from main DB
    role_source_ids = None
    if args.role:
        from tbd.search import resolve_role_ids
        role_source_ids = resolve_role_ids(db, args.role, candidate_ids)
        if not role_source_ids:
            print(f"No {args.role} content found matching filters.")
            return 0

    # Embed query and search
    query_embedding = backend.embed_one(query)
    embed_conn = open_embeddings_db(embed_db)
    # Widen initial search for modes that aggregate or filter post-hoc
    search_limit = args.limit
    if args.thread:
        search_limit = max(args.limit, 40)
    elif args.first or args.conversations:
        search_limit = max(args.limit * 10, 100)
    results = search_similar(
        embed_conn,
        query_embedding,
        limit=search_limit,
        conversation_ids=candidate_ids,
        role_source_ids=role_source_ids,
    )
    embed_conn.close()

    if not results:
        print(f"No results for: {query}")
        return 0

    # Apply threshold filter if specified
    if args.threshold is not None:
        results = [r for r in results if r["score"] >= args.threshold]
        if not results:
            print(f"No results above threshold {args.threshold} for: {query}")
            return 0

    # Post-processing: --first (earliest match above threshold)
    if args.first:
        from tbd.api import first_mention
        earliest = first_mention(results, threshold=0.65, db_path=db)
        if not earliest:
            print(f"No results above relevance threshold for: {query}")
            return 0
        results = [earliest]

    # Trim to requested limit after post-processing (except --conversations which handles its own limit)
    if not args.conversations:
        results = results[:args.limit]

    # Enrich results with metadata from main DB
    main_conn = _sqlite3.connect(db)
    main_conn.row_factory = _sqlite3.Row

    # Enrich results with file refs (skip for --conversations mode)
    if not args.conversations:
        from tbd.api import fetch_file_refs
        all_source_ids = []
        for r in results:
            all_source_ids.extend(r.get("source_ids") or [])
        if all_source_ids:
            refs_by_prompt = fetch_file_refs(main_conn, all_source_ids)
            for r in results:
                r_refs = []
                for sid in (r.get("source_ids") or []):
                    r_refs.extend(refs_by_prompt.get(sid, []))
                r["file_refs"] = r_refs

    # Select and run formatter
    from tbd.output import FormatterContext, print_refs_content, select_formatter
    formatter = select_formatter(args)
    ctx = FormatterContext(query=query, results=results, conn=main_conn, args=args)
    formatter.format(ctx)

    # --refs content dump (post-processor, not part of formatter)
    if args.refs and not args.conversations:
        all_refs = []
        for r in results:
            all_refs.extend(r.get("file_refs") or [])
        filter_basenames = None
        if isinstance(args.refs, str):
            filter_basenames = [b.strip() for b in args.refs.split(",") if b.strip()]
        print_refs_content(all_refs, filter_basenames)

    main_conn.close()
    return 0


def _ask_build_index(db: Path, embed_db: Path, *, rebuild: bool, backend_name: str | None, verbose: bool) -> int:
    """Build or incrementally update the embeddings index."""
    from tbd.api import build_index

    try:
        result = build_index(
            db_path=db,
            embed_db_path=embed_db,
            rebuild=rebuild,
            backend=backend_name,
            verbose=verbose,
        )
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'tbd ingest' to create it.")
        return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result["chunks_added"] == 0 and verbose:
        print(f"Index is up to date. ({result['total_chunks']} chunks)")

    return 0

def cmd_tag(args) -> int:
    """Apply a tag to a conversation, workspace, or tool_call."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    conn = open_database(db)

    entity_type = args.entity_type
    entity_id = args.entity_id
    tag_name = args.tag

    # Validate entity exists
    if entity_type == "conversation":
        row = conn.execute("SELECT id FROM conversations WHERE id = ?", (entity_id,)).fetchone()
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

    tag_id = get_or_create_tag(conn, tag_name)
    result = apply_tag(conn, entity_type, entity_id, tag_id, commit=True)

    if result:
        print(f"Applied tag '{tag_name}' to {entity_type} {entity_id}")
    else:
        print(f"Tag '{tag_name}' already applied to {entity_type} {entity_id}")

    conn.close()
    return 0


def cmd_tags(args) -> int:
    """List all tags."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    conn = open_database(db)
    tags = list_tags(conn)

    if not tags:
        print("No tags defined.")
        conn.close()
        return 0

    for tag in tags:
        counts = []
        if tag["conversation_count"]:
            counts.append(f"{tag['conversation_count']} conversations")
        if tag["workspace_count"]:
            counts.append(f"{tag['workspace_count']} workspaces")
        if tag["tool_call_count"]:
            counts.append(f"{tag['tool_call_count']} tool_calls")
        count_str = f" ({', '.join(counts)})" if counts else ""
        desc = f" - {tag['description']}" if tag["description"] else ""
        print(f"  {tag['name']}{desc}{count_str}")

    conn.close()
    return 0


def _fmt_tokens(n: int) -> str:
    """Format token count: 1234 -> '1.2k', 12345 -> '12.3k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_workspace(path: str | None) -> str:
    """Format workspace path for display. Shows (root) for root/empty paths."""
    if not path:
        return ""
    if path == "/" or path == "":
        return "(root)"
    return Path(path).name

def _query_detail(args) -> int:
    """Show conversation detail timeline."""
    from tbd.api import get_conversation

    db = Path(args.db) if args.db else None

    try:
        detail = get_conversation(args.conversation_id, db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'tbd ingest' to create it.")
        return 1

    if not detail:
        print(f"Conversation not found: {args.conversation_id}")
        return 1

    # Header
    ws_name = Path(detail.workspace_path).name if detail.workspace_path else ""
    started = detail.started_at[:16].replace("T", " ") if detail.started_at else ""
    total_tokens = detail.total_input_tokens + detail.total_output_tokens

    print(f"Conversation: {detail.id}")
    if ws_name:
        print(f"Workspace: {ws_name}")
    print(f"Started: {started}")
    print(f"Model: {detail.model or 'unknown'}")
    print(f"Tokens: {_fmt_tokens(total_tokens)} (input: {_fmt_tokens(detail.total_input_tokens)} / output: {_fmt_tokens(detail.total_output_tokens)})")
    print()

    # Timeline
    for ex in detail.exchanges:
        ts = ex.timestamp[11:16] if ex.timestamp and len(ex.timestamp) >= 16 else ""

        # Prompt
        if ex.prompt_text:
            text = ex.prompt_text
            if len(text) > 200:
                text = text[:200] + "..."
            print(f"[prompt] {ts}")
            print(f"  {text}")
            print()

        # Response
        if ex.response_text is not None or ex.tool_calls:
            print(f"[response] {ts} ({_fmt_tokens(ex.input_tokens)} in / {_fmt_tokens(ex.output_tokens)} out)")
            if ex.response_text:
                text = ex.response_text
                if len(text) > 200:
                    text = text[:200] + "..."
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
    from tbd.api import QueryError, list_query_files, run_query_file

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
        print("Run 'tbd ingest' to create it.")
        return 1
    except QueryError as e:
        if "Missing variables" in str(e):
            # Extract missing vars for usage hint
            import re
            match = re.search(r"Missing variables: (.+)", str(e))
            missing = match.group(1).split(", ") if match else []
            print(f"Query '{args.sql_name}' requires variables not provided: {', '.join(missing)}")
            print(f"Usage: tbd query sql {args.sql_name} " + " ".join(f"--var {v}=<value>" for v in missing))
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

    from tbd.api import list_conversations

    db = Path(args.db) if args.db else None

    try:
        conversations = list_conversations(
            db_path=db,
            workspace=args.workspace,
            model=args.model,
            since=args.since,
            before=args.before,
            search=args.search,
            tool=args.tool,
            tag=args.tag,
            limit=args.count,
            oldest_first=args.oldest,
        )
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'tbd ingest' to create it.")
        return 1

    if not conversations:
        if args.json:
            print("[]")
        else:
            print("No conversations found.")
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
            }
            for c in conversations
        ]
        print(json.dumps(out, indent=2))
        return 0

    # Verbose mode: full table with all columns
    if args.verbose:
        columns = ["id", "workspace", "model", "started_at", "prompts", "responses", "tokens", "cost"]
        str_rows = []
        for c in conversations:
            cid = c.id[:12] if c.id else ""
            ws = _fmt_workspace(c.workspace_path)
            model = c.model or ""
            started = c.started_at[:16].replace("T", " ") if c.started_at else ""
            prompts = str(c.prompt_count)
            responses = str(c.response_count)
            tokens = str(c.total_tokens)
            cost = f"${c.cost:.4f}" if c.cost else "$0.0000"
            str_rows.append([cid, ws, model, started, prompts, responses, tokens, cost])

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
        ws = _fmt_workspace(c.workspace_path)
        model = c.model or ""
        started = c.started_at[:16].replace("T", " ") if c.started_at else ""
        tokens = _fmt_tokens(c.total_tokens)
        print(f"{cid}  {started}  {ws}  {model}  {c.prompt_count}p/{c.response_count}r  {tokens} tok")

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
        print(f"Total tokens: {_fmt_tokens(total_tokens)}")

    return 0


def cmd_backfill(args) -> int:
    """Backfill derived data from existing records."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
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
    else:
        # Default: backfill response attributes (original behavior)
        print("Backfilling response attributes (cache tokens)...")
        count = backfill_response_attributes(conn)
        print(f"Done. Inserted {count} attributes.")

    conn.close()
    return 0


def cmd_adapters(args) -> int:
    """List discovered adapters."""
    from tbd.api import list_adapters

    adapters = list_adapters()

    if not adapters:
        print("No adapters found.")
        return 0

    # Compute column widths
    name_width = max(len(a.name) for a in adapters)
    source_width = max(len(a.source) for a in adapters)

    # Header
    print(f"{'NAME':<{name_width}}  {'SOURCE':<{source_width}}  LOCATIONS")

    for a in adapters:
        locations = ", ".join(a.locations) if a.locations else "-"
        print(f"{a.name:<{name_width}}  {a.source:<{source_width}}  {locations}")

    return 0


def cmd_copy(args) -> int:
    """Copy built-in resources to config directory for customization."""
    from tbd.api import (
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
            print("Usage: tbd copy adapter <name> [--force]")
            print("       tbd copy adapter --all [--force]")
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
            print("Usage: tbd copy query <name> [--force]")
            print("       tbd copy query --all [--force]")
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

    # ask (semantic search)
    p_ask = subparsers.add_parser(
        "ask",
        help="Semantic search over conversations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tbd ask "chunking"                 # hybrid: FTS5 recall → embeddings rerank
  tbd ask -v "chunking"              # full chunk text
  tbd ask --full "chunking"          # complete exchange from DB
  tbd ask --context 3 "chunking"     # ±3 exchanges around match
  tbd ask --chrono "chunking"        # sort by time instead of score
  tbd ask --embeddings-only "chunking"  # skip FTS5, pure embeddings
  tbd ask --thread "chunking"         # narrative thread: top convos + shortlist
  tbd ask --recall 200 "error"       # widen FTS5 candidate pool
  tbd ask -w myproject "architecture"   # FTS5 + workspace filter
  tbd ask --role user "chunking"     # only search user prompts
  tbd ask --first "error handling"   # earliest mention above threshold
  tbd ask --conversations "testing"  # rank conversations, not chunks
  tbd ask --refs "authelia"          # show file ref annotations + content dump
  tbd ask --refs HANDOFF.md "setup"  # content dump filtered to specific file
  tbd ask --threshold 0.7 "error"    # only results with score >= 0.7""",
    )
    p_ask.add_argument("query", nargs="*", help="Natural language search query")
    p_ask.add_argument("-n", "--limit", type=int, default=10, help="Max results (default: 10)")
    p_ask.add_argument("-v", "--verbose", action="store_true", help="Show full chunk text")
    p_ask.add_argument("--full", action="store_true", help="Show complete prompt+response exchange")
    p_ask.add_argument("--context", type=int, metavar="N", help="Show ±N exchanges around match")
    p_ask.add_argument("--chrono", action="store_true", help="Sort results by time instead of score")
    p_ask.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    p_ask.add_argument("-m", "--model", metavar="NAME", help="Filter by model name")
    p_ask.add_argument("--since", metavar="DATE", help="Conversations started after this date")
    p_ask.add_argument("--before", metavar="DATE", help="Conversations started before this date")
    p_ask.add_argument("--index", action="store_true", help="Build/update embeddings index")
    p_ask.add_argument("--rebuild", action="store_true", help="Rebuild embeddings index from scratch")
    p_ask.add_argument("--backend", metavar="NAME", help="Embedding backend (ollama, fastembed)")
    p_ask.add_argument("--embed-db", metavar="PATH", help="Alternate embeddings database path")
    p_ask.add_argument("--thread", action="store_true", help="Two-tier narrative thread output: top conversations expanded, rest as shortlist")
    p_ask.add_argument("--embeddings-only", action="store_true", help="Skip FTS5 recall, use pure embeddings")
    p_ask.add_argument("--recall", type=int, default=80, metavar="N", help="FTS5 conversation recall limit (default: 80)")
    p_ask.add_argument("--role", choices=["user", "assistant"], help="Filter by source role (user prompts or assistant responses)")
    p_ask.add_argument("--first", action="store_true", help="Return chronologically earliest match above threshold")
    p_ask.add_argument("--conversations", action="store_true", help="Aggregate scores per conversation, return ranked conversations")
    p_ask.add_argument("--refs", nargs="?", const=True, metavar="FILES", help="Show file references; optionally filter by comma-separated basenames")
    p_ask.add_argument("--threshold", type=float, metavar="SCORE", help="Filter results below this relevance score (e.g., 0.7)")
    p_ask.add_argument("--json", action="store_true", help="Output as structured JSON")
    p_ask.add_argument("--format", metavar="NAME", help="Use named formatter (built-in or drop-in plugin)")
    p_ask.set_defaults(func=cmd_ask)

    # tag
    p_tag = subparsers.add_parser("tag", help="Apply a tag to an entity")
    p_tag.add_argument("entity_type", choices=["conversation", "workspace", "tool_call"], help="Entity type")
    p_tag.add_argument("entity_id", help="Entity ID (ULID)")
    p_tag.add_argument("tag", help="Tag name")
    p_tag.set_defaults(func=cmd_tag)

    # tags
    p_tags = subparsers.add_parser("tags", help="List all tags")
    p_tags.set_defaults(func=cmd_tags)

    # query
    p_query = subparsers.add_parser(
        "query",
        help="List conversations with filters, or run SQL queries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tbd query                         # list recent conversations
  tbd query -w myproject            # filter by workspace
  tbd query -s "error handling"     # FTS5 search
  tbd query <id>                    # show conversation detail
  tbd query sql                     # list available .sql files
  tbd query sql cost                # run the 'cost' query
  tbd query sql cost --var ws=proj  # run with variable substitution""",
    )
    p_query.add_argument("conversation_id", nargs="?", help="Conversation ID for detail view, or 'sql' for SQL query mode")
    p_query.add_argument("sql_name", nargs="?", help="SQL query name (when using 'sql' subcommand)")
    p_query.add_argument("-v", "--verbose", action="store_true", help="Full table with all columns")
    p_query.add_argument("-n", "--count", type=int, default=10, help="Number of conversations to show (0=all, default: 10)")
    p_query.add_argument("--latest", action="store_true", default=True, help="Sort by newest first (default)")
    p_query.add_argument("--oldest", action="store_true", help="Sort by oldest first")
    p_query.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    p_query.add_argument("-m", "--model", metavar="NAME", help="Filter by model name")
    p_query.add_argument("--since", metavar="DATE", help="Conversations started after this date (ISO or YYYY-MM-DD)")
    p_query.add_argument("--before", metavar="DATE", help="Conversations started before this date")
    p_query.add_argument("-s", "--search", metavar="QUERY", help="Full-text search (FTS5 syntax)")
    p_query.add_argument("-t", "--tool", metavar="NAME", help="Filter by canonical tool name (e.g. shell.execute)")
    p_query.add_argument("-l", "--tag", metavar="NAME", help="Filter by tag name")
    p_query.add_argument("--json", action="store_true", help="Output as JSON array")
    p_query.add_argument("--stats", action="store_true", help="Show summary totals after list")
    p_query.add_argument("--var", action="append", metavar="KEY=VALUE", help="Substitute $KEY with VALUE in SQL (for 'sql' subcommand)")
    p_query.set_defaults(func=cmd_query)

    # backfill
    p_backfill = subparsers.add_parser("backfill", help="Backfill derived data from existing records")
    p_backfill.add_argument("--shell-tags", action="store_true", help="Tag shell.execute calls with shell:* categories")
    p_backfill.set_defaults(func=cmd_backfill)

    # path
    p_path = subparsers.add_parser("path", help="Show XDG paths")
    p_path.set_defaults(func=cmd_path)

    # adapters
    p_adapters = subparsers.add_parser("adapters", help="List discovered adapters")
    p_adapters.set_defaults(func=cmd_adapters)

    # copy
    p_copy = subparsers.add_parser(
        "copy",
        help="Copy built-in resources for customization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tbd copy adapter claude_code    # copy adapter to ~/.config/tbd/adapters/
  tbd copy adapter --all          # copy all built-in adapters
  tbd copy query cost             # copy query to ~/.config/tbd/queries/""",
    )
    p_copy.add_argument("resource_type", choices=["adapter", "query"], help="Resource type to copy")
    p_copy.add_argument("name", nargs="?", help="Resource name")
    p_copy.add_argument("--all", action="store_true", help="Copy all resources of this type")
    p_copy.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_copy.set_defaults(func=cmd_copy)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
