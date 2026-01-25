"""CLI for tbd - conversation log aggregator."""

import argparse
import sys
from pathlib import Path

from tbd.adapters.registry import load_all_adapters
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
        from tbd.domain import Source
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
        print("Run 'tbd ingest' to create it.")
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
    candidate_ids = _ask_filter_conversations(db, args)

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
        role_source_ids = _ask_resolve_role_ids(db, args.role, candidate_ids)
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
        results = _ask_first_mention(db, results, threshold=0.65)
        if not results:
            print(f"No results above relevance threshold for: {query}")
            return 0

    # Post-processing: --conversations (aggregate per conversation)
    if args.conversations:
        main_conn = _sqlite3.connect(db)
        main_conn.row_factory = _sqlite3.Row
        _print_conversation_results(main_conn, results, query, limit=args.limit)
        main_conn.close()
        return 0

    # Trim to requested limit after post-processing
    results = results[:args.limit]

    # Enrich results with metadata from main DB
    main_conn = _sqlite3.connect(db)
    main_conn.row_factory = _sqlite3.Row

    # Enrich results with file refs (skip for --conversations mode)
    if not args.conversations:
        all_source_ids = []
        for r in results:
            all_source_ids.extend(r.get("source_ids") or [])
        if all_source_ids:
            refs_by_prompt = _fetch_file_refs(main_conn, all_source_ids)
            for r in results:
                r_refs = []
                for sid in (r.get("source_ids") or []):
                    r_refs.extend(refs_by_prompt.get(sid, []))
                r["file_refs"] = r_refs

    if args.thread:
        _print_thread_results(main_conn, results, query)
    else:
        _print_ask_results(main_conn, results, query, args=args)

    # --refs content dump
    if args.refs and not args.conversations:
        all_refs = []
        for r in results:
            all_refs.extend(r.get("file_refs") or [])
        filter_basenames = None
        if isinstance(args.refs, str):
            filter_basenames = [b.strip() for b in args.refs.split(",") if b.strip()]
        _print_refs_content(all_refs, filter_basenames)

    main_conn.close()
    return 0


def _ask_build_index(db: Path, embed_db: Path, *, rebuild: bool, backend_name: str | None, verbose: bool) -> int:
    """Build or incrementally update the embeddings index."""
    import sqlite3 as _sqlite3

    from tbd.embeddings import get_backend
    from tbd.embeddings.chunker import extract_exchange_window_chunks
    from tbd.storage.embeddings import (
        chunk_count,
        clear_all,
        get_indexed_conversation_ids,
        open_embeddings_db,
        set_meta,
        store_chunk,
    )

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


def _ask_resolve_role_ids(db: Path, role: str, candidate_ids: set[str] | None) -> set[str] | None:
    """Resolve source IDs for a given role.

    For 'user': returns prompt IDs (prompts are user messages).
    For 'assistant': returns prompt IDs whose responses contain assistant content
    (chunks reference the prompt_id that triggered the response).
    """
    import sqlite3 as _sqlite3

    if candidate_ids is not None and not candidate_ids:
        return None

    conn = _sqlite3.connect(db)
    conn.row_factory = _sqlite3.Row

    if candidate_ids is not None:
        placeholders = ",".join("?" * len(candidate_ids))
        conv_filter = f"AND p.conversation_id IN ({placeholders})"
        params = list(candidate_ids)
    else:
        conv_filter = ""
        params = []

    if role == "user":
        # Prompts are user messages — return their IDs directly
        rows = conn.execute(
            f"SELECT p.id FROM prompts p WHERE 1=1 {conv_filter}", params
        ).fetchall()
    else:
        # 'assistant': return prompt IDs that have responses (assistant replied)
        rows = conn.execute(
            f"""SELECT DISTINCT r.prompt_id AS id
                FROM responses r
                JOIN prompts p ON p.id = r.prompt_id
                WHERE 1=1 {conv_filter}""",
            params,
        ).fetchall()

    conn.close()
    return {row["id"] for row in rows} if rows else None


def _ask_first_mention(db: Path, results: list[dict], *, threshold: float = 0.65) -> list[dict]:
    """Return the chronologically earliest result above the relevance threshold."""
    import sqlite3 as _sqlite3

    # Filter to results above threshold
    above = [r for r in results if r["score"] >= threshold]
    if not above:
        return []

    # Get timestamps for conversations
    conv_ids = list({r["conversation_id"] for r in above})
    conn = _sqlite3.connect(db)
    conn.row_factory = _sqlite3.Row
    placeholders = ",".join("?" * len(conv_ids))
    rows = conn.execute(
        f"SELECT id, started_at FROM conversations WHERE id IN ({placeholders})",
        conv_ids,
    ).fetchall()
    conn.close()

    conv_times = {row["id"]: row["started_at"] or "" for row in rows}

    # Sort by conversation start time, then by chunk_id (ULID = time-ordered)
    above.sort(key=lambda r: (conv_times.get(r["conversation_id"], ""), r["chunk_id"]))

    # Return just the earliest match
    return [above[0]]


def _strip_line_numbers(text: str) -> str:
    """Remove line number prefixes from Read tool output (e.g. '     1→' or '   123→')."""
    import re
    return re.sub(r"^\s*\d+\u2192", "", text, flags=re.MULTILINE)


def _extract_file_content(result_json: str | None) -> str | None:
    """Parse tool_call.result JSON and return clean file text."""
    import json

    if not result_json:
        return None

    try:
        result = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(result, dict):
        return None

    content = result.get("content") or result.get("output")
    if content is None:
        return None

    # Content might be a string or a list of content blocks
    if isinstance(content, str):
        return _strip_line_numbers(content)
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return _strip_line_numbers("\n".join(parts)) if parts else None

    return None


def _fetch_file_refs(conn, source_ids: list[str]) -> dict[str, list[dict]]:
    """Batch query: prompt_ids → {prompt_id: [{path, op, content}, ...]}."""
    if not source_ids:
        return {}

    import json

    placeholders = ",".join("?" * len(source_ids))
    rows = conn.execute(f"""
        SELECT r.prompt_id, t.name AS tool_name,
               tc.input AS input_json,
               tc.result AS result_json
        FROM tool_calls tc
        JOIN responses r ON r.id = tc.response_id
        JOIN tools t ON t.id = tc.tool_id
        WHERE r.prompt_id IN ({placeholders})
          AND t.name IN ('file.read', 'file.write', 'file.edit')
        ORDER BY tc.timestamp
    """, source_ids).fetchall()

    refs_by_prompt: dict[str, list[dict]] = {}
    for row in rows:
        try:
            input_data = json.loads(row["input_json"]) if row["input_json"] else {}
        except (json.JSONDecodeError, TypeError):
            input_data = {}

        path = input_data.get("file_path")
        if not path:
            continue

        op_map = {"file.read": "r", "file.write": "w", "file.edit": "e"}
        op = op_map.get(row["tool_name"], "?")

        refs_by_prompt.setdefault(row["prompt_id"], []).append({
            "path": path,
            "basename": Path(path).name,
            "op": op,
            "content": _extract_file_content(row["result_json"]),
        })

    return refs_by_prompt


def _format_refs_annotation(refs: list[dict], *, max_shown: int = 5) -> str:
    """Compact one-liner: 'refs: file(r) file(w) +N more'."""
    if not refs:
        return ""

    # Deduplicate: same basename+op shown once
    seen = set()
    unique = []
    for ref in refs:
        key = (ref["basename"], ref["op"])
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    shown = unique[:max_shown]
    parts = [f"{r['basename']}({r['op']})" for r in shown]
    overflow = len(unique) - max_shown
    if overflow > 0:
        parts.append(f"+{overflow} more")

    return "refs: " + " ".join(parts)


def _print_refs_content(all_refs: list[dict], filter_basenames: list[str] | None = None) -> None:
    """Print file reference content dump section."""
    if not all_refs:
        return

    # Deduplicate by path+op (keep first occurrence for point-in-time snapshot)
    seen = set()
    unique = []
    for ref in all_refs:
        key = (ref["path"], ref["op"])
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    # Apply basename filter if provided
    if filter_basenames:
        filter_set = {b.lower() for b in filter_basenames}
        unique = [r for r in unique if r["basename"].lower() in filter_set]
        if not unique:
            names = ", ".join(filter_basenames)
            print(f"No file references matching: {names}")
            return

    op_labels = {"r": "read", "w": "write", "e": "edit"}

    print(f"\n{'─── File References ─' * 1}{'─' * 30}")
    print()

    for i, ref in enumerate(unique, 1):
        op_label = op_labels.get(ref["op"], ref["op"])
        print(f"[{i}] {ref['basename']} ({op_label})")
        print(f"    {ref['path']}")
        print("────")
        content = ref.get("content")
        if content:
            print(content)
        else:
            print("(no content available)")
        print("────")
        print()


def _print_conversation_results(conn, results: list[dict], query: str, *, limit: int = 10) -> None:
    """Aggregate chunk scores per conversation, print ranked conversations."""
    from statistics import mean as _mean

    # Group by conversation
    by_conv: dict[str, list[dict]] = {}
    for r in results:
        by_conv.setdefault(r["conversation_id"], []).append(r)

    # Score each conversation: max score, with best excerpt
    conv_scores = []
    for conv_id, chunks in by_conv.items():
        max_score = max(c["score"] for c in chunks)
        mean_score = _mean(c["score"] for c in chunks)
        best_chunk = max(chunks, key=lambda c: c["score"])
        conv_scores.append({
            "conversation_id": conv_id,
            "max_score": max_score,
            "mean_score": mean_score,
            "chunk_count": len(chunks),
            "best_excerpt": best_chunk["text"],
            "best_chunk": best_chunk,
        })

    conv_scores.sort(key=lambda x: x["max_score"], reverse=True)
    conv_scores = conv_scores[:limit]

    # Enrich with metadata
    conv_ids = [c["conversation_id"] for c in conv_scores]
    placeholders = ",".join("?" * len(conv_ids))
    meta_rows = conn.execute(f"""
        SELECT c.id, c.started_at, w.path AS workspace
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE c.id IN ({placeholders})
    """, conv_ids).fetchall()
    meta = {row["id"]: dict(row) for row in meta_rows}

    print(f"Conversations for: {query}\n")
    for c in conv_scores:
        conv_id = c["conversation_id"]
        m = meta.get(conv_id, {})
        short_id = conv_id[:12]
        workspace = m.get("workspace") or ""
        if workspace:
            workspace = Path(workspace).name
        started = (m.get("started_at") or "")[:10]
        max_s = c["max_score"]
        mean_s = c["mean_score"]
        n_chunks = c["chunk_count"]

        print(f"  {short_id}  max={max_s:.3f}  mean={mean_s:.3f}  [{n_chunks} chunks]  {started}  {workspace}")
        snippet = c["best_excerpt"][:200].replace("\n", " ")
        if len(c["best_excerpt"]) > 200:
            snippet += "..."
        print(f"    {snippet}")
        print()


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

        # File refs annotation
        file_refs = r.get("file_refs")
        if file_refs:
            annotation = _format_refs_annotation(file_refs)
            print(f"    {annotation}")

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
            print("    ---")


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


def _print_thread_results(conn, results: list[dict], query: str) -> None:
    """Two-tier thread output: narrative tier (expanded) + shortlist tier (compact)."""
    from collections import defaultdict

    # Aggregate chunk scores per conversation (max score per conversation)
    conv_scores: dict[str, float] = {}
    conv_chunks: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        cid = r["conversation_id"]
        conv_chunks[cid].append(r)
        if cid not in conv_scores or r["score"] > conv_scores[cid]:
            conv_scores[cid] = r["score"]

    # Gather metadata for conversations
    conv_ids = list(conv_scores.keys())
    placeholders = ",".join("?" * len(conv_ids))
    meta_rows = conn.execute(f"""
        SELECT c.id, c.started_at, w.path AS workspace
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE c.id IN ({placeholders})
    """, conv_ids).fetchall()
    meta = {row["id"]: dict(row) for row in meta_rows}

    # Partition: tier 1 = conversations with max_score > mean of all max_scores
    scores = list(conv_scores.values())
    mean_score = sum(scores) / len(scores) if scores else 0.0
    tier1_ids = [cid for cid, s in conv_scores.items() if s > mean_score]
    tier2_ids = [cid for cid in conv_scores if cid not in set(tier1_ids)]

    # Sort tier 1 chronologically
    tier1_ids.sort(key=lambda cid: meta.get(cid, {}).get("started_at") or "")
    # Sort tier 2 by score descending
    tier2_ids.sort(key=lambda cid: conv_scores[cid], reverse=True)

    print(f"Results for: {query}\n")

    # --- Tier 1: Narrative thread ---
    for cid in tier1_ids:
        m = meta.get(cid, {})
        workspace = m.get("workspace") or ""
        if workspace:
            workspace = Path(workspace).name
        started = (m.get("started_at") or "")[:10]

        print(f"\u2500\u2500\u2500 {workspace}  {started} \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")

        # Best-matching chunk for this conversation
        best = max(conv_chunks[cid], key=lambda c: c["score"])
        source_ids = best.get("source_ids", [])

        if source_ids:
            _print_thread_exchange(conn, source_ids)
        else:
            # Fallback: show chunk text with type label
            side = "[user]" if best["chunk_type"] == "prompt" else "[asst]"
            text = best["text"].strip()
            if len(text) > 600:
                text = text[:600] + "..."
            print(f"  {side} {text}")

        # File refs annotation
        file_refs = best.get("file_refs")
        if file_refs:
            annotation = _format_refs_annotation(file_refs)
            print(f"  {annotation}")

        print()

    # --- Tier 2: Compact shortlist ---
    if tier2_ids:
        print(f"  {'─' * 50}")
        print("  More results:\n")
        for cid in tier2_ids:
            m = meta.get(cid, {})
            short_id = cid[:12]
            workspace = m.get("workspace") or ""
            if workspace:
                workspace = Path(workspace).name
            started = (m.get("started_at") or "")[:10]
            score = conv_scores[cid]

            # Snippet from best chunk
            best = max(conv_chunks[cid], key=lambda c: c["score"])
            snippet = best["text"][:120].replace("\n", " ")
            if len(best["text"]) > 120:
                snippet += "..."

            # File count tag
            file_refs = best.get("file_refs", [])
            files_tag = f"  [{len(file_refs)} files]" if file_refs else ""

            print(f"  {short_id}  {score:.3f}  {workspace:20s}  {started}{files_tag}  {snippet}")
        print()


def _print_thread_exchange(conn, source_ids: list[str]) -> None:
    """Print role-labeled exchange text for tier 1 thread output."""
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
            # Truncate very long prompts sensibly
            if len(prompt_text) > 500:
                prompt_text = prompt_text[:500] + "..."
            print(f"  [user] {prompt_text}")
        if response_text:
            # Truncate very long responses
            if len(response_text) > 800:
                response_text = response_text[:800] + "..."
            print(f"  [asst] {response_text}")

    if not prompt_rows:
        print("  (no exchange text available)")


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


def _query_detail(args) -> int:
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
            # Tool calls for this response (collapse consecutive same tool+status)
            tcs = tc_by_response.get(row["id"], [])
            if tcs:
                collapsed = []
                prev_key = None
                count = 0
                for tc in tcs:
                    name = tc["tool_name"] or "unknown"
                    status = tc["status"] or "unknown"
                    key = (name, status)
                    if key == prev_key:
                        count += 1
                    else:
                        if prev_key is not None:
                            collapsed.append((prev_key[0], prev_key[1], count))
                        prev_key = key
                        count = 1
                if prev_key is not None:
                    collapsed.append((prev_key[0], prev_key[1], count))
                for name, status, n in collapsed:
                    if n > 1:
                        print(f"  \u2192 {name} \u00d7{n} ({status})")
                    else:
                        print(f"  \u2192 {name} ({status})")
            print()

    conn.close()
    return 0


def _query_sql(args) -> int:
    """List or run .sql query files (formerly 'queries' command)."""
    from string import Template

    qdir = queries_dir()

    # List mode: no name provided
    if not args.sql_name:
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
    sql_file = qdir / f"{args.sql_name}.sql"
    if not sql_file.exists():
        print(f"Query not found: {sql_file}")
        print("Available queries:")
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
        print(f"Query '{args.sql_name}' requires variables not provided: {', '.join(missing)}")
        print(f"Usage: tbd query sql {args.sql_name} " + " ".join(f"--var {v}=<value>" for v in missing))
        return 1

    # Execute
    db = Path(args.db) if args.db else db_path()
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


def cmd_query(args) -> int:
    """List conversations with composable filters."""
    # Dispatch to sql subcommand if conversation_id is "sql"
    if args.conversation_id == "sql":
        return _query_sql(args)

    # Dispatch to detail view if conversation ID provided
    if args.conversation_id:
        return _query_detail(args)

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

    if args.tag:
        conditions.append(
            "c.id IN (SELECT ct.conversation_id FROM conversation_tags ct"
            " JOIN tags tg ON tg.id = ct.tag_id WHERE tg.name = ?)"
        )
        params.append(args.tag)

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
            ws = _fmt_workspace(row["workspace"])
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
        ws = _fmt_workspace(row["workspace"])
        model = row["model"] or ""
        started = row["started_at"][:16].replace("T", " ") if row["started_at"] else ""
        prompts = row["prompts"]
        responses = row["responses"]
        tokens = _fmt_tokens(row["tokens"])
        print(f"{cid}  {started}  {ws}  {model}  {prompts}p/{responses}r  {tokens} tok")

    # Stats summary (shown after list when --stats flag is set)
    if args.stats:
        total_convs = len(rows)
        total_prompts = sum(row["prompts"] for row in rows)
        total_responses = sum(row["responses"] for row in rows)
        total_tokens = sum(row["tokens"] for row in rows)
        print()
        print("--- Stats ---")
        print(f"Conversations: {total_convs}")
        print(f"Total prompts: {total_prompts}")
        print(f"Total responses: {total_responses}")
        print(f"Total tokens: {_fmt_tokens(total_tokens)}")

    conn.close()
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
