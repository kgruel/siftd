"""CLI handler for 'siftd ask' — semantic search over conversations."""

import argparse
import sys
from pathlib import Path
from typing import cast

from siftd.paths import db_path, embeddings_db_path


def _apply_ask_config(args) -> None:
    """Apply config defaults to args if no formatter flag is explicitly set."""
    from siftd.config import get_ask_defaults

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

    from siftd.embeddings import embeddings_available
    from siftd.storage.embeddings import (
        open_embeddings_db,
        search_similar,
    )

    # Apply config defaults before processing
    _apply_ask_config(args)

    db = Path(args.db) if args.db else db_path()
    embed_db = Path(args.embed_db) if args.embed_db else embeddings_db_path()

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
        return _ask_build_index(db, embed_db, rebuild=args.rebuild, backend_name=args.backend, verbose=True)

    # Search mode — need a query
    query = " ".join(args.query) if args.query else ""
    if not query:
        print("Usage: siftd ask <query>")
        print("       siftd ask --index     (build/update index)")
        print("       siftd ask --rebuild   (rebuild index from scratch)")
        return 1

    if not embed_db.exists():
        print("No embeddings index found.")
        print("Run 'siftd ask --index' to build it.")
        return 1

    # Check embeddings availability before search
    if not embeddings_available():
        print("Semantic search requires the [embed] extra.", file=sys.stderr)
        print()
        print("Install with:")
        print("  siftd install embed")
        print()
        print("Or use FTS5 search instead:")
        print(f'  siftd query -s "{query}"')
        return 1

    # Resolve backend for query embedding
    from siftd.embeddings import get_backend
    try:
        backend = get_backend(preferred=args.backend, verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Compose filters: get candidate conversation IDs from main DB
    from siftd.search import filter_conversations, get_active_conversation_ids
    from siftd.storage.tags import DERIVATIVE_TAG

    exclude_tags = list(getattr(args, "no_tag", None) or [])
    if not args.include_derivative:
        exclude_tags.append(DERIVATIVE_TAG)

    candidate_ids = filter_conversations(
        db,
        workspace=args.workspace,
        model=args.model,
        since=args.since,
        before=args.before,
        tags=getattr(args, "tag", None),
        all_tags=getattr(args, "all_tags", None),
        exclude_tags=exclude_tags or None,
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

        from siftd.storage.fts import fts5_recall_conversations

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
        from siftd.search import resolve_role_ids
        role_source_ids = resolve_role_ids(db, args.role, candidate_ids)
        if not role_source_ids:
            print(f"No {args.role} content found matching filters.")
            return 0

    # Embed query and search
    use_mmr = not args.no_diversity
    query_embedding = backend.embed_one(query)
    embed_conn = open_embeddings_db(embed_db, read_only=True)
    # Widen initial search for modes that aggregate or filter post-hoc
    search_limit = args.limit
    if args.thread:
        search_limit = max(args.limit, 40)
    elif args.first or args.conversations:
        search_limit = max(args.limit * 10, 100)
    # Widen further for MMR to have candidates to diversify from
    if use_mmr:
        search_limit = max(search_limit * 3, search_limit)
    results: list[dict] = search_similar(
        embed_conn,
        query_embedding,
        limit=search_limit,
        conversation_ids=candidate_ids,
        role_source_ids=role_source_ids,
        include_embeddings=use_mmr,
    )
    embed_conn.close()

    if not results:
        print(f"No results for: {query}")
        return 0

    # Apply MMR diversity reranking
    if use_mmr and results:
        from siftd.search import mmr_rerank
        mmr_limit = args.limit
        if args.thread:
            mmr_limit = max(args.limit, 40)
        elif args.first or args.conversations:
            mmr_limit = max(args.limit * 10, 100)
        results = mmr_rerank(
            results,
            query_embedding,
            lambda_=args.lambda_,
            limit=mmr_limit,
        )

    # Apply threshold filter if specified
    if args.threshold is not None:
        results = [r for r in results if r["score"] >= args.threshold]
        if not results:
            print(f"No results above threshold {args.threshold} for: {query}")
            return 0

    # Post-processing: --first (earliest match above threshold)
    if args.first:
        from siftd.api import first_mention
        earliest = first_mention(results, threshold=0.65, db_path=db)
        if not earliest:
            print(f"No results above relevance threshold for: {query}")
            return 0
        results = [cast(dict, earliest)]

    # Trim to requested limit after post-processing (except --conversations which handles its own limit)
    if not args.conversations:
        results = results[:args.limit]

    # Enrich results with metadata from main DB
    main_conn = _sqlite3.connect(db)
    main_conn.row_factory = _sqlite3.Row

    # Enrich results with file refs (skip for --conversations mode)
    if not args.conversations:
        from siftd.api import fetch_file_refs
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
    from siftd.output import FormatterContext, print_refs_content, select_formatter
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
        print(f"Tip: Tag useful results for future retrieval: siftd tag {first_id} research:<topic>", file=sys.stderr)

    main_conn.close()
    return 0


def _ask_build_index(db: Path, embed_db: Path, *, rebuild: bool, backend_name: str | None, verbose: bool) -> int:
    """Build or incrementally update the embeddings index."""
    from siftd.api import build_index

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
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result["chunks_added"] == 0 and verbose:
        print(f"Index is up to date. ({result['total_chunks']} chunks)")

    return 0


def build_ask_parser(subparsers) -> None:
    """Add the 'ask' subparser to the CLI."""
    p_ask = subparsers.add_parser(
        "ask",
        help="Semantic search over conversations (requires [embed] extra)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Note: Requires the [embed] extra. Install with: siftd install embed

examples:
  # search
  siftd ask "error handling"                        # basic semantic search
  siftd ask -w myproject "auth flow"                # filter by workspace
  siftd ask --since 2024-06 "testing"               # filter by date

  # refine
  siftd ask "design decision" --thread              # narrative: top conversations expanded
  siftd ask "why we chose X" --context 2            # ±2 surrounding exchanges
  siftd ask "testing approach" --role user           # just your prompts, not responses
  siftd ask "event sourcing" --conversations        # rank whole conversations, not chunks
  siftd ask "when first discussed Y" --first        # earliest match above threshold
  siftd ask --threshold 0.7 "architecture"          # only high-relevance results

  # inspect
  siftd ask -v "chunking"                           # full chunk text
  siftd ask --full "chunking"                       # complete prompt+response exchange
  siftd ask --refs "authelia"                       # file references + content
  siftd ask --refs HANDOFF.md "setup"               # filter refs to specific file

  # filter by tags
  siftd ask -l research:auth "auth flow"            # search within tagged conversations
  siftd ask -l research: -l useful: "pattern"       # OR — any research: or useful: tag
  siftd ask --all-tags important --all-tags reviewed "design"  # AND — must have both
  siftd ask -l research: --no-tag archived "auth"   # combine OR + NOT

  # save useful results for future retrieval
  siftd tag 01HX... research:auth                   # bookmark a conversation
  siftd tag --last research:architecture            # tag most recent conversation
  siftd query -l research:auth                      # retrieve tagged conversations

  # tuning
  siftd ask --embeddings-only "chunking"            # skip FTS5, pure embeddings
  siftd ask --recall 200 "error"                    # widen FTS5 candidate pool
  siftd ask --chrono "chunking"                     # sort by time instead of score""",
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
    p_ask.add_argument("--include-derivative", action="store_true", help="Include derivative conversations (siftd ask/query results, excluded by default)")
    p_ask.add_argument("--no-diversity", action="store_true", help="Disable MMR diversity reranking, use pure relevance order")
    p_ask.add_argument("--lambda", type=float, default=0.7, dest="lambda_", metavar="FLOAT", help="MMR lambda: 1.0=pure relevance, 0.0=pure diversity (default: 0.7)")
    p_ask.add_argument("-l", "--tag", action="append", metavar="NAME", help="Filter by conversation tag (repeatable, OR logic)")
    p_ask.add_argument("--all-tags", action="append", metavar="NAME", help="Require all specified tags (AND logic)")
    p_ask.add_argument("--no-tag", action="append", metavar="NAME", help="Exclude conversations with this tag (NOT logic)")
    p_ask.set_defaults(func=cmd_ask)
