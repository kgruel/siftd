"""CLI handler for 'siftd search' — unified search over conversations.

Supports three engines, selected via --mode:
- hybrid (default with embeddings): FTS5 recall + semantic reranking
- fts (fallback without embeddings): keyword search without embeddings
- semantic: pure embeddings without FTS5 recall
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from siftd.cli._common import _parse_turns_range, add_anchor_window_args, resolve_db
from siftd.cli._filters import extract_filter_args
from siftd.output import status
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


def _can_delegate_to_serve(args, *, db: Path) -> bool:
    """Conservatively decide whether it's safe to delegate to siftd-serve.

    A served instance uses its own [embed] config; a delegated search downgrades
    truthfully via the envelope mode field (no embeddings-DB override rides HTTP —
    the `--embed-db` search flag was removed with the index-management flags).
    """
    from siftd.serve.delegation import can_delegate

    return can_delegate(db=db)


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
    embed_db = embeddings_db_path()

    if not db.exists():
        status.db_missing(db)
        return 1

    # Search mode — need a query OR a tag facet. A query ranks content; a bare
    # tag facet enumerates tagged elements (filter-only search, decision 1).
    query = " ".join(args.query) if args.query else ""
    facet_only = not query and bool(getattr(args, "tag", None) or getattr(args, "all_tags", None))
    if not query and not facet_only:
        # A usage triplet can't ride a callout hint (it flattens newlines), so the
        # examples travel on a lines() body — print_ambiguous_error's idiom — all
        # to stderr so a piped stdout stays clean.
        from painted import print_block

        from siftd.output.common import should_use_ansi
        from siftd.output.listing import lines

        status.error("A search query or tag filter is required.")
        print_block(
            lines([
                "siftd search <query>",
                "siftd search --tag NAME  (enumerate tagged elements)",
                "siftd embed              (build/update the semantic index)",
            ]),
            sys.stderr,
            use_ansi=should_use_ansi(sys.stderr),
        )
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
        status.error("--refs is not supported with --json")
        return 1

    # --view=thread with --json: warn and ignore (JSON formatter doesn't use thread grouping)
    if args.json and args.view == "thread":
        status.info("--view=thread is ignored with --json output")

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
    if requested_mode in ("semantic", "hybrid") and not has_embeddings and not facet_only:
        import json
        if not embeddings_available():
            if args.json:
                print(json.dumps({"error": f"Mode '{requested_mode}' requires an embedding backend. Configure embed.backend or run 'siftd install embed'"}))
            else:
                status.error(
                    f"Mode '{requested_mode}' requires an embedding backend.",
                    hint="Configure embed.backend, or run 'siftd install embed' for the local backend.",
                )
        else:
            if args.json:
                print(json.dumps({"error": "No embeddings index found. Run 'siftd embed' to build it."}))
            else:
                status.error(
                    "No embeddings index found.",
                    hint="Run 'siftd embed' to build it.",
                )
        return 1

    try:
        search_mode = resolve_search_mode(requested_mode, has_embeddings=has_embeddings)
    except EmbeddingsRequiredError:
        # Covered by the explicit pre-check above; defensive.
        status.error(f"Mode '{requested_mode}' requires embeddings.")
        return 1
    except ValueError as e:
        status.error(str(e))
        return 1

    # Explicit `--mode fts` uses the dedicated lean keyword path (which warns
    # about embeddings-only flags). auto-resolved fts (no embeddings installed)
    # stays on the main path below, keeping full view/flag support — it just
    # ranks by FTS5 instead of semantic.
    if requested_mode == "fts" and not facet_only:
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
        status.error(str(e))
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
    if not args.refs and _can_delegate_to_serve(args, db=db):
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
    # EmbeddingConfigError (e.g. a revoked API key) is a config failure, not degradable —
    # it doesn't subclass RuntimeError, so it's caught explicitly for a clean error line.
    from siftd.api import EmbeddingConfigError

    if view_result is None:
        try:
            view_result, caveats = execute_for_render(op)
        except (RuntimeError, ValueError, EmbeddingConfigError) as e:
            status.error(str(e))
            return 1

    # The engine that actually ran: a runtime embed failure degrades hybrid/semantic
    # to fts (SearchView.executed_mode); a delegated read carries the server's mode.
    # This is what gets reported — never the pre-resolved value.
    effective_mode = getattr(view_result, "executed_mode", None) or search_mode

    # Empty results: distinguish a genuinely empty engine result from a
    # deliberately-emptied one (threshold / select=first) for a precise message.
    if not view_result.results:
        if args.json:
            _print_empty_json_results(args, query, db, mode=effective_mode, caveats=caveats)
        else:
            if view_result.empty_reason == "threshold":
                status.info(f"No results above threshold {args.threshold} for: {query}")
            elif view_result.empty_reason == "first":
                status.info(f"No results above relevance threshold for: {query}")
            else:
                status.info(f"No results for: {query}")
            status.caveats(caveats)
        return 0

    # --around dropped results whose conversation lacked the anchor phrase.
    if view_result.n_skipped > 0:
        status.info(
            f"filtered {view_result.n_skipped} result(s) without --around phrase '{args.around}' in conversation"
        )

    # Privacy warning for full content display
    if args.full or args.refs:
        status.info("Showing full content which may contain sensitive information.")

    # ctx "mode" = resolved engine (truthful report of what ran); the view shape
    # and the thread tier1/tier2 split ride the SearchView itself.
    output = fmt.render_search(
        view_result,
        fidelity,
        query=query,
        mode=effective_mode,
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
        status.warning(f"{flags_str} ignored in FTS5 mode (requires embeddings)")

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
        status.error(str(e))
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
    # --refs forces local (file content never rides the wire); else delegate and
    # reconstruct the SearchView, indistinguishable from local execution.
    if not args.refs and _can_delegate_to_serve(args, db=db):
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
                status.error("FTS index not found.", hint="Run 'siftd ingest' first.")
            elif "fts5" in err_msg or "syntax" in err_msg:
                status.error(f"Invalid search query: {e}", hint="Check your search query for syntax errors.")
            else:
                status.error(f"Database error: {e}")
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
            status.info(f"No results for: {query}")
            status.caveats(caveats)
        return 0

    if view_result.n_skipped > 0:
        status.info(
            f"filtered {view_result.n_skipped} result(s) without --around phrase '{args.around}' in conversation"
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
        help="Search conversations (auto-selects FTS5 or semantic based on embed.backend)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Auto-selects the engine: hybrid (FTS5 + semantic) when an embedding
backend is configured, else FTS5 keyword search. Set embed.backend for a remote
provider, or run 'siftd install embed' for the local backend.

examples:
  siftd search "error handling"                   # auto (hybrid or FTS5)
  siftd search -w myproject "auth flow"           # filter by workspace
  siftd search --since 7d "testing"               # filter by date
  siftd search "design decision" --view thread    # narrative: top conversations
  siftd search "why X" --around why --turns -2:+2 # window around a phrase match
  siftd search -l research: "auth flow"           # search within tagged conversations

(--context was removed; use --around PHRASE --turns -N:+N)""",
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

    search_display = p_search.add_argument_group("output")
    search_display.add_argument("-v", "--verbose", action="store_true", help="Show full chunk text")
    search_display.add_argument("--format", metavar="NAME", help="Use named formatter (built-in or drop-in plugin)")

    # Navigation: phrase-anchored window (--around + --turns only; query-specific anchors not on search)
    # Note: --context was removed in v0.9.x; use --around PHRASE --turns -N:+N instead.
    add_anchor_window_args(p_search, anchors=frozenset({"around"}), windows=frozenset({"turns"}))

    # Result modes — three orthogonal axes; join the "view" section
    mode_group = p_search.add_argument_group("view")
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
    engine_group = p_search.add_argument_group("search")
    engine_group.add_argument(
        "--mode",
        type=_engine_mode,
        default="auto",
        metavar="ENGINE",
        help="Search engine: auto (default), fts, semantic, or hybrid. auto picks hybrid when embeddings are installed, else fts.",
    )

    # Search tuning — join the "search" section
    tuning_group = p_search.add_argument_group("search")
    tuning_group.add_argument("--recall", type=int, default=80, metavar="N", help="FTS5 conversation recall limit (default: 80)")
    tuning_group.add_argument("--threshold", type=float, metavar="SCORE", help="Filter results below this score (e.g., 0.7)")
    tuning_group.add_argument("--raw-fts", action="store_true", help="Pass query directly to FTS5 without tokenization (advanced: skips OR fallback)")

    # Diversity (MMR reranking) — join the "ranking" section
    diversity_group = p_search.add_argument_group("ranking")
    diversity_group.add_argument("--no-diversity", action="store_true", help="Disable MMR reranking for deterministic pure relevance order")
    diversity_group.add_argument("--lambda", type=float, default=0.7, dest="lambda_", metavar="FLOAT", help="MMR lambda: 1.0=relevance, 0.0=diversity (default: 0.7)")

    # Recency boost — join the "ranking" section
    recency_group = p_search.add_argument_group("ranking")
    recency_group.add_argument("--recency", action="store_true", help="Boost recent results (exponential decay, mild 15%% boost)")
    recency_group.add_argument("--recency-half-life", type=float, default=30.0, metavar="DAYS", help="Days until recency boost decays to half (default: 30)")
    recency_group.add_argument("--recency-max-boost", type=float, default=1.15, metavar="MULT", help="Max boost multiplier for today's results (default: 1.15)")

    # Scope options — join the "ranking" section
    scope_group = p_search.add_argument_group("ranking")
    scope_group.add_argument("--no-exclude-active", action="store_true", help="Include results from active sessions (excluded by default)")
    scope_group.add_argument("--include-derivative", action="store_true", help="Include derivative conversations (siftd search/query results)")

    # Index management moved to `siftd embed`. --index/--rebuild/--backend/--embed-db are
    # no longer flags here; parse_known_args routes them to _search_unknown_hint below,
    # which redirects to the new command (the --context→--around removal precedent).

    def _search_unknown_hint(unknowns):
        # Both "--flag value" (two tokens) and "--flag=value" (one token) must match.
        def _seen(flag: str) -> bool:
            return any(u == flag or u.startswith(f"{flag}=") for u in unknowns)

        if _seen("--context"):
            return "note: --context N was removed in v0.9.x. Use --around PHRASE --turns -N:+N instead."
        if any(_seen(f) for f in ("--index", "--rebuild", "--backend", "--embed-db")):
            return (
                "note: index management moved to 'siftd embed'. "
                "Use 'siftd embed' (incremental) or 'siftd embed --rebuild'; "
                "the backend is set by config (embed.backend)."
            )
        return None

    p_search.set_defaults(func=cmd_search, _unknown_hint=_search_unknown_hint)
