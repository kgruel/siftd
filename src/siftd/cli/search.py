"""CLI handler for 'siftd search' — unified search over conversations.

Supports three modes:
- Hybrid (default with embeddings): FTS5 recall + semantic reranking
- FTS5-only (--fts or fallback): keyword search without embeddings
- Semantic-only (--semantic): pure embeddings without FTS5 recall
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from siftd.cli._common import _parse_turns_range, add_anchor_window_args, resolve_db
from siftd.cli._filters import extract_filter_args
from siftd.paths import embeddings_db_path


def _print_empty_json_results(args, query: str, db: Path, *, mode: str = "fts", caveats: list | None = None) -> None:
    """Emit empty JSON results for --json output modes. ``mode`` is the resolved engine."""
    import json

    from painted import Fidelity

    from siftd.output.format_registry import select_format

    fmt = select_format(json_mode=True, is_tty=False)
    result = fmt.render_search([], Fidelity(), query=query, view="chunks", mode=mode, caveats=caveats or [])
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


def _validate_search_axes(args) -> str | None:
    """Return an error string if the axis combination is invalid, else None."""
    if args.view in ("thread", "conversations") and args.sort == "time":
        return f"--view={args.view} is incompatible with --sort=time ({args.view} imposes its own ordering)"
    if getattr(args, "turns_range", None) is not None and getattr(args, "around", None) is None:
        return (
            "--turns requires --around PHRASE on search; "
            "use 'siftd show <id> --turns A:B' for conversation-detail navigation"
        )
    return None


def cmd_search(args) -> int:
    """Unified search over conversations — auto-selects FTS5 or semantic based on availability."""
    from siftd.api import embeddings_available

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

    # Validate axis combinations before any execution
    axis_err = _validate_search_axes(args)
    if axis_err:
        print(f"siftd: error: {axis_err}", file=sys.stderr)
        sys.exit(2)

    # Validate the --turns window format early for a friendly exit(2); search_view
    # re-parses the raw string when it runs the recipe (on the wire or locally).
    if getattr(args, "around", None) is not None and getattr(args, "turns_range", None) is not None:
        _parse_turns_range(args.turns_range)

    # --refs with --json is not supported (refs dump would break JSON validity)
    if args.json and args.refs:
        print("Error: --refs is not supported with --json", file=sys.stderr)
        return 1

    # --view=thread with --json: warn and ignore (JSON formatter doesn't use thread grouping)
    if args.json and args.view == "thread":
        print("Note: --view=thread is ignored with --json output", file=sys.stderr)

    # Extract standard filters once for delegation and candidate resolution
    filters = extract_filter_args(args)

    # Determine the engine that will run. --mode is the engine selector
    # (auto|fts|semantic|hybrid); 'auto' resolves against local availability.
    # The resolved value is what gets reported back as output.mode — never 'auto'.
    from siftd.api.search import EmbeddingsRequiredError, resolve_search_mode

    requested_mode = getattr(args, "mode", "auto")
    has_embeddings = embeddings_available() and embed_db.exists()

    # Explicit semantic/hybrid without embeddings: surface the precise reason
    # (extra missing vs index missing). Keep stdout clean so `--json | jq` stays
    # valid — human text to stderr, structured error envelope on stdout.
    if requested_mode in ("semantic", "hybrid") and not has_embeddings:
        import json
        if not embeddings_available():
            if args.json:
                print(json.dumps({"error": f"Mode '{requested_mode}' requires the [embed] extra. Install with: siftd install embed"}))
            else:
                print(f"Mode '{requested_mode}' requires the [embed] extra.", file=sys.stderr)
                print(file=sys.stderr)
                print("Install with:", file=sys.stderr)
                print("  siftd install embed", file=sys.stderr)
        else:
            if args.json:
                print(json.dumps({"error": "No embeddings index found. Run 'siftd search --index' to build it."}))
            else:
                print("No embeddings index found.", file=sys.stderr)
                print("Run 'siftd search --index' to build it.", file=sys.stderr)
        return 1

    try:
        search_mode = resolve_search_mode(requested_mode, has_embeddings=has_embeddings)
    except EmbeddingsRequiredError:
        # Covered by the explicit pre-check above; defensive.
        print(f"Mode '{requested_mode}' requires embeddings.", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"siftd: error: {e}", file=sys.stderr)
        return 1

    # Explicit `--mode fts` uses the dedicated lean keyword path (which warns
    # about embeddings-only flags). auto-resolved fts (no embeddings installed)
    # stays on the main path below, keeping full view/flag support — it just
    # ranks by FTS5 instead of semantic.
    if requested_mode == "fts":
        return _search_fts_only(args, db, query, filters)

    from siftd.api.dispatch import (
        Operation,
        deserialize_caveats,
        execute_for_render,
        from_wire,
    )
    from siftd.api.search import search_view
    from siftd.cli._common import fidelity_from_args

    # Validate the output format up front — fail fast before any search work.
    from siftd.output.format_registry import select_format
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    try:
        fmt = select_format(
            name=getattr(args, "format", None),
            json_mode=args.json,
            is_tty=sys.stdout.isatty(),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    fidelity = fidelity_from_args(args)
    rerank = "mmr" if not args.no_diversity else "relevance"

    # One Operation now carries the whole search: engine + the post-processing
    # recipe (search_view = search_chunks ∘ process_search_view). ``n`` is the
    # FINAL result count; search_view widens the engine pool internally. The
    # recipe controls (view/sort/select/threshold/full/around/turns) travel on
    # the wire so a delegated or REST search runs the identical recipe.
    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=search_view,
        params={
            "q": query,
            "db_path": db,
            "embed_db": embed_db,
            "n": args.limit,
            "mode": search_mode,
            "workspace": filters.workspace,
            "model": filters.model,
            "since": filters.since,
            "before": filters.before,
            "tag": filters.tag,
            "all_tags": filters.all_tags,
            "no_tag": filters.no_tag,
            "tag_kind": filters.tag_kind,
            "owner": filters.owner,
            "tool": filters.tool,
            "tool_tag": filters.tool_tag,
            "exclude_active": not args.no_exclude_active,
            "include_derivative": args.include_derivative,
            "recall": args.recall,
            "rerank": rerank,
            "lambda_": args.lambda_,
            "recency": args.recency,
            "recency_half_life": args.recency_half_life,
            "recency_max_boost": args.recency_max_boost,
            "backend": args.backend,
            "raw_fts": getattr(args, "raw_fts", False),
            "debug_ids": getattr(args, "debug_ids", False),
            "view": args.view,
            "sort": args.sort,
            "select": args.select,
            "threshold": args.threshold,
            "full": args.full,
            "around": getattr(args, "around", None),
            "turns": getattr(args, "turns_range", None),
        },
        render_method="search",
        fidelity=fidelity,
        db=db,
    )

    # --refs dumps file CONTENT, which never rides the wire (privacy + the JSON
    # envelope omits it), so run locally to keep file_refs content-bearing.
    # Otherwise try serve delegation (warm caches, embeddings loaded) and
    # reconstruct the SearchView from the wire — indistinguishable from local.
    view_result = None
    caveats: list = []
    if not args.refs and _can_delegate_to_serve(args, db=db, embed_db=embed_db):
        try:
            body = try_serve(op)
        except ServeRequest4xx as e:
            print_serve_4xx(e)
            return 1
        if isinstance(body, dict):
            # I5: surface the server's caveats (stale index, degraded mode) on
            # the delegated path — without this the thin client always shows none.
            caveats = deserialize_caveats(body)
            view_result = from_wire(op, body)

    # Local execution (or a wire body that failed to deserialize → fall back).
    if view_result is None:
        try:
            view_result, caveats = execute_for_render(op)
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Empty results: distinguish a genuinely empty engine result from a
    # deliberately-emptied one (threshold / select=first) for a precise message.
    if not view_result.results:
        if args.json:
            _print_empty_json_results(args, query, db, mode=search_mode, caveats=caveats)
        else:
            if view_result.empty_reason == "threshold":
                print(f"No results above threshold {args.threshold} for: {query}")
            elif view_result.empty_reason == "first":
                print(f"No results above relevance threshold for: {query}")
            else:
                print(f"No results for: {query}")
            for c in caveats:
                print(f"note: {c.message}")
        return 0

    # --around dropped results whose conversation lacked the anchor phrase.
    if view_result.n_skipped > 0:
        print(
            f"note: filtered {view_result.n_skipped} result(s) without --around phrase '{args.around}' in conversation",
            file=sys.stderr,
        )

    # Privacy warning for full content display
    if args.full or args.refs:
        print("Note: Showing full content which may contain sensitive information.", file=sys.stderr)

    # ctx "mode" = resolved engine (truthful report of what ran); the view shape
    # and the thread tier1/tier2 split ride the SearchView itself.
    output = fmt.render_search(
        view_result,
        fidelity,
        query=query,
        mode=search_mode,
        debug_ids=getattr(args, "debug_ids", False),
        caveats=caveats,
    )
    from siftd.output.painted_bridge import emit_output

    emit_output(output)

    # --refs content dump (post-processor, not part of formatter)
    if args.refs and args.view != "conversations":
        from siftd.output.common import print_refs_content

        all_refs = []
        for r in view_result.results:
            all_refs.extend(r.get("file_refs") or [])
        filter_basenames = None
        if isinstance(args.refs, str):
            filter_basenames = [b.strip() for b in args.refs.split(",") if b.strip()]
        print_refs_content(all_refs, filter_basenames)

    return 0


def _search_fts_only(args, db: Path, query: str, filters=None) -> int:
    """FTS5-only search mode — keyword search without embeddings."""
    import sqlite3

    from painted import Fidelity

    from siftd.api.dispatch import Operation, deserialize_caveats, execute_for_render
    from siftd.cli._common import fidelity_from_args

    # Warn about flags that are ignored in FTS5-only mode
    unsupported_flags = []
    if args.view == "thread":
        unsupported_flags.append("--view=thread")
    if args.view == "conversations":
        unsupported_flags.append("--view=conversations")
    if args.full:
        unsupported_flags.append("--full")
    if args.verbose:
        unsupported_flags.append("--verbose/-v")
    if args.select == "first":
        unsupported_flags.append("--select=first")
    if args.refs:
        unsupported_flags.append("--refs")
    if args.format:
        unsupported_flags.append("--format")

    if unsupported_flags:
        flags_str = ", ".join(unsupported_flags)
        print(f"WARNING: {flags_str} ignored in FTS5 mode (requires embeddings)", file=sys.stderr)

    # Compose filters
    if filters is None:
        filters = extract_filter_args(args)

    # Validate the output format up front — fail fast before any search work,
    # matching cmd_search (the slice-3 fail-fast principle). fmt is consumed by
    # the non-empty render below; the empty path emits a manual envelope.
    from siftd.output.format_registry import select_format

    try:
        fmt = select_format(
            name=getattr(args, "format", None),
            json_mode=args.json,
            is_tty=sys.stdout.isatty(),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    fidelity = Fidelity()

    from siftd.api.search import search_view

    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=search_view,
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
            "tag_kind": filters.tag_kind,
            "owner": filters.owner,
            "tool": filters.tool,
            "tool_tag": filters.tool_tag,
            "exclude_active": not args.no_exclude_active,
            "include_derivative": args.include_derivative,
            "raw_fts": getattr(args, "raw_fts", False),
            "debug_ids": getattr(args, "debug_ids", False),
            # FTS only ever produces the chunks shape; the embeddings-dependent
            # recipe steps (views/threshold/select/full) were warned-and-dropped
            # above. --sort and --around still apply.
            "view": "chunks",
            "sort": args.sort,
            "around": getattr(args, "around", None),
            "turns": getattr(args, "turns_range", None),
        },
        render_method="search",
        fidelity=fidelity_from_args(args),
        db=db,
    )

    from siftd.api.dispatch import from_wire

    caveats: list = []
    view_result = None
    # Resolve embed_db same as cmd_search so the custom --embed-db guard fires correctly.
    _raw_embed_db = getattr(args, "embed_db", None)
    _embed_db = Path(_raw_embed_db).expanduser() if _raw_embed_db else embeddings_db_path()
    # --refs forces local (file content never rides the wire); else delegate and
    # reconstruct the SearchView, indistinguishable from local execution.
    if not args.refs and _can_delegate_to_serve(args, db=db, embed_db=_embed_db):
        from siftd.serve.client import ServeRequest4xx
        from siftd.serve.delegation import print_serve_4xx, try_serve
        try:
            body = try_serve(op)
        except ServeRequest4xx as e:
            print_serve_4xx(e)
            return 1
        if isinstance(body, dict):
            # I5: surface the server's caveats on the delegated path.
            caveats = deserialize_caveats(body)
            view_result = from_wire(op, body)

    if view_result is None:
        try:
            view_result, caveats = execute_for_render(op)
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

    import json as json_mod

    # Empty results don't need a formatter — emit the manual FTS envelope (or
    # text) and return before validating --format (which is ignored in FTS mode
    # anyway), preserving the prior no-results behavior.
    if not view_result.results:
        if args.json:
            out = {
                "query": query,
                "mode": "fts",
                "view": "chunks",
                "results": [],
                "caveats": [asdict(c) for c in caveats],
            }
            if unsupported_flags:
                out["warnings"] = [
                    f"{flag} ignored in FTS5 mode (requires embeddings)"
                    for flag in unsupported_flags
                ]
            print(json_mod.dumps(out, indent=2))
        else:
            print(f"No results for: {query}")
            for c in caveats:
                print(f"note: {c.message}")
        return 0

    if view_result.n_skipped > 0:
        print(
            f"note: filtered {view_result.n_skipped} result(s) without --around phrase '{args.around}' in conversation",
            file=sys.stderr,
        )

    output = fmt.render_search(view_result, fidelity, query=query, mode="fts", debug_ids=getattr(args, "debug_ids", False), caveats=caveats)
    if isinstance(output, dict):
        if unsupported_flags:
            output["warnings"] = [
                f"{flag} ignored in FTS5 mode (requires embeddings)"
                for flag in unsupported_flags
            ]
        print(json_mod.dumps(output, indent=2, default=str))
    else:
        from siftd.output.painted_bridge import emit_output

        emit_output(output)

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


def _engine_mode(value: str) -> str:
    """argparse type for ``--mode``: validate the engine selector and redirect the
    old render values to ``--view`` with a clear hint (clean-break migration)."""
    if value in ("chunks", "thread", "conversations"):
        raise argparse.ArgumentTypeError(
            f"--mode now selects the search engine; use --view {value} for result shape"
        )
    from siftd.api.search import SEARCH_MODES

    if value not in SEARCH_MODES:
        raise argparse.ArgumentTypeError(
            f"invalid engine {value!r}; choose from {', '.join(SEARCH_MODES)}"
        )
    return value


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

  # explicit engine selection
  siftd search --mode fts "error handling"             # force FTS5 keyword search
  siftd search --mode semantic "auth flow"             # force semantic search

  # refine
  siftd search "design decision" --view=thread         # narrative: top conversations expanded
  siftd search "why we chose X" --around "why" --turns -2:+2  # ±2 turns around phrase
  siftd search "event sourcing" --view=conversations   # rank whole conversations, not chunks
  siftd search "when first discussed Y" --select=first # earliest match above threshold
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

  # filter by tool use
  siftd search --tool shell.execute "test failure"     # only conversations that ran a shell
  siftd search --tool-tag shell:vcs "merge conflict"   # conversations with a git-tagged tool call

  # save useful results for future retrieval
  siftd tag 01HX... research:auth                   # bookmark a conversation
  siftd tag --last research:architecture            # tag most recent conversation
  siftd query -l research:auth                      # retrieve tagged conversations

  # tuning
  siftd search --recall 200 "error"                    # widen FTS5 candidate pool
  siftd search --sort=time "chunking"                   # sort by time instead of score

  # diversity vs relevance (MMR reranking)
  siftd search --no-diversity "chunking"               # pure relevance order (deterministic)
  siftd search --lambda 0.5 "design"                   # more diverse results (less redundancy)
  siftd search --json "auth" | jq '.results[0].breakdown'  # score component breakdown
  siftd search --json "auth" | jq '.results[0].turn_index'  # turn index for drill-in

note: --context N was removed in v0.9.x. Use --around PHRASE --turns -N:+N instead.
  Example: --context 2 → --around "phrase" --turns -2:+2""",
    )

    # Positional argument
    p_search.add_argument("query", nargs="*", help="Natural language search query")

    # Filtering options (most commonly used)
    from siftd.cli._filters import add_filter_args

    add_filter_args(p_search, include_tool=True, include_tool_tag=True)

    # Output options
    from siftd.cli._common import add_fidelity_args, add_output_args

    add_output_args(p_search, json=True, limit=True, limit_default=10)
    add_fidelity_args(p_search, full=True)

    search_display = p_search.add_argument_group("search display")
    search_display.add_argument("-v", "--verbose", action="store_true", help="Show full chunk text")
    search_display.add_argument("--format", metavar="NAME", help="Use named formatter (built-in or drop-in plugin)")

    # Navigation: phrase-anchored window (--around + --turns only; query-specific anchors not on search)
    # Note: --context was removed in v0.9.x; use --around PHRASE --turns -N:+N instead.
    add_anchor_window_args(p_search, anchors=frozenset({"around"}), windows=frozenset({"turns"}))

    # Result modes — three orthogonal axes
    mode_group = p_search.add_argument_group("result modes")
    mode_group.add_argument(
        "--select",
        choices=["all", "first"],
        default="all",
        metavar="SELECTOR",
        help="Result selector: all (default) or first (chronologically earliest match above threshold)",
    )
    mode_group.add_argument(
        "--sort",
        choices=["score", "time"],
        default="score",
        metavar="ORDER",
        help="Sort order: score (default, relevance) or time (chronological). Incompatible with --mode=thread/conversations.",
    )
    mode_group.add_argument(
        "--view",
        choices=["chunks", "thread", "conversations"],
        default="chunks",
        metavar="VIEW",
        help="Result shape: chunks (default), thread (narrative drill-down), or conversations (aggregated ranking)",
    )
    mode_group.add_argument("--refs", nargs="?", const=True, metavar="FILES", help="Show file references; optionally filter by comma-separated basenames")

    # Engine selection
    engine_group = p_search.add_argument_group("search engine")
    engine_group.add_argument(
        "--mode",
        type=_engine_mode,
        default="auto",
        metavar="ENGINE",
        help="Search engine: auto (default), fts, semantic, or hybrid. auto picks hybrid when embeddings are installed, else fts.",
    )

    # Search tuning
    tuning_group = p_search.add_argument_group("search tuning")
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

    def _search_unknown_hint(unknowns):
        # Both "--context 2" (two tokens) and "--context=2" (one token) must match.
        if any(u == "--context" or u.startswith("--context=") for u in unknowns):
            return "note: --context N was removed in v0.9.x. Use --around PHRASE --turns -N:+N instead."
        return None

    p_search.set_defaults(func=cmd_search, _unknown_hint=_search_unknown_hint)
