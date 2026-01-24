"""CLI for tbd - conversation log aggregator."""

import argparse
import sys
from pathlib import Path

from adapters.registry import load_all_adapters
from ingestion import ingest_all, IngestStats
from paths import db_path, embeddings_db_path, ensure_dirs, data_dir, queries_dir
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
            elif self._adapter.NAME == "codex_cli":
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
    adapters = load_all_adapters()
    if args.path:
        adapters = [_adapter_with_paths(a, args.path) for a in adapters]
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


def cmd_ask(args) -> int:
    """Semantic search over conversation content using embeddings."""
    import sqlite3 as _sqlite3

    from storage.embeddings import (
        open_embeddings_db,
        store_chunk,
        get_indexed_conversation_ids,
        clear_all,
        search_similar,
        set_meta,
        get_meta,
        chunk_count,
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
    from embeddings import get_backend
    try:
        backend = get_backend(preferred=args.backend, verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Compose filters: get candidate conversation IDs from main DB
    candidate_ids = _ask_filter_conversations(db, args)

    # Embed query and search
    query_embedding = backend.embed_one(query)
    embed_conn = open_embeddings_db(embed_db)
    results = search_similar(
        embed_conn,
        query_embedding,
        limit=args.limit,
        conversation_ids=candidate_ids,
    )
    embed_conn.close()

    if not results:
        print(f"No results for: {query}")
        return 0

    # Enrich results with metadata from main DB
    main_conn = _sqlite3.connect(db)
    main_conn.row_factory = _sqlite3.Row
    _print_ask_results(main_conn, results, query, args=args)
    main_conn.close()
    return 0


def _ask_build_index(db: Path, embed_db: Path, *, rebuild: bool, backend_name: str | None, verbose: bool) -> int:
    """Build or incrementally update the embeddings index."""
    import sqlite3 as _sqlite3

    from storage.embeddings import (
        open_embeddings_db,
        store_chunk,
        get_indexed_conversation_ids,
        clear_all,
        set_meta,
        chunk_count,
    )
    from embeddings import get_backend
    from embeddings.chunker import extract_exchange_window_chunks

    try:
        backend = get_backend(preferred=backend_name, verbose=verbose)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    embed_conn = open_embeddings_db(embed_db)

    if rebuild:
        if verbose:
            print("Clearing existing index...")
        clear_all(embed_conn)

    # Determine which conversations need indexing
    already_indexed = get_indexed_conversation_ids(embed_conn)

    # Get exchange-window chunks from main DB
    main_conn = _sqlite3.connect(db)
    main_conn.row_factory = _sqlite3.Row

    tokenizer = _get_tokenizer()
    target_tokens = 256
    max_tokens = 512
    overlap_tokens = 25

    chunks = extract_exchange_window_chunks(
        main_conn,
        tokenizer,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        exclude_conversation_ids=already_indexed,
    )
    main_conn.close()

    if not chunks:
        total = chunk_count(embed_conn)
        if verbose:
            print(f"Index is up to date. ({total} chunks)")
        embed_conn.close()
        return 0

    if verbose:
        print(f"Embedding {len(chunks)} new chunks...")

    # Batch embed
    texts = [c["text"] for c in chunks]
    batch_size = 64
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        all_embeddings.extend(backend.embed(batch))
        if verbose and len(texts) > batch_size:
            done = min(i + batch_size, len(texts))
            print(f"  {done}/{len(texts)}", file=sys.stderr)

    # Store with real token counts
    for chunk, embedding in zip(chunks, all_embeddings):
        store_chunk(
            embed_conn,
            conversation_id=chunk["conversation_id"],
            chunk_type=chunk["chunk_type"],
            text=chunk["text"],
            embedding=embedding,
            token_count=chunk["token_count"],
            source_ids=chunk.get("source_ids"),
        )
    embed_conn.commit()

    # Record strategy metadata
    set_meta(embed_conn, "backend", backend.name)
    set_meta(embed_conn, "dimension", str(backend.dimension))
    set_meta(embed_conn, "strategy", "exchange-window")
    set_meta(embed_conn, "target_tokens", str(target_tokens))
    set_meta(embed_conn, "max_tokens", str(max_tokens))

    total = chunk_count(embed_conn)
    if verbose:
        print(f"Done. Index has {total} chunks ({backend.name}, dim={backend.dimension}).")

    embed_conn.close()
    return 0


def _get_tokenizer():
    """Get the fastembed tokenizer for token counting."""
    from fastembed import TextEmbedding
    emb = TextEmbedding("BAAI/bge-small-en-v1.5")
    return emb.model.tokenizer


def _ask_filter_conversations(db: Path, args) -> set[str] | None:
    """Apply workspace/model/date filters and return candidate conversation IDs.

    Returns None if no filters applied (search all).
    """
    import sqlite3 as _sqlite3

    has_filter = any([
        getattr(args, "workspace", None),
        getattr(args, "model", None),
        getattr(args, "since", None),
        getattr(args, "before", None),
    ])
    if not has_filter:
        return None

    conn = _sqlite3.connect(db)
    conn.row_factory = _sqlite3.Row

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

    where = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT DISTINCT c.id
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        LEFT JOIN responses r ON r.conversation_id = c.id
        LEFT JOIN models m ON m.id = r.model_id
        {where}
    """

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {row["id"] for row in rows}


def _print_ask_results(conn, results: list[dict], query: str, *, args=None) -> None:
    """Format and print semantic search results with progressive disclosure."""
    verbose = getattr(args, "verbose", False) if args else False
    full = getattr(args, "full", False) if args else False
    context_n = getattr(args, "context", None) if args else None
    chrono = getattr(args, "chrono", False) if args else False

    # Gather metadata for conversations
    conv_ids = list({r["conversation_id"] for r in results})
    placeholders = ",".join("?" * len(conv_ids))
    meta_rows = conn.execute(f"""
        SELECT c.id, c.started_at, w.path AS workspace
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE c.id IN ({placeholders})
    """, conv_ids).fetchall()
    meta = {row["id"]: dict(row) for row in meta_rows}

    if chrono:
        results = sorted(
            results,
            key=lambda r: (meta.get(r["conversation_id"], {}).get("started_at") or "", r["chunk_id"]),
        )

    print(f"Results for: {query}\n")
    for r in results:
        conv_id = r["conversation_id"]
        m = meta.get(conv_id, {})
        short_id = conv_id[:12]
        workspace = m.get("workspace") or ""
        if workspace:
            workspace = Path(workspace).name
        started = (m.get("started_at") or "")[:10]
        side = r["chunk_type"].upper()
        score = r["score"]

        print(f"  {short_id}  {score:.3f}  [{side:8s}]  {started}  {workspace}")

        if context_n is not None:
            # --context N: show ±N exchanges around the match
            _print_context(conn, r, context_n)
        elif full:
            # --full: show complete prompt+response from main DB
            _print_full_exchange(conn, r)
        elif verbose:
            # -v: show full chunk text, no truncation
            for line in r["text"].splitlines():
                print(f"    {line}")
        else:
            # Default: truncated snippet
            snippet = r["text"][:200].replace("\n", " ")
            if len(r["text"]) > 200:
                snippet += "..."
            print(f"    {snippet}")

        print()


def _print_full_exchange(conn, result: dict) -> None:
    """Print complete prompt+response text for the source exchanges."""
    source_ids = result.get("source_ids", [])
    if not source_ids:
        # Fallback: show chunk text
        for line in result["text"].splitlines():
            print(f"    {line}")
        return

    placeholders = ",".join("?" * len(source_ids))

    # Get prompt text
    prompt_rows = conn.execute(f"""
        SELECT p.id, GROUP_CONCAT(json_extract(pc.content, '$.text'), '\n') AS text
        FROM prompts p
        JOIN prompt_content pc ON pc.prompt_id = p.id
        WHERE p.id IN ({placeholders})
          AND pc.block_type = 'text'
          AND json_extract(pc.content, '$.text') IS NOT NULL
        GROUP BY p.id
        ORDER BY p.timestamp
    """, source_ids).fetchall()

    # Get response text
    response_rows = conn.execute(f"""
        SELECT r.prompt_id, GROUP_CONCAT(json_extract(rc.content, '$.text'), '\n') AS text
        FROM responses r
        JOIN response_content rc ON rc.response_id = r.id
        WHERE r.prompt_id IN ({placeholders})
          AND rc.block_type = 'text'
          AND json_extract(rc.content, '$.text') IS NOT NULL
        GROUP BY r.id
    """, source_ids).fetchall()
    resp_by_prompt = {row[0]: row[1] for row in response_rows}

    for row in prompt_rows:
        prompt_text = (row[1] or "").strip()
        response_text = (resp_by_prompt.get(row[0]) or "").strip()
        if prompt_text:
            print(f"    > {prompt_text.splitlines()[0]}")
            for line in prompt_text.splitlines()[1:]:
                print(f"    > {line}")
        if response_text:
            for line in response_text.splitlines():
                print(f"    {line}")
        if prompt_text or response_text:
            print(f"    ---")


def _print_context(conn, result: dict, n: int) -> None:
    """Print ±N exchanges around the matched source exchanges."""
    source_ids = result.get("source_ids", [])
    conv_id = result["conversation_id"]

    if not source_ids:
        for line in result["text"].splitlines():
            print(f"    {line}")
        return

    # Get all prompts in this conversation, ordered by timestamp
    all_prompts = conn.execute("""
        SELECT p.id, p.timestamp
        FROM prompts p
        WHERE p.conversation_id = ?
        ORDER BY p.timestamp
    """, (conv_id,)).fetchall()

    prompt_order = [row[0] for row in all_prompts]

    # Find the index range of source prompts
    source_set = set(source_ids)
    source_indices = [i for i, pid in enumerate(prompt_order) if pid in source_set]
    if not source_indices:
        for line in result["text"].splitlines():
            print(f"    {line}")
        return

    start = max(0, min(source_indices) - n)
    end = min(len(prompt_order), max(source_indices) + n + 1)
    context_ids = prompt_order[start:end]

    placeholders = ",".join("?" * len(context_ids))

    # Get prompt text
    prompt_rows = conn.execute(f"""
        SELECT p.id, GROUP_CONCAT(json_extract(pc.content, '$.text'), '\n') AS text
        FROM prompts p
        JOIN prompt_content pc ON pc.prompt_id = p.id
        WHERE p.id IN ({placeholders})
          AND pc.block_type = 'text'
          AND json_extract(pc.content, '$.text') IS NOT NULL
        GROUP BY p.id
        ORDER BY p.timestamp
    """, context_ids).fetchall()

    # Get response text
    response_rows = conn.execute(f"""
        SELECT r.prompt_id, GROUP_CONCAT(json_extract(rc.content, '$.text'), '\n') AS text
        FROM responses r
        JOIN response_content rc ON rc.response_id = r.id
        WHERE r.prompt_id IN ({placeholders})
          AND rc.block_type = 'text'
          AND json_extract(rc.content, '$.text') IS NOT NULL
        GROUP BY r.id
    """, context_ids).fetchall()
    resp_by_prompt = {row[0]: row[1] for row in response_rows}

    for row in prompt_rows:
        pid = row[0]
        marker = ">>>" if pid in source_set else "   "
        prompt_text = (row[1] or "").strip()
        response_text = (resp_by_prompt.get(pid) or "").strip()
        if prompt_text:
            print(f"    {marker} > {prompt_text.splitlines()[0]}")
            for line in prompt_text.splitlines()[1:]:
                print(f"    {marker} > {line}")
        if response_text:
            for line in response_text.splitlines():
                print(f"    {marker} {line}")
        print(f"    {marker} ---")


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


def _fmt_tokens(n: int) -> str:
    """Format token count: 1234 -> '1.2k', 12345 -> '12.3k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _extract_text(raw: str) -> str:
    """Extract plain text from a content block (may be JSON-wrapped)."""
    import json
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "text" in obj:
            return obj["text"]
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


def _logs_detail(args) -> int:
    """Show conversation detail timeline."""
    import sqlite3

    db = Path(args.db) if args.db else db_path()
    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'tbd ingest' to create it.")
        return 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cid = args.conversation_id

    # Find conversation (support prefix match)
    conv = conn.execute(
        "SELECT c.id, c.started_at, w.path AS workspace "
        "FROM conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id "
        "WHERE c.id = ? OR c.id LIKE ?",
        (cid, f"{cid}%"),
    ).fetchone()
    if not conv:
        print(f"Conversation not found: {cid}")
        conn.close()
        return 1

    conv_id = conv["id"]

    # Header: model and total tokens
    model_row = conn.execute(
        "SELECT m.name FROM responses r "
        "LEFT JOIN models m ON m.id = r.model_id "
        "WHERE r.conversation_id = ? "
        "GROUP BY m.name ORDER BY COUNT(*) DESC LIMIT 1",
        (conv_id,),
    ).fetchone()
    model_name = model_row["name"] if model_row else "unknown"

    totals = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS input_tok, "
        "COALESCE(SUM(output_tokens), 0) AS output_tok "
        "FROM responses WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    total_input = totals["input_tok"]
    total_output = totals["output_tok"]
    total_tokens = total_input + total_output

    ws_name = Path(conv["workspace"]).name if conv["workspace"] else ""
    started = conv["started_at"][:16].replace("T", " ") if conv["started_at"] else ""

    print(f"Conversation: {conv_id}")
    if ws_name:
        print(f"Workspace: {ws_name}")
    print(f"Started: {started}")
    print(f"Model: {model_name}")
    print(f"Tokens: {_fmt_tokens(total_tokens)} (input: {_fmt_tokens(total_input)} / output: {_fmt_tokens(total_output)})")
    print()

    # Fetch prompts
    prompts = conn.execute(
        "SELECT id, timestamp FROM prompts WHERE conversation_id = ? ORDER BY timestamp",
        (conv_id,),
    ).fetchall()

    # Fetch prompt text content
    prompt_texts: dict = {}
    for p in prompts:
        blocks = conn.execute(
            "SELECT content FROM prompt_content "
            "WHERE prompt_id = ? AND block_type = 'text' ORDER BY block_index",
            (p["id"],),
        ).fetchall()
        parts = [_extract_text(b["content"]) for b in blocks]
        prompt_texts[p["id"]] = " ".join(parts).strip()

    # Fetch responses
    responses = conn.execute(
        "SELECT id, prompt_id, timestamp, input_tokens, output_tokens "
        "FROM responses WHERE conversation_id = ? ORDER BY timestamp",
        (conv_id,),
    ).fetchall()

    # Fetch response text content
    response_texts: dict = {}
    for r in responses:
        blocks = conn.execute(
            "SELECT content FROM response_content "
            "WHERE response_id = ? AND block_type = 'text' ORDER BY block_index",
            (r["id"],),
        ).fetchall()
        parts = [_extract_text(b["content"]) for b in blocks]
        response_texts[r["id"]] = " ".join(parts).strip()

    # Fetch tool calls grouped by response
    tool_calls = conn.execute(
        "SELECT tc.response_id, t.name AS tool_name, tc.status "
        "FROM tool_calls tc "
        "LEFT JOIN tools t ON t.id = tc.tool_id "
        "WHERE tc.conversation_id = ? "
        "ORDER BY tc.timestamp",
        (conv_id,),
    ).fetchall()

    # Group tool calls by response_id
    tc_by_response: dict = {}
    for tc in tool_calls:
        tc_by_response.setdefault(tc["response_id"], []).append(tc)

    # Build timeline: interleave prompts and responses chronologically
    events = []
    for p in prompts:
        events.append(("prompt", p))
    for r in responses:
        events.append(("response", r))
    events.sort(key=lambda e: e[1]["timestamp"] or "")

    for kind, row in events:
        ts = row["timestamp"][11:16] if row["timestamp"] and len(row["timestamp"]) >= 16 else ""

        if kind == "prompt":
            text = prompt_texts.get(row["id"], "")
            if len(text) > 200:
                text = text[:200] + "..."
            print(f"[prompt] {ts}")
            if text:
                print(f"  {text}")
            print()
        else:
            input_tok = row["input_tokens"] or 0
            output_tok = row["output_tokens"] or 0
            text = response_texts.get(row["id"], "")
            if len(text) > 200:
                text = text[:200] + "..."
            print(f"[response] {ts} ({_fmt_tokens(input_tok)} in / {_fmt_tokens(output_tok)} out)")
            if text:
                print(f"  {text}")
            # Tool calls for this response
            tcs = tc_by_response.get(row["id"], [])
            for tc in tcs:
                name = tc["tool_name"] or "unknown"
                status = tc["status"] or "unknown"
                print(f"  \u2192 {name} ({status})")
            print()

    conn.close()
    return 0


def cmd_logs(args) -> int:
    """List conversations with composable filters."""
    # Dispatch to detail view if conversation ID provided
    if args.conversation_id:
        return _logs_detail(args)

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

    if args.search:
        conditions.append(
            "c.id IN (SELECT conversation_id FROM content_fts WHERE content_fts MATCH ?)"
        )
        params.append(args.search)

    if args.tool:
        conditions.append(
            "c.id IN (SELECT tc.conversation_id FROM tool_calls tc"
            " JOIN tools t ON t.id = tc.tool_id WHERE t.name = ?)"
        )
        params.append(args.tool)

    if args.label:
        conditions.append(
            "c.id IN (SELECT cl.conversation_id FROM conversation_labels cl"
            " JOIN labels lb ON lb.id = cl.label_id WHERE lb.name = ?)"
        )
        params.append(args.label)

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
            c.id AS conversation_id,
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
        if not args.json:
            print("No conversations found.")
        else:
            print("[]")
        conn.close()
        return 0

    # JSON output
    if args.json:
        import json
        out = []
        for row in rows:
            out.append({
                "id": row["conversation_id"],
                "workspace": row["workspace"],
                "model": row["model"],
                "started_at": row["started_at"],
                "prompts": row["prompts"],
                "responses": row["responses"],
                "tokens": row["tokens"],
                "cost": row["cost"],
            })
        print(json.dumps(out, indent=2))
        conn.close()
        return 0

    # Verbose mode: full table with all columns
    if args.verbose:
        columns = ["id", "workspace", "model", "started_at", "prompts", "responses", "tokens", "cost"]
        str_rows = []
        for row in rows:
            cid = row["conversation_id"][:12] if row["conversation_id"] else ""
            ws = Path(row["workspace"]).name if row["workspace"] else ""
            model = row["model"] or ""
            started = row["started_at"][:16].replace("T", " ") if row["started_at"] else ""
            prompts = str(row["prompts"])
            responses = str(row["responses"])
            tokens = str(row["tokens"])
            cost = f"${row['cost']:.4f}" if row["cost"] else "$0.0000"
            str_rows.append([cid, ws, model, started, prompts, responses, tokens, cost])

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

    # Default: short mode — one dense line per conversation with truncated ID
    for row in rows:
        cid = row["conversation_id"][:12] if row["conversation_id"] else ""
        ws = Path(row["workspace"]).name if row["workspace"] else ""
        model = row["model"] or ""
        started = row["started_at"][:16].replace("T", " ") if row["started_at"] else ""
        prompts = row["prompts"]
        responses = row["responses"]
        tokens = _fmt_tokens(row["tokens"])
        print(f"{cid}  {started}  {ws}  {model}  {prompts}p/{responses}r  {tokens} tok")

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

    # ask (semantic search)
    p_ask = subparsers.add_parser(
        "ask",
        help="Semantic search over conversations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tbd ask "chunking"                 # scan: snippets with scores
  tbd ask -v "chunking"              # full chunk text
  tbd ask --full "chunking"          # complete exchange from DB
  tbd ask --context 3 "chunking"     # ±3 exchanges around match
  tbd ask --chrono "chunking"        # sort by time instead of score""",
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
    p_ask.set_defaults(func=cmd_ask)

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
    p_logs.add_argument("conversation_id", nargs="?", help="Show detail for a specific conversation ID")
    p_logs.add_argument("-v", "--verbose", action="store_true", help="Full table with all columns")
    p_logs.add_argument("-n", "--count", type=int, default=10, help="Number of conversations to show (0=all, default: 10)")
    p_logs.add_argument("--latest", action="store_true", default=True, help="Sort by newest first (default)")
    p_logs.add_argument("--oldest", action="store_true", help="Sort by oldest first")
    p_logs.add_argument("-w", "--workspace", metavar="SUBSTR", help="Filter by workspace path substring")
    p_logs.add_argument("-m", "--model", metavar="NAME", help="Filter by model name")
    p_logs.add_argument("--since", metavar="DATE", help="Conversations started after this date (ISO or YYYY-MM-DD)")
    p_logs.add_argument("--before", metavar="DATE", help="Conversations started before this date")
    p_logs.add_argument("-q", "--search", metavar="QUERY", help="Full-text search (FTS5 syntax)")
    p_logs.add_argument("-t", "--tool", metavar="NAME", help="Filter by canonical tool name (e.g. shell.execute)")
    p_logs.add_argument("-l", "--label", metavar="NAME", help="Filter by label name")
    p_logs.add_argument("--json", action="store_true", help="Output as JSON array")
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
