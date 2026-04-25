"""CLI handler for 'siftd search' — unified search over conversations.

Supports three modes:
- Hybrid (default with embeddings): FTS5 recall + semantic reranking
- FTS5-only (--fts or fallback): keyword search without embeddings
- Semantic-only (--semantic): pure embeddings without FTS5 recall
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from siftd.cli._common import resolve_db
from siftd.cli._filters import extract_filter_args
from siftd.paths import embeddings_db_path


def _print_empty_json_results(args, query: str, db: Path) -> None:
    """Emit empty JSON results for --json output modes."""
    import json

    from painted import Fidelity

    from siftd.output.format_registry import select_format

    fmt = select_format(json_mode=True, is_tty=False)
    result = fmt.render_search([], Fidelity(), query=query, mode="chunks")
    if isinstance(result, dict):
        print(json.dumps(result, indent=2))
    else:
        print(result)


def _can_delegate_to_serve(args, *, db: Path, embed_db: Path) -> bool:
    """Conservatively decide whether it's safe to delegate to siftd-serve."""
    from siftd.serve.delegation import can_delegate

    if not can_delegate(db=db):
        return False

    # Embeddings DB overrides are not supported over HTTP.
    if getattr(args, "embed_db", None):
        if embed_db != embeddings_db_path():
            return False

    return True

def _chunks_from_rows(rows) -> list[Any]:
    from siftd.api.search import SearchChunk

    chunks: list[Any] = []
    for r in rows:
        if hasattr(r, "conversation_id") and hasattr(r, "score") and hasattr(r, "to_render_dict"):
            chunks.append(r)
        else:
            chunks.append(SearchChunk.from_mapping(r))
    return chunks


def _rows_from_chunks(chunks: list[Any]) -> list[dict]:
    return [c.to_render_dict() for c in chunks]


def _fetch_search_metadata(conn, results):
    """Fetch conversation metadata and enrich results in-place via API primitive."""
    from siftd.api.search import enrich_search_metadata

    chunks = _chunks_from_rows(results)
    enrich_search_metadata(conn, chunks)
    if results and isinstance(results[0], dict):
        rendered = _rows_from_chunks(chunks)
        for i, row in enumerate(rendered):
            results[i].update(row)


def _aggregate_conversations(results, *, limit=10):
    """Aggregate search results by conversation via API primitive."""
    from siftd.api.search import aggregate_by_conversation

    chunks = _chunks_from_rows(results)
    convs = aggregate_by_conversation(chunks, limit=limit)
    return [c.to_render_dict() for c in convs]


def _compute_thread_tiers(results):
    """Split results into thread tiers via API primitive."""
    from siftd.api.search import compute_thread_tiers

    chunks = _chunks_from_rows(results)
    tier1, tier2 = compute_thread_tiers(chunks)
    return _rows_from_chunks(tier1), _rows_from_chunks(tier2)


def _enrich_exchanges(conn, results):
    """Fetch full prompt+response texts via API primitive."""
    from siftd.api.search import enrich_exchanges

    chunks = _chunks_from_rows(results)
    enrich_exchanges(conn, chunks)
    if results and isinstance(results[0], dict):
        rendered = _rows_from_chunks(chunks)
        for i, row in enumerate(rendered):
            results[i].update(row)


def _enrich_context(conn, results, n):
    """Fetch +/-N surrounding exchanges via API primitive."""
    from siftd.api.search import enrich_context_window

    chunks = _chunks_from_rows(results)
    enrich_context_window(conn, chunks, n)
    if results and isinstance(results[0], dict):
        rendered = _rows_from_chunks(chunks)
        for i, row in enumerate(rendered):
            results[i].update(row)


def cmd_search(args) -> int:
    """Unified search over conversations — auto-selects FTS5 or semantic based on availability."""
    from siftd.api import embeddings_available, open_database

    db = resolve_db(args)
    embed_db = Path(args.embed_db).expanduser() if args.embed_db else embeddings_db_path()

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    # Index or rebuild mode — requires embeddings
    if args.index or args.rebuild:
        if not embeddings_available():
            print("Semantic search requires the [embed] extra.", file=sys.stderr)
            print()
            print("Install with:")
            print("  siftd install embed")
            return 1
        return _search_build_index(db, embed_db, rebuild=args.rebuild, backend_name=args.backend, verbose=True)

    # Search mode — need a query
    query = " ".join(args.query) if args.query else ""
    if not query:
        print("Usage: siftd search <query>")
        print("       siftd search --index     (build/update index)")
        print("       siftd search --rebuild   (rebuild index from scratch)")
        return 1

    # --refs with --json is not supported (refs dump would break JSON validity)
    if args.json and args.refs:
        print("Error: --refs is not supported with --json", file=sys.stderr)
        return 1

    # --thread with --json: warn and ignore (JSON formatter doesn't use thread grouping)
    if args.json and args.thread:
        print("Note: --thread is ignored with --json output", file=sys.stderr)

    # Extract standard filters once for delegation and candidate resolution
    filters = extract_filter_args(args)

    # Determine search mode: FTS5-only, semantic-only, or hybrid
    use_fts = getattr(args, "fts", False)
    use_semantic = getattr(args, "semantic", False)

    # Mutual exclusivity check
    if use_fts and use_semantic:
        print("Error: --fts and --semantic are mutually exclusive", file=sys.stderr)
        return 1

    # --fts mode: pure FTS5, no embeddings required
    if use_fts:
        return _search_fts_only(args, db, query, filters)

    # --semantic mode: force embeddings-only (no FTS5 recall), error if unavailable
    if use_semantic:
        # Force embeddings-only mode (skip FTS5 recall)
        args.embeddings_only = True

    # Determine search mode — check embeddings availability
    has_embeddings = embeddings_available() and embed_db.exists()

    if use_semantic:
        # --semantic: require embeddings, error if unavailable
        if not embeddings_available():
            print("Semantic search requires the [embed] extra.", file=sys.stderr)
            print()
            print("Install with:")
            print("  siftd install embed")
            return 1
        if not embed_db.exists():
            print("No embeddings index found.")
            print("Run 'siftd search --index' to build it.")
            return 1
        search_mode = "semantic"
    elif not has_embeddings:
        # Auto-fallback to FTS with hint
        if embeddings_available() and not embed_db.exists():
            print("[FTS5 mode - embeddings index not built: siftd search --index]", file=sys.stderr)
        else:
            print("[FTS5 mode - for semantic search: siftd install embed]", file=sys.stderr)
        search_mode = "fts"
    else:
        search_mode = "hybrid"

    # Widen limit for modes that aggregate or filter post-hoc
    widened_limit = args.limit
    if args.thread:
        widened_limit = max(args.limit, 40)
    elif args.first or args.conversations:
        widened_limit = max(args.limit * 10, 100)

    from siftd.api.dispatch import Operation, execute
    from siftd.api.search import (
        enrich_file_refs,
        filter_by_threshold,
        search_chunks,
        sort_chunks_by_time,
    )
    from siftd.cli._common import fidelity_from_args
    from siftd.serve.delegation import try_serve

    fidelity = fidelity_from_args(args)
    rerank = "mmr" if not args.no_diversity else "relevance"

    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=search_chunks,
        params={
            "q": query,
            "db_path": db,
            "embed_db": embed_db,
            "n": widened_limit,
            "mode": search_mode,
            "workspace": filters.workspace,
            "model": filters.model,
            "since": filters.since,
            "before": filters.before,
            "tag": filters.tag,
            "all_tags": filters.all_tags,
            "no_tag": filters.no_tag,
            "owner": filters.owner,
            "exclude_active": not args.no_exclude_active,
            "include_derivative": args.include_derivative,
            "recall": args.recall,
            "rerank": rerank,
            "lambda_": args.lambda_,
            "recency": args.recency,
            "recency_half_life": args.recency_half_life,
            "recency_max_boost": args.recency_max_boost,
            "backend": args.backend,
            # Serve-only: route uses embeddings_only instead of mode
            "embeddings_only": search_mode == "semantic",
            "raw_fts": getattr(args, "raw_fts", False),
        },
        render_method="search",
        fidelity=fidelity,
        db=db,
    )

    # Try serve delegation (warm caches, embeddings loaded)
    # Skip for FTS mode (serve does hybrid/semantic) and custom --embed-db
    raw_results: Any | None = None
    if search_mode != "fts" and _can_delegate_to_serve(args, db=db, embed_db=embed_db):
        raw_results = try_serve(op)

    # Local execution
    if raw_results is None:
        try:
            raw_results = execute(op)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if isinstance(raw_results, dict):
        chunks = _chunks_from_rows(raw_results.get("results", []))
    elif isinstance(raw_results, list):
        chunks = _chunks_from_rows(raw_results)
    else:
        chunks = []

    if not chunks:
        if args.json:
            _print_empty_json_results(args, query, db)
        else:
            print(f"No results for: {query}")
        return 0

    # Apply threshold filter if specified
    if args.threshold is not None:
        chunks = filter_by_threshold(chunks, threshold=args.threshold)
        if not chunks:
            if args.json:
                _print_empty_json_results(args, query, db)
            else:
                print(f"No results above threshold {args.threshold} for: {query}")
            return 0

    # Post-processing: --first (earliest match above threshold)
    if args.first:
        from siftd.api.search import first_mention
        effective_threshold = args.threshold if args.threshold is not None else 0.65
        earliest = first_mention(chunks, threshold=effective_threshold, db_path=db)
        if not earliest:
            if args.json:
                _print_empty_json_results(args, query, db)
            else:
                print(f"No results above relevance threshold for: {query}")
            return 0
        chunks = _chunks_from_rows([earliest])

    # Trim to requested limit after post-processing
    # Skip for modes that manage their own candidate pools:
    # - --conversations: aggregates per conversation, handles own limit
    # - --thread: widened pool for grouping, formatter handles presentation
    if not args.conversations and not args.thread:
        chunks = chunks[:args.limit]
    results = _rows_from_chunks(chunks)

    # Enrich results with metadata from main DB
    main_conn = open_database(db, read_only=True)

    # Enrich results with file refs (skip for --conversations mode)
    if not args.conversations:
        file_ref_chunks = _chunks_from_rows(results)
        enrich_file_refs(main_conn, file_ref_chunks)
        results = _rows_from_chunks(file_ref_chunks)

    # Privacy warning for full content display
    if args.full or args.refs:
        print("Note: Showing full content which may contain sensitive information.", file=sys.stderr)

    # Select output format and determine mode

    from siftd.output.common import print_refs_content
    from siftd.output.format_registry import select_format

    try:
        fmt = select_format(
            name=getattr(args, "format", None),
            json_mode=args.json,
            is_tty=sys.stdout.isatty(),
        )
    except ValueError as e:
        main_conn.close()
        print(f"Error: {e}", file=sys.stderr)
        return 1

    mode = "chunks"
    if args.conversations:
        mode = "conversations"
    elif args.thread:
        mode = "thread"

    try:
        # Metadata enrichment
        _fetch_search_metadata(main_conn, results)

        # Warn if --by-time is used with a mode that ignores it
        if args.by_time and mode in ("conversations", "thread"):
            print(f"Note: --by-time has no effect in {mode} mode", file=sys.stderr)

        # Sort by time if requested
        if args.by_time and mode == "chunks":
            results = _rows_from_chunks(sort_chunks_by_time(results))

        # Mode-specific data processing
        ctx_kwargs: dict = {"query": query, "mode": mode}

        if mode == "conversations":
            render_results = _aggregate_conversations(results, limit=getattr(args, "limit", 10))
        elif mode == "thread":
            # Enrich tier1 exchanges for thread display
            _enrich_exchanges(main_conn, results)
            tier1, tier2 = _compute_thread_tiers(results)
            ctx_kwargs["tier1"] = tier1
            ctx_kwargs["tier2"] = tier2
            render_results = results
        else:
            render_results = results

        # Exchange enrichment for --full
        if args.full and mode == "chunks":
            _enrich_exchanges(main_conn, results)
            render_results = results

        # Context enrichment for --context N
        context_n = getattr(args, "context", None)
        if context_n is not None and mode == "chunks":
            _enrich_context(main_conn, results, context_n)
            render_results = results

        output = fmt.render_search(render_results, op.fidelity, **ctx_kwargs)
        from siftd.output.painted_bridge import emit_output

        emit_output(output)

        # --refs content dump (post-processor, not part of formatter)
        if args.refs and not args.conversations:
            all_refs = []
            for r in render_results:
                all_refs.extend(r.get("file_refs") or [])
            filter_basenames = None
            if isinstance(args.refs, str):
                filter_basenames = [b.strip() for b in args.refs.split(",") if b.strip()]
            print_refs_content(all_refs, filter_basenames)

        # Tagging hint (skip for JSON output)
        if not args.json and render_results:
            first_id = render_results[0]["conversation_id"][:12]
            print(f"Tip: Tag useful results for future retrieval: siftd tag {first_id} research:<topic>", file=sys.stderr)
    finally:
        main_conn.close()

    return 0


def _search_fts_only(args, db: Path, query: str, filters=None) -> int:
    """FTS5-only search mode — keyword search without embeddings."""
    import sqlite3

    from painted import Fidelity

    from siftd.api import open_database
    from siftd.api.dispatch import Operation, execute
    from siftd.api.search import search_chunks
    from siftd.cli._common import fidelity_from_args

    # Warn about flags that are ignored in FTS5-only mode
    unsupported_flags = []
    if args.thread:
        unsupported_flags.append("--thread")
    if args.context:
        unsupported_flags.append("--context")
    if args.full:
        unsupported_flags.append("--full")
    if args.verbose:
        unsupported_flags.append("--verbose/-v")
    if args.conversations:
        unsupported_flags.append("--conversations")
    if args.first:
        unsupported_flags.append("--first")
    if args.refs:
        unsupported_flags.append("--refs")
    if args.by_time:
        unsupported_flags.append("--by-time")
    if args.format:
        unsupported_flags.append("--format")

    if unsupported_flags:
        flags_str = ", ".join(unsupported_flags)
        print(f"WARNING: {flags_str} ignored in FTS5 mode (requires embeddings)", file=sys.stderr)

    # Compose filters
    if filters is None:
        filters = extract_filter_args(args)

    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=search_chunks,
        params={
            "q": query,
            "db_path": db,
            "n": args.limit,
            "mode": "fts",
            "workspace": filters.workspace,
            "model": filters.model,
            "since": filters.since,
            "before": filters.before,
            "tag": filters.tag,
            "all_tags": filters.all_tags,
            "no_tag": filters.no_tag,
            "owner": filters.owner,
            "exclude_active": not args.no_exclude_active,
            "include_derivative": args.include_derivative,
            "embeddings_only": False,
            "raw_fts": getattr(args, "raw_fts", False),
        },
        render_method="search",
        fidelity=fidelity_from_args(args),
        db=db,
    )

    try:
        raw_results = execute(op)
    except sqlite3.OperationalError as e:
        err_msg = str(e).lower()
        if "no such table" in err_msg and "fts" in err_msg:
            print("FTS index not found. Run 'siftd ingest' first.", file=sys.stderr)
        elif "fts5" in err_msg or "syntax" in err_msg:
            print(f"Invalid search query: {e}", file=sys.stderr)
            print("Tip: Check your search query for syntax errors.", file=sys.stderr)
        else:
            print(f"Database error: {e}", file=sys.stderr)
        return 1

    # Limit results
    chunks = _chunks_from_rows(raw_results)[:args.limit]
    results = _rows_from_chunks(chunks)

    if not results:
        if args.json:
            import json

            out = {
                "query": query,
                "mode": "fts5",
                "results": [],
            }
            if unsupported_flags:
                out["warnings"] = [
                    f"{flag} ignored in FTS5 mode (requires embeddings)"
                    for flag in unsupported_flags
                ]
            print(json.dumps(out, indent=2))
        else:
            print(f"No results for: {query}")
        return 0

    # Enrich with metadata and render via unified formatter system
    import json as json_mod

    from siftd.output.format_registry import select_format

    conn = open_database(db, read_only=True)
    try:
        _fetch_search_metadata(conn, results)
    finally:
        conn.close()

    fidelity = Fidelity()
    fmt = select_format(
        name=getattr(args, "format", None),
        json_mode=args.json,
        is_tty=sys.stdout.isatty(),
    )

    output = fmt.render_search(results, fidelity, query=query, mode="chunks")
    if isinstance(output, dict):
        # Preserve FTS5-specific fields for JSON
        if unsupported_flags:
            output["warnings"] = [
                f"{flag} ignored in FTS5 mode (requires embeddings)"
                for flag in unsupported_flags
            ]
        output["mode"] = "fts5"
        print(json_mod.dumps(output, indent=2, default=str))
    else:
        from siftd.output.painted_bridge import emit_output

        emit_output(output)

    # Tagging hint (skip for JSON output)
    if not args.json and results:
        first_id = results[0]["conversation_id"][:12]
        print(f"Tip: Tag useful results: siftd tag {first_id} research:<topic>", file=sys.stderr)

    return 0


def _search_build_index(db: Path, embed_db: Path, *, rebuild: bool, backend_name: str | None, verbose: bool) -> int:
    """Build or incrementally update the embeddings index."""
    from siftd.api import IncrementalCompatError, build_index

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
        print("Run 'siftd ingest' to create it.")
        return 1
    except IncrementalCompatError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result["chunks_added"] == 0 and verbose:
        print(f"Index is up to date. ({result['total_chunks']} chunks)")

    return 0


def build_search_parser(subparsers) -> None:
    """Add the 'search' subparser to the CLI."""
    p_search = subparsers.add_parser(
        "search",
        help="Search conversations (auto-selects FTS5 or semantic based on what's installed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Unified search: auto-selects the best available search mechanism.
- With embeddings installed: hybrid search (FTS5 recall + semantic reranking)
- Without embeddings: FTS5 keyword search (install embeddings: siftd install embed)

examples:
  # search (auto-selects best available mode)
  siftd search "error handling"                        # hybrid or FTS5 (auto)
  siftd search -w myproject "auth flow"                # filter by workspace
  siftd search --since 2024-06 "testing"               # filter by date

  # explicit mode selection
  siftd search --fts "error handling"                  # force FTS5 keyword search
  siftd search --semantic "auth flow"                  # force semantic search

  # refine
  siftd search "design decision" --thread              # narrative: top conversations expanded
  siftd search "why we chose X" --context 2            # ±2 surrounding exchanges
  siftd search "event sourcing" --conversations        # rank whole conversations, not chunks
  siftd search "when first discussed Y" --first        # earliest match above threshold
  siftd search --threshold 0.7 "architecture"          # only high-relevance results

  # inspect
  siftd search -v "chunking"                           # full chunk text
  siftd search --full "chunking"                       # complete prompt+response exchange
  siftd search --refs "authelia"                       # file references + content
  siftd search --refs HANDOFF.md "setup"               # filter refs to specific file

  # filter by tags
  siftd search -l research:auth "auth flow"            # search within tagged conversations
  siftd search -l research: -l useful: "pattern"       # OR — any research: or useful: tag
  siftd search --all-tags important --all-tags reviewed "design"  # AND — must have both
  siftd search -l research: --no-tag archived "auth"   # combine OR + NOT

  # save useful results for future retrieval
  siftd tag 01HX... research:auth                   # bookmark a conversation
  siftd tag --last research:architecture            # tag most recent conversation
  siftd query -l research:auth                      # retrieve tagged conversations

  # tuning
  siftd search --embeddings-only "chunking"            # skip FTS5, pure embeddings
  siftd search --recall 200 "error"                    # widen FTS5 candidate pool
  siftd search --by-time "chunking"                     # sort by time instead of score

  # diversity vs relevance (MMR reranking)
  siftd search --no-diversity "chunking"               # pure relevance order (deterministic)
  siftd search --lambda 0.5 "design"                   # more diverse results (less redundancy)
  siftd search --json "auth" | jq '.results[0].breakdown'  # score component breakdown""",
    )

    # Positional argument
    p_search.add_argument("query", nargs="*", help="Natural language search query")

    # Filtering options (most commonly used)
    from siftd.cli._filters import add_filter_args

    add_filter_args(p_search)

    # Output options
    output_group = p_search.add_argument_group("output")
    output_group.add_argument("-n", "--limit", type=int, default=10, help="Max results (default: 10)")
    output_group.add_argument("-v", "--verbose", action="store_true", help="Show full chunk text")
    output_group.add_argument("--full", action="store_true", help="Show complete prompt+response exchange")
    output_group.add_argument("--context", type=int, metavar="N", help="Show ±N exchanges around match")
    output_group.add_argument("--thread", action="store_true", help="Narrative thread: top conversations expanded, rest as shortlist")
    output_group.add_argument("--by-time", action="store_true", help="Sort results by time instead of score")
    output_group.add_argument("--json", action="store_true", help="Output as structured JSON")
    output_group.add_argument("--format", metavar="NAME", help="Use named formatter (built-in or drop-in plugin)")

    # Result modes
    mode_group = p_search.add_argument_group("result modes")
    mode_group.add_argument("--conversations", action="store_true", help="Aggregate scores per conversation, return ranked conversations")
    mode_group.add_argument("--first", action="store_true", help="Return chronologically earliest match above threshold")
    mode_group.add_argument("--refs", nargs="?", const=True, metavar="FILES", help="Show file references; optionally filter by comma-separated basenames")

    # Search mode selection
    mode_selection = p_search.add_argument_group("search mode")
    mode_selection.add_argument("--fts", action="store_true", help="Force FTS5 keyword search (no embeddings)")
    mode_selection.add_argument("--semantic", action="store_true", help="Force semantic search (requires embeddings)")

    # Search tuning
    tuning_group = p_search.add_argument_group("search tuning")
    tuning_group.add_argument("--embeddings-only", action="store_true", help="Skip FTS5 recall, use pure embeddings")
    tuning_group.add_argument("--recall", type=int, default=80, metavar="N", help="FTS5 conversation recall limit (default: 80)")
    tuning_group.add_argument("--threshold", type=float, metavar="SCORE", help="Filter results below this score (e.g., 0.7)")
    tuning_group.add_argument("--raw-fts", action="store_true", help="Pass query directly to FTS5 without tokenization (advanced: skips OR fallback)")

    # Diversity (MMR reranking)
    diversity_group = p_search.add_argument_group("diversity")
    diversity_group.add_argument("--no-diversity", action="store_true", help="Disable MMR reranking for deterministic pure relevance order")
    diversity_group.add_argument("--lambda", type=float, default=0.7, dest="lambda_", metavar="FLOAT", help="MMR lambda: 1.0=relevance, 0.0=diversity (default: 0.7)")

    # Recency boost
    recency_group = p_search.add_argument_group("recency")
    recency_group.add_argument("--recency", action="store_true", help="Boost recent results (exponential decay, mild 15%% boost)")
    recency_group.add_argument("--recency-half-life", type=float, default=30.0, metavar="DAYS", help="Days until recency boost decays to half (default: 30)")
    recency_group.add_argument("--recency-max-boost", type=float, default=1.15, metavar="MULT", help="Max boost multiplier for today's results (default: 1.15)")

    # Scope options
    scope_group = p_search.add_argument_group("scope")
    scope_group.add_argument("--no-exclude-active", action="store_true", help="Include results from active sessions (excluded by default)")
    scope_group.add_argument("--include-derivative", action="store_true", help="Include derivative conversations (siftd search/query results)")

    # Index management
    index_group = p_search.add_argument_group("index management")
    index_group.add_argument("--index", action="store_true", help="Build/update embeddings index")
    index_group.add_argument("--rebuild", action="store_true", help="Rebuild embeddings index from scratch")
    index_group.add_argument("--backend", metavar="NAME", help="Embedding backend (ollama, fastembed)")
    index_group.add_argument("--embed-db", metavar="PATH", help="Alternate embeddings database path")

    p_search.set_defaults(func=cmd_search)
