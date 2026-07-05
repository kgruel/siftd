"""CLI handler for 'siftd embed' — build and inspect the semantic-search index.

Not an Operation: `siftd embed` mutates/reads machine-local derived state (the
embeddings DB) with the local key — there is nothing to delegate and no wire path,
unlike search's shared-data query surface. Imports flow only through ``siftd.api``.
"""

import argparse
from pathlib import Path

from siftd.cli._common import embedding_awaiting_message, resolve_db
from siftd.output import fmt_count, status
from siftd.paths import embeddings_db_path


def cmd_embed(args) -> int:
    """Build or inspect the embeddings index for semantic search."""
    from siftd.api import embeddings_available

    db = resolve_db(args)
    embed_db = Path(args.embed_db).expanduser() if args.embed_db else embeddings_db_path()

    if args.status:
        return _embed_status(args, db, embed_db)

    if not db.exists():
        status.db_missing(db)
        return 1

    # Cheap availability gate (no DB read, no model load). The precise reason is
    # available via `siftd embed --status`.
    if not embeddings_available():
        status.error(
            "No embedding backend is configured.",
            hint=(
                "Set embed.backend (voyage|openai|...) with an API key, or run "
                "'siftd install embed' for the local backend. See 'siftd embed --status'."
            ),
        )
        return 1

    from siftd.api import EmbeddingConfigError, IncrementalCompatError, build_index

    try:
        result = build_index(
            db_path=db, embed_db_path=embed_db, rebuild=args.rebuild, verbose=True
        )
    except FileNotFoundError as e:
        status.error(str(e), hint="Run 'siftd ingest' to create it.")
        return 1
    except IncrementalCompatError as e:
        status.error(str(e))
        return 1
    except (RuntimeError, ValueError, EmbeddingConfigError) as e:
        # EmbeddingConfigError (e.g. a revoked key mid-embed) is not a RuntimeError;
        # catch it for a clean error line rather than a traceback.
        status.error(str(e))
        return 1

    added = result["chunks_added"]
    removed = result["chunks_removed"]
    pruned = result["conversations_pruned"]
    total = result["total_chunks"]

    if added == 0 and removed == 0:
        status.confirm(f"Index is up to date. ({fmt_count(total)} chunks)")
        return 0

    parts: list[str] = []
    if added:
        parts.append(f"{fmt_count(added)} chunk(s) added")
    if removed:
        parts.append(f"{fmt_count(removed)} chunk(s) removed")
    if pruned:
        parts.append(f"{pruned} conversation(s) pruned")
    status.confirm(f"{', '.join(parts)}. Index has {fmt_count(total)} chunks.")
    return 0


def _embed_status(args, db: Path, embed_db: Path) -> int:
    """Render the ``--status`` overview: configured backend + built-index stats."""
    from siftd.api import embed_status

    report = embed_status(db_path=db, embed_db_path=embed_db)

    if args.json:
        import json
        from dataclasses import asdict

        print(json.dumps(asdict(report), indent=2))
        return 0

    from painted import Style

    from siftd.output.listing import print_definitions, print_heading
    from siftd.output.theme import domain_styles

    ds = domain_styles()

    def cell(value: object, style: Style | None = None) -> list[tuple[str, Style | None]]:
        return [(str(value), style)]

    print_heading("Embedding backend")
    backend_label = report.configured_backend or "none"
    if not report.configured_usable:
        backend_label = f"{backend_label} (not usable)"
    print_definitions([
        ("Configured", cell(backend_label)),
        ("Status", cell(report.configured_reason)),
    ])

    print()
    print_heading("Index")
    if report.total_chunks == 0:
        # Unbuilt, or built but empty (no chunks) — an incremental embed suffices; this is
        # never a rebuild situation, so don't surface schema/needs_rebuild here.
        print_definitions([
            ("Location", cell(embed_db)),
            ("State", cell("not built")),
            ("Conversations", cell(report.conversations_total, ds.metric)),
        ])
        status.info("Run 'siftd embed' to build the index.")
        return 0

    if report.needs_rebuild:
        status.warning(
            f"Index schema is outdated (v{report.schema_version or 1}); a rebuild is required.",
            hint="Run 'siftd embed --rebuild'.",
        )
    elif report.backend_mismatch:
        set_hint = (
            f" or set embed.backend = {report.stored_backend_config}"
            if report.stored_backend_config
            else ""
        )
        status.warning(
            f"Index was built with a different backend ({report.stored_backend}) than the "
            f"configured one ({report.configured_backend}); the next 'siftd embed' would fail.",
            hint=f"Run 'siftd embed --rebuild'{set_hint}.",
        )

    stored = report.stored_backend or "unknown"
    if report.stored_model:
        stored = f"{stored} ({report.stored_model})"
    dim = report.stored_dimension if report.stored_dimension is not None else "?"

    chunk_breakdown = ", ".join(
        f"{ctype} {fmt_count(cnt)}"
        for ctype, cnt in sorted(report.chunk_counts.items())
    )
    coverage = (
        f"{fmt_count(report.conversations_indexed)}/"
        f"{fmt_count(report.conversations_total)}"
    )

    rows: list[tuple[str, list[tuple[str, Style | None]]]] = [
        ("Location", cell(embed_db)),
        ("Backend", cell(stored)),
        ("Dimension", cell(dim, ds.metric)),
        ("Schema", cell(f"v{report.schema_version or 1}", ds.metric)),
        ("Chunks", cell(fmt_count(report.total_chunks), ds.metric)),
    ]
    if chunk_breakdown:
        rows.append(("By type", cell(chunk_breakdown)))
    rows.append(("Coverage", [(coverage, ds.metric), (" conversations", None)]))
    rows.append(("Stale", cell(report.conversations_stale, ds.metric)))
    rows.append(("Size", cell(_human_size(report.db_size_bytes))))
    if report.built_at:
        rows.append(("Built", cell(report.built_at)))
    print_definitions(rows)

    if report.conversations_stale and not report.needs_rebuild:
        subject, hint = embedding_awaiting_message(report.conversations_stale)
        status.info(subject, hint=hint)
    return 0


def _human_size(n: int) -> str:
    """Format a byte count as a compact human-readable size."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def build_embed_parser(subparsers) -> None:
    """Add the 'embed' subparser to the CLI."""
    p_embed = subparsers.add_parser(
        "embed",
        help="Build and inspect the semantic-search index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Builds the embeddings index that powers semantic search. The backend
is config-driven (embed.backend); install the local backend or configure a remote
one with 'siftd install embed'.

examples:
  siftd embed                 # incremental: index new + changed, prune removed
  siftd embed --rebuild       # rebuild the whole index from scratch
  siftd embed --status        # backend, coverage, staleness, size""",
    )
    p_embed.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the entire index from scratch (instead of incremental)",
    )
    p_embed.add_argument(
        "--status",
        action="store_true",
        help="Show index stats: backend, model, coverage, staleness, size",
    )
    p_embed.add_argument(
        "--embed-db",
        metavar="PATH",
        help="Alternate embeddings database path",
    )
    p_embed.add_argument("--json", action="store_true", help="Output --status as JSON")
    p_embed.set_defaults(func=cmd_embed)
