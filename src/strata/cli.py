"""CLI for strata - conversation log aggregator."""

import argparse
import sys
from pathlib import Path

from strata.adapters.registry import load_all_adapters, wrap_adapter_paths
from strata.ingestion import IngestStats, ingest_all
from strata.paths import data_dir, db_path, embeddings_db_path, ensure_dirs, queries_dir
from strata.storage.sqlite import (
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
    from strata.api import get_stats

    db = Path(args.db) if args.db else None

    try:
        stats = get_stats(db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'strata ingest' to create it.")
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
    from strata.paths import cache_dir, config_dir, db_path

    print(f"Data directory:   {data_dir()}")
    print(f"Config directory: {config_dir()}")
    print(f"Cache directory:  {cache_dir()}")
    print(f"Database:         {db_path()}")
    return 0


def _apply_ask_config(args) -> None:
    """Apply config defaults to args if no formatter flag is explicitly set."""
    from strata.config import get_ask_defaults

    # Check if any formatter-related flag was explicitly set
    formatter_flags = ["format", "json", "verbose", "full", "thread", "context", "conversations"]
    has_explicit_formatter = any(
        getattr(args, flag, None) not in (None, False)
        for flag in formatter_flags
    )

    if has_explicit_formatter:
        return

    # Apply config defaults
    defaults = get_ask_defaults()
    for key, value in defaults.items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)


def cmd_ask(args) -> int:
    """Semantic search over conversation content using embeddings."""
    import sqlite3 as _sqlite3

    from strata.storage.embeddings import (
        open_embeddings_db,
        search_similar,
    )

    # Apply config defaults before processing
    _apply_ask_config(args)

    db = Path(args.db) if args.db else db_path()
    embed_db = Path(args.embed_db) if args.embed_db else embeddings_db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'strata ingest' to create it.")
        return 1

    # Index or rebuild mode
    if args.index or args.rebuild:
        return _ask_build_index(db, embed_db, rebuild=args.rebuild, backend_name=args.backend, verbose=True)

    # Search mode — need a query
    query = " ".join(args.query) if args.query else ""
    if not query:
        print("Usage: strata ask <query>")
        print("       strata ask --index     (build/update index)")
        print("       strata ask --rebuild   (rebuild index from scratch)")
        return 1

    if not embed_db.exists():
        print("No embeddings index found.")
        print("Run 'strata ask --index' to build it.")
        return 1

    # Resolve backend for query embedding
    from strata.embeddings import get_backend
    try:
        backend = get_backend(preferred=args.backend, verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Compose filters: get candidate conversation IDs from main DB
    from strata.search import filter_conversations, get_active_conversation_ids
    candidate_ids = filter_conversations(
        db,
        workspace=args.workspace,
        model=args.model,
        since=args.since,
        before=args.before,
    )

    # Exclude conversations from active sessions (unless opted out)
    exclude_active_ids = set()
    if not args.no_exclude_active:
        exclude_active_ids = get_active_conversation_ids(db)
        if exclude_active_ids:
            if candidate_ids is not None:
                candidate_ids = candidate_ids - exclude_active_ids
            else:
                conn_tmp = _sqlite3.connect(db)
                conn_tmp.row_factory = _sqlite3.Row
                all_ids = {
                    row["id"]
                    for row in conn_tmp.execute("SELECT id FROM conversations").fetchall()
                }
                conn_tmp.close()
                candidate_ids = all_ids - exclude_active_ids

    # Hybrid recall: FTS5 narrows candidates, embeddings rerank
    if not args.embeddings_only:
        import sqlite3 as _sqlite3_main

        from strata.storage.sqlite import fts5_recall_conversations

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
        from strata.search import resolve_role_ids
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
        from strata.api import first_mention
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
        from strata.api import fetch_file_refs
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
    from strata.output import FormatterContext, print_refs_content, select_formatter
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

    # Tagging hint (skip for JSON output)
    if not args.json and results:
        first_id = results[0]["conversation_id"][:12]
        print(f"Tip: Tag useful results for future retrieval: strata tag {first_id} research:<topic>", file=sys.stderr)

    main_conn.close()
    return 0


def _ask_build_index(db: Path, embed_db: Path, *, rebuild: bool, backend_name: str | None, verbose: bool) -> int:
    """Build or incrementally update the embeddings index."""
    from strata.api import build_index

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
        print("Run 'strata ingest' to create it.")
        return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result["chunks_added"] == 0 and verbose:
        print(f"Index is up to date. ({result['total_chunks']} chunks)")

    return 0

def _parse_tag_args(positional: list[str]) -> tuple[str, str, str] | None:
    """Parse positional args for tag command.

    Returns (entity_type, entity_id, tag_name) or None if invalid.
    Supports:
      - <id> <tag>                    -> conversation, id, tag
      - <entity_type> <id> <tag>      -> entity_type, id, tag
    """
    if len(positional) == 2:
        # Default: conversation
        return ("conversation", positional[0], positional[1])
    elif len(positional) == 3:
        entity_type = positional[0]
        if entity_type not in ("conversation", "workspace", "tool_call"):
            return None
        return (entity_type, positional[1], positional[2])
    return None


def cmd_tag(args) -> int:
    """Apply a tag to a conversation, workspace, or tool_call."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'strata ingest' to create it.")
        return 1

    conn = open_database(db)

    # Handle --last mode
    if args.last is not None:
        if not args.positional or len(args.positional) != 1:
            print("Usage: strata tag --last N <tag>")
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
        print("Usage: strata tag <id> <tag>")
        print("       strata tag <entity_type> <id> <tag>")
        print("       strata tag --last <tag>")
        print("\nEntity types: conversation (default), workspace, tool_call")
        conn.close()
        return 1

    entity_type, entity_id, tag_name = parsed

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

    tag_id = get_or_create_tag(conn, tag_name)
    result = apply_tag(conn, entity_type, resolved_id, tag_id, commit=True)

    if result:
        print(f"Applied tag '{tag_name}' to {entity_type} {resolved_id[:12]}")
    else:
        print(f"Tag '{tag_name}' already applied to {entity_type} {resolved_id[:12]}")

    conn.close()
    return 0


def cmd_tags(args) -> int:
    """List all tags."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'strata ingest' to create it.")
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


def cmd_tools(args) -> int:
    """Show tool usage summary by category."""
    from strata.api import get_tool_tag_summary, get_tool_tags_by_workspace

    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'strata ingest' to create it.")
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
            print(str(e))
            return 1

        if not results:
            print(f"No tool calls with '{prefix}*' tags found.")
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
        print(str(e))
        return 1

    if not tags:
        print(f"No tool calls with '{prefix}*' tags found.")
        print("Run 'strata backfill --shell-tags' to categorize shell commands.")
        return 0

    total = sum(t.count for t in tags)
    print(f"Tool call tags ({prefix}*): {total} total\n")

    for tag in tags:
        # Strip prefix for display
        category = tag.name[len(prefix):] if tag.name.startswith(prefix) else tag.name
        pct = (tag.count / total) * 100 if total > 0 else 0
        print(f"  {category}: {tag.count} ({pct:.1f}%)")

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
    from strata.api import get_conversation

    db = Path(args.db) if args.db else None

    try:
        detail = get_conversation(args.conversation_id, db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'strata ingest' to create it.")
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
    if detail.tags:
        print(f"Tags: {', '.join(detail.tags)}")
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
    from strata.api import QueryError, list_query_files, run_query_file

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
        print("Run 'strata ingest' to create it.")
        return 1
    except QueryError as e:
        if "Missing variables" in str(e):
            # Extract missing vars for usage hint
            import re
            match = re.search(r"Missing variables: (.+)", str(e))
            missing = match.group(1).split(", ") if match else []
            print(f"Query '{args.sql_name}' requires variables not provided: {', '.join(missing)}")
            print(f"Usage: strata query sql {args.sql_name} " + " ".join(f"--var {v}=<value>" for v in missing))
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

    from strata.api import list_conversations

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
            tool_tag=getattr(args, "tool_tag", None),
            limit=args.count,
            oldest_first=args.oldest,
        )
    except FileNotFoundError as e:
        print(str(e))
        print("Run 'strata ingest' to create it.")
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
                "tags": c.tags,
            }
            for c in conversations
        ]
        print(json.dumps(out, indent=2))
        return 0

    # Verbose mode: full table with all columns
    if args.verbose:
        columns = ["id", "workspace", "model", "started_at", "prompts", "responses", "tokens", "cost", "tags"]
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
        ws = _fmt_workspace(c.workspace_path)
        model = c.model or ""
        started = c.started_at[:16].replace("T", " ") if c.started_at else ""
        tokens = _fmt_tokens(c.total_tokens)
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
        print(f"Total tokens: {_fmt_tokens(total_tokens)}")

    return 0


def cmd_backfill(args) -> int:
    """Backfill derived data from existing records."""
    db = Path(args.db) if args.db else db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'strata ingest' to create it.")
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


def cmd_config(args) -> int:
    """View or modify config settings."""
    from strata.config import get_config, load_config, set_config
    from strata.paths import config_file

    # strata config path
    if args.action == "path":
        print(config_file())
        return 0

    # strata config get <key>
    if args.action == "get":
        if not args.key:
            print("Usage: strata config get <key>")
            print("Example: strata config get ask.formatter")
            return 1
        value = get_config(args.key)
        if value is None:
            print(f"Key not set: {args.key}")
            return 1
        print(value)
        return 0

    # strata config set <key> <value>
    if args.action == "set":
        if not args.key or not args.value:
            print("Usage: strata config set <key> <value>")
            print("Example: strata config set ask.formatter verbose")
            return 1
        set_config(args.key, args.value)
        print(f"Set {args.key} = {args.value}")
        return 0

    # strata config (show all)
    doc = load_config()
    if not doc:
        print("No config file found.")
        print(f"Create one at: {config_file()}")
        return 0

    import tomlkit
    print(tomlkit.dumps(doc).strip())
    return 0


def cmd_adapters(args) -> int:
    """List discovered adapters."""
    from strata.api import list_adapters

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
    from strata.api import (
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
            print("Usage: strata copy adapter <name> [--force]")
            print("       strata copy adapter --all [--force]")
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
            print("Usage: strata copy query <name> [--force]")
            print("       strata copy query --all [--force]")
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


def cmd_doctor(args) -> int:
    """Run health checks and report findings."""
    from strata.api import list_checks, run_checks

    db = Path(args.db) if args.db else None
    subcommand = args.subcommand

    # strata doctor checks — list available checks
    if subcommand == "checks":
        checks = list_checks()
        print("Available checks:")
        for check in checks:
            fix_marker = " [fix]" if check.has_fix else ""
            print(f"  {check.name}{fix_marker}")
            print(f"    {check.description}")
        return 0

    # Run checks (default or specific)
    checks_to_run = [subcommand] if subcommand and subcommand != "fixes" else None
    try:
        findings = run_checks(checks=checks_to_run, db_path=db)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if not findings:
        print("No issues found.")
        return 0

    # Display findings grouped by severity
    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 3), f.check))

    icons = {"info": "i", "warning": "!", "error": "x"}
    show_fixes = subcommand == "fixes"

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

    # strata doctor fixes — show consolidated fix commands
    if show_fixes:
        fixable = [f for f in findings if f.fix_available and f.fix_command]
        if fixable:
            print("\nTo fix these issues, run:")
            seen_commands = set()
            for f in fixable:
                if f.fix_command not in seen_commands:
                    print(f"  {f.fix_command}")
                    seen_commands.add(f.fix_command)

    return 1 if error_count > 0 else 0


def _fmt_ago(seconds: float) -> str:
    """Format seconds as a human-readable 'ago' string."""
    minutes = int(seconds / 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining:
        return f"{hours}h {remaining}m ago"
    return f"{hours}h ago"


def cmd_peek(args) -> int:
    """Inspect live sessions directly from disk."""
    import json as _json
    import time

    from strata.api import (
        find_session_file,
        list_active_sessions,
        read_session_detail,
        tail_session,
    )

    # Detail mode: session ID provided
    if args.session_id:
        path = find_session_file(args.session_id)
        if path is None:
            print(f"Session not found: {args.session_id}")
            return 1

        # Tail mode
        if args.tail:
            if args.json:
                lines = tail_session(path, lines=20)
                for line in lines:
                    print(line)
            else:
                lines = tail_session(path, lines=20)
                for line in lines:
                    print(line)
            return 0

        # Detail mode
        detail = read_session_detail(path, last_n=args.last)
        if detail is None:
            print(f"Could not read session: {path}")
            return 1

        if args.json:
            out = {
                "session_id": detail.info.session_id,
                "file_path": str(detail.info.file_path),
                "workspace_path": detail.info.workspace_path,
                "workspace_name": detail.info.workspace_name,
                "model": detail.info.model,
                "started_at": detail.started_at,
                "exchange_count": detail.info.exchange_count,
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
        started = ""
        if detail.started_at:
            started = detail.started_at[11:16] if len(detail.started_at) >= 16 else detail.started_at

        print(detail.info.session_id)
        parts = []
        if ws:
            parts.append(ws)
        parts.append(model)
        if started:
            parts.append(f"started {started}")
        parts.append(f"{detail.info.exchange_count} exchanges")
        print(" \u00b7 ".join(parts))
        print()

        # Exchanges
        for ex in detail.exchanges:
            ts = ""
            if ex.timestamp and len(ex.timestamp) >= 16:
                ts = ex.timestamp[11:16]

            # Prompt
            if ex.prompt_text is not None:
                print(f"[{ts}] user")
                text = ex.prompt_text
                if len(text) > 200:
                    text = text[:200] + "..."
                for line in text.splitlines():
                    print(f"  {line}")
                print()

            # Response
            if ex.response_text is not None or ex.tool_calls:
                token_info = f"{_fmt_tokens(ex.input_tokens)} in / {_fmt_tokens(ex.output_tokens)} out"
                print(f"[{ts}] assistant ({token_info})")
                if ex.response_text:
                    text = ex.response_text
                    if len(text) > 200:
                        text = text[:200] + "..."
                    for line in text.splitlines():
                        print(f"  {line}")
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
    sessions = list_active_sessions(
        workspace=args.workspace,
        include_inactive=args.all,
    )

    if not sessions:
        print("No active sessions found.")
        return 0

    if args.json:
        out = [
            {
                "session_id": s.session_id,
                "file_path": str(s.file_path),
                "workspace_path": s.workspace_path,
                "workspace_name": s.workspace_name,
                "model": s.model,
                "last_activity": s.last_activity,
                "exchange_count": s.exchange_count,
            }
            for s in sessions
        ]
        print(_json.dumps(out, indent=2))
        return 0

    now = time.time()
    for s in sessions:
        sid = s.session_id[:8]
        ws = s.workspace_name or ""
        ago = _fmt_ago(now - s.last_activity)
        exchanges = f"{s.exchange_count} exchanges"
        model = s.model or ""
        # Shorten model name: strip date suffix if present
        if model and "-" in model:
            # e.g. "claude-opus-4-5-20251101" -> "claude-opus-4-5"
            parts = model.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
                model = parts[0]
        print(f"  {sid}  {ws:<16s} {ago:<12s} {exchanges:<16s} {model}")

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
        prog="strata",
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
  # search
  strata ask "error handling"                        # basic semantic search
  strata ask -w myproject "auth flow"                # filter by workspace
  strata ask --since 2024-06 "testing"               # filter by date

  # refine
  strata ask "design decision" --thread              # narrative: top conversations expanded
  strata ask "why we chose X" --context 2            # ±2 surrounding exchanges
  strata ask "testing approach" --role user           # just your prompts, not responses
  strata ask "event sourcing" --conversations        # rank whole conversations, not chunks
  strata ask "when first discussed Y" --first        # earliest match above threshold
  strata ask --threshold 0.7 "architecture"          # only high-relevance results

  # inspect
  strata ask -v "chunking"                           # full chunk text
  strata ask --full "chunking"                       # complete prompt+response exchange
  strata ask --refs "authelia"                       # file references + content
  strata ask --refs HANDOFF.md "setup"               # filter refs to specific file

  # save useful results for future retrieval
  strata tag 01HX... research:auth                   # bookmark a conversation
  strata tag --last research:architecture            # tag most recent conversation
  strata query -l research:auth                      # retrieve tagged conversations

  # tuning
  strata ask --embeddings-only "chunking"            # skip FTS5, pure embeddings
  strata ask --recall 200 "error"                    # widen FTS5 candidate pool
  strata ask --chrono "chunking"                     # sort by time instead of score""",
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
    p_ask.add_argument("--no-exclude-active", action="store_true", help="Include results from active sessions (excluded by default)")
    p_ask.set_defaults(func=cmd_ask)

    # tag
    p_tag = subparsers.add_parser(
        "tag",
        help="Apply a tag to a conversation (or other entity)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  strata tag 01HX... important       # tag conversation (default)
  strata tag --last important        # tag most recent conversation
  strata tag --last 3 review         # tag 3 most recent conversations
  strata tag workspace 01HY... proj  # explicit entity type
  strata tag tool_call 01HZ... slow  # tag a tool call""",
    )
    p_tag.add_argument("positional", nargs="*", help="[entity_type] entity_id tag")
    p_tag.add_argument("-n", "--last", type=int, metavar="N", help="Tag N most recent conversations")
    p_tag.set_defaults(func=cmd_tag)

    # tags
    p_tags = subparsers.add_parser("tags", help="List all tags")
    p_tags.set_defaults(func=cmd_tags)

    # tools
    p_tools = subparsers.add_parser(
        "tools",
        help="Summarize tool usage by category",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  strata tools                    # shell command categories summary
  strata tools --by-workspace     # breakdown by workspace
  strata tools --prefix shell:    # filter by tag prefix""",
    )
    p_tools.add_argument("--by-workspace", action="store_true", help="Show breakdown by workspace")
    p_tools.add_argument("--prefix", metavar="PREFIX", help="Tag prefix to filter (default: shell:)")
    p_tools.add_argument("-n", "--limit", type=int, default=20, help="Max workspaces for --by-workspace (default: 20)")
    p_tools.set_defaults(func=cmd_tools)

    # query
    p_query = subparsers.add_parser(
        "query",
        help="List conversations with filters, or run SQL queries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  strata query                         # list recent conversations
  strata query -w myproject            # filter by workspace
  strata query -s "error handling"     # FTS5 search
  strata query --tool-tag shell:test   # conversations with test commands
  strata query -w proj --tool-tag shell:vcs  # combine filters
  strata query <id>                    # show conversation detail
  strata query sql                     # list available .sql files
  strata query sql cost                # run the 'cost' query
  strata query sql cost --var ws=proj  # run with variable substitution""",
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
    p_query.add_argument("-l", "--tag", metavar="NAME", help="Filter by conversation tag")
    p_query.add_argument("--tool-tag", metavar="NAME", help="Filter by tool call tag (e.g. shell:test)")
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

    # config
    p_config = subparsers.add_parser(
        "config",
        help="View or modify config settings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  strata config                        # show all config
  strata config path                   # show config file path
  strata config get ask.formatter      # get specific value
  strata config set ask.formatter verbose  # set value""",
    )
    p_config.add_argument("action", nargs="?", choices=["get", "set", "path"], help="Action to perform")
    p_config.add_argument("key", nargs="?", help="Config key (dotted path, e.g., ask.formatter)")
    p_config.add_argument("value", nargs="?", help="Value to set (for 'set' action)")
    p_config.set_defaults(func=cmd_config)

    # adapters
    p_adapters = subparsers.add_parser("adapters", help="List discovered adapters")
    p_adapters.set_defaults(func=cmd_adapters)

    # copy
    p_copy = subparsers.add_parser(
        "copy",
        help="Copy built-in resources for customization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  strata copy adapter claude_code    # copy adapter to ~/.config/strata/adapters/
  strata copy adapter --all          # copy all built-in adapters
  strata copy query cost             # copy query to ~/.config/strata/queries/""",
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
  strata doctor                    # run all checks
  strata doctor checks             # list available checks
  strata doctor fixes              # show fix commands for issues
  strata doctor ingest-pending     # run specific check""",
    )
    p_doctor.add_argument("subcommand", nargs="?", help="'checks' to list, 'fixes' to show fixes, or check name")
    p_doctor.set_defaults(func=cmd_doctor)

    # peek
    p_peek = subparsers.add_parser(
        "peek",
        help="Inspect live sessions from disk (bypasses SQLite)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  strata peek                    # list active sessions (last 2 hours)
  strata peek --all              # list all sessions
  strata peek -w myproject        # filter by workspace name
  strata peek c520f862           # detail view for session
  strata peek c520 --last 10     # show last 10 exchanges
  strata peek c520 --tail        # raw JSONL tail""",
    )
    p_peek.add_argument("session_id", nargs="?", help="Session ID prefix for detail view")
    p_peek.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace name substring")
    p_peek.add_argument("--all", action="store_true", help="Include inactive sessions (not just last 2 hours)")
    p_peek.add_argument("--last", type=int, default=5, metavar="N", help="Number of exchanges to show (default: 5)")
    p_peek.add_argument("--tail", action="store_true", help="Raw JSONL tail (last 20 lines)")
    p_peek.add_argument("--json", action="store_true", help="Output as structured JSON")
    p_peek.set_defaults(func=cmd_peek)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
