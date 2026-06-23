"""CLI handlers for meta commands (config, adapters) and db-delegated functions."""

import argparse
import json
from pathlib import Path

from siftd.api import list_workspaces
from siftd.cli._common import resolve_db
from siftd.output import status
from siftd.paths import cache_dir, config_dir, config_file, data_dir, db_path


def cmd_status(args) -> int:
    """Show database status and statistics."""
    from siftd.api import get_stats
    from siftd.api.dispatch import Operation, execute
    from siftd.api.stats import dict_to_stats, read_stats_cache
    from siftd.output import fmt_timestamp
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    db = Path(args.db) if args.db else None
    effective_db = db or db_path()

    # Fidelity not needed for stats rendering (no depth/visibility controls)
    # but Operation requires it — use a minimal default.
    from painted import Fidelity

    op = Operation(
        path="/api/v1/stats",
        method="GET",
        fn=get_stats,
        params={"db_path": db},
        render_method="stats",
        fidelity=Fidelity(),
        db=effective_db,
    )

    stats = None

    # Tier 1: delegate to running server (DB already warm)
    try:
        result = try_serve(op)
    except ServeRequest4xx as e:
        print_serve_4xx(e)
        return 1
    if result is not None:
        stats = dict_to_stats(result)

    # Tier 2: read from cache (avoids 1.5s cold-open on large DBs)
    if stats is None:
        stats = read_stats_cache(db_path=db)

    # Tier 3: local execution (cold-open fallback)
    if stats is None:
        try:
            stats = execute(op)
        except FileNotFoundError as e:
            status.error(str(e), hint="Run 'siftd ingest' to create it.")
            return 1

    # JSON output
    if args.json:
        from siftd.api import embeddings_available

        out = {
            "db_path": str(stats.db_path),
            "db_size_bytes": stats.db_size_bytes,
            "counts": {
                "conversations": stats.counts.conversations,
                "prompts": stats.counts.prompts,
                "responses": stats.counts.responses,
                "tool_calls": stats.counts.tool_calls,
                "harnesses": stats.counts.harnesses,
                "workspaces": stats.counts.workspaces,
                "tools": stats.counts.tools,
                "models": stats.counts.models,
                "ingested_files": stats.counts.ingested_files,
            },
            "harnesses": [
                {"name": h.name, "source": h.source, "log_format": h.log_format}
                for h in stats.harnesses
            ],
            "harness_counts": [
                {"name": hc.name, "conversations": hc.conversation_count}
                for hc in stats.harness_counts
            ],
            "top_workspaces": [
                {
                    "path": w.path,
                    "conversation_count": w.conversation_count,
                    "last_activity": w.last_activity,
                }
                for w in stats.top_workspaces
            ],
            "models": stats.models,
            "top_tools": [
                {"name": t.name, "usage_count": t.usage_count}
                for t in stats.top_tools
            ],
            "token_coverage": {
                "responses": stats.token_coverage.responses,
                "with_tokens": stats.token_coverage.with_tokens,
                "pct_with_tokens": stats.token_coverage.pct_with_tokens,
                "by_harness": [
                    {
                        "name": h.name,
                        "responses": h.responses,
                        "with_tokens": h.with_tokens,
                        "pct_with_tokens": h.pct_with_tokens,
                    }
                    for h in stats.token_coverage.by_harness
                ],
            },
            "top_tags": [
                {"name": t.name, "count": t.count} for t in stats.top_tags
            ],
            "activity_window": {
                "earliest": stats.activity_window[0],
                "latest": stats.activity_window[1],
            },
            "last_ingest_at": stats.last_ingest_at,
            "features": {
                "embeddings": embeddings_available(),
            },
        }
        print(json.dumps(out, indent=2))
        return 0

    from siftd.output.listing import StatusReport
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    report = StatusReport()
    report.preamble(
        {
            "Database": str(stats.db_path),
            "Size": f"{stats.db_size_bytes / 1024:.1f} KB",
        }
    )
    # Counts join the amber metric thread (consistent with the query/peek lists
    # and the ingest summary table). Conversations — the primary entity every
    # other count is a child or dimension of — takes the bright grand-total tier.
    report.section(
        "Counts",
        {
            "Conversations": [(str(stats.counts.conversations), ds.metric_strong)],
            "Prompts": [(str(stats.counts.prompts), ds.metric)],
            "Responses": [(str(stats.counts.responses), ds.metric)],
            "Tool calls": [(str(stats.counts.tool_calls), ds.metric)],
            "Harnesses": [(str(stats.counts.harnesses), ds.metric)],
            "Workspaces": [(str(stats.counts.workspaces), ds.metric)],
            "Tools": [(str(stats.counts.tools), ds.metric)],
            "Models": [(str(stats.counts.models), ds.metric)],
            "Ingested files": [(str(stats.counts.ingested_files), ds.metric)],
        },
    )
    report.section(
        "Harnesses",
        [(h.name, f"({h.source}, {h.log_format})") for h in stats.harnesses],
    )
    workspaces = []
    for w in stats.top_workspaces:
        last_activity = fmt_timestamp(w.last_activity)
        last_str = f" (last {last_activity})" if last_activity else ""
        workspaces.append((w.path, f"{w.conversation_count} conversations{last_str}"))
    report.section("Workspaces (top 10)", workspaces)
    report.lines_section("Models", list(stats.models))
    report.section(
        "Tools (top 10 by usage)",
        [(t.name, [(str(t.usage_count), ds.metric)]) for t in stats.top_tools],
    )
    coverage = [
        (
            "Responses with tokens",
            f"{stats.token_coverage.with_tokens}/{stats.token_coverage.responses} "
            f"({stats.token_coverage.pct_with_tokens:.2f}%)",
        ),
    ]
    coverage.extend(
        (h.name, f"{h.with_tokens}/{h.responses} ({h.pct_with_tokens:.2f}%)")
        for h in stats.token_coverage.by_harness
    )
    report.section("Token Coverage", coverage)

    earliest, latest = stats.activity_window
    if earliest or latest:
        earliest_fmt = fmt_timestamp(earliest)
        latest_fmt = fmt_timestamp(latest)
        window = None
        if earliest_fmt and latest_fmt:
            window = f"{earliest_fmt} -> {latest_fmt}"
        elif earliest_fmt:
            window = f"{earliest_fmt} -> (unknown)"
        elif latest_fmt:
            window = f"(unknown) -> {latest_fmt}"
        if window is not None:
            report.section("Activity window", {"Conversations": window})

    if stats.harness_counts:
        report.section(
            "Harness activity",
            [(hc.name, [(str(hc.conversation_count), ds.metric)]) for hc in stats.harness_counts],
        )

    if stats.top_tags:
        report.section(
            "Tags (top 5)",
            [(tag.name, [(str(tag.count), ds.metric)]) for tag in stats.top_tags],
        )

    if stats.last_ingest_at:
        report.section("Ingest", {"Last ingest": fmt_timestamp(stats.last_ingest_at)})

    from siftd.api import embeddings_available

    report.section(
        "Features",
        {
            "Embeddings": "installed"
            if embeddings_available()
            else "not installed (run: siftd install embed)"
        },
    )
    report.render()
    return 0


def cmd_workspaces(args) -> int:
    """List workspaces with conversation counts."""
    from painted import Fidelity

    from siftd.api.dispatch import Operation, execute
    from siftd.output import fmt_timestamp, fmt_workspace
    from siftd.serve.client import ServeRequest4xx
    from siftd.serve.delegation import print_serve_4xx, try_serve

    db = resolve_db(args)
    limit = args.limit if args.limit > 0 else 10000

    op = Operation(
        path="/api/v1/workspaces",
        method="GET",
        fn=list_workspaces,
        params={"db_path": db, "n": limit},
        render_method="raw",
        fidelity=Fidelity(),
        db=db,
    )

    rows = None

    # Try serve delegation
    try:
        result = try_serve(op)
    except ServeRequest4xx as e:
        print_serve_4xx(e)
        return 1
    if result is not None and isinstance(result, dict) and "workspaces" in result:
        rows = [
            {
                "id": w.get("id"),
                "path": w["path"],
                "git_remote": w.get("git_remote"),
                "convs": w["conversations"],
                "last_activity": w.get("last_activity"),
            }
            for w in result["workspaces"]
        ]

    if rows is None:
        try:
            rows = execute(op)
        except FileNotFoundError as e:
            if args.json:
                print("[]")
                return 0
            status.error(str(e), hint="Run 'siftd ingest' to create it.")
            return 1

    if args.json:
        out = [
            {
                "id": row["id"],
                "path": row["path"],
                "git_remote": row["git_remote"],
                "conversations": row["convs"],
                "last_activity": row["last_activity"],
            }
            for row in rows
        ]
        print(json.dumps(out, indent=2))
        return 0

    if not rows:
        status.info("No workspaces found.")
        return 0

    for row in rows:
        name = fmt_workspace(row["path"])
        last_activity = fmt_timestamp(row["last_activity"])
        last_str = f"  last {last_activity}" if last_activity else ""
        print(f"{name}  {row['convs']} conversations{last_str}")

    return 0


def cmd_path(args) -> int:
    """Show XDG paths."""
    print(f"Data directory:   {data_dir()}")
    print(f"Config directory: {config_dir()}")
    print(f"Cache directory:  {cache_dir()}")
    print(f"Database:         {db_path()}")
    return 0


def cmd_config(args) -> int:
    """View or modify config settings."""
    from siftd.config import (
        _validate_config,
        append_config_list,
        get_config,
        get_tag_prefixes,
        load_config,
        remove_config_list,
        set_config,
    )

    # siftd config path
    if args.action == "path":
        print(config_file())
        return 0

    # siftd config tag-prefixes [--json]
    if args.action == "tag-prefixes":
        prefixes = get_tag_prefixes()
        if getattr(args, "json", False):
            print(json.dumps(prefixes, indent=2, sort_keys=True))
            return 0
        if not prefixes:
            status.info("No tag prefixes defined.")
            return 0
        from siftd.output.listing import print_definitions

        # Sorted name → prefix mapping, one aligned pair per line.
        print_definitions([(name, prefixes[name]) for name in sorted(prefixes)])
        return 0

    # siftd config get <key>
    if args.action == "get":
        if not args.key:
            print("Usage: siftd config get <key>")
            print("Example: siftd config get serve.host")
            return 1
        value = get_config(args.key)
        if value is None:
            status.error(f"Key not set: {args.key}")
            return 1
        print(value)
        return 0

    # siftd config set <key> <value>
    if args.action == "set":
        if not args.key or not args.value:
            print("Usage: siftd config set <key> <value>")
            print("Example: siftd config set serve.port 9090")
            return 1
        try:
            set_config(args.key, args.value)
        except ValueError as exc:
            status.error(str(exc))
            return 1
        # Re-read to show stored value (confirms type coercion)
        stored = get_config(args.key)
        status.confirm(f"Set {args.key} = {stored}")
        return 0

    # siftd config append <key> <value>
    if args.action == "append":
        if not args.key or args.value is None:
            print("Usage: siftd config append <key> <value>")
            print("Example: siftd config append adapters.claude_code.locations ~/.claude/projects")
            return 1
        try:
            changed = append_config_list(args.key, args.value)
        except ValueError as exc:
            status.error(str(exc))
            return 1
        if changed:
            status.confirm(f"Appended {args.value} to {args.key}")
        else:
            status.info(f"Value already present for {args.key}")
        return 0

    # siftd config remove <key> <value>
    if args.action == "remove":
        if not args.key or args.value is None:
            print("Usage: siftd config remove <key> <value>")
            print("Example: siftd config remove adapters.claude_code.locations ~/.claude/projects")
            return 1
        try:
            changed = remove_config_list(args.key, args.value)
        except ValueError as exc:
            status.error(str(exc))
            return 1
        if not changed:
            status.error(f"Value not found for {args.key}: {args.value}")
            return 1
        status.confirm(f"Removed {args.value} from {args.key}")
        return 0

    # siftd config (show all)
    path = config_file()
    if not path.exists():
        status.info("No config file found.", hint=f"Create one at: {path}")
        return 0

    doc = load_config()
    _validate_config(doc)
    print(path.read_text().strip())
    return 0


def cmd_adapters(args) -> int:
    """List discovered adapters."""
    from siftd.api import list_adapters

    adapters = list_adapters()

    if not adapters:
        if args.json:
            print("[]")
        else:
            status.info("No adapters found.")
        return 0

    # JSON output
    if args.json:
        out = [
            {
                "name": a.name,
                "origin": a.origin,
                "locations": a.locations,
                "source_path": a.source_path,
                "entrypoint": a.entrypoint,
            }
            for a in adapters
        ]
        print(json.dumps(out, indent=2))
        return 0

    from siftd.output import print_table

    str_rows = [
        [a.name, a.origin, ", ".join(a.locations) if a.locations else "-"]
        for a in adapters
    ]
    print_table(["NAME", "ORIGIN", "LOCATIONS"], str_rows)

    return 0


def build_meta_parser(subparsers) -> None:
    """Add 'config' and 'adapters' subparsers."""
    # config
    p_config = subparsers.add_parser(
        "config",
        help="View or modify config settings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd config                        # show all config
  siftd config path                   # show config file path
  siftd config get serve.host             # get specific value
  siftd config set serve.port 9090        # set value
  siftd config append adapters.claude_code.locations ~/.claude/projects
  siftd config remove adapters.claude_code.locations ~/.claude/projects
  siftd config tag-prefixes               # show resolved tag-prefix table
  siftd config tag-prefixes --json        # same, as JSON""",
    )
    p_config.add_argument(
        "action",
        nargs="?",
        choices=["get", "set", "path", "append", "remove", "tag-prefixes"],
        help="Action to perform",
    )
    p_config.add_argument("key", nargs="?", help="Config key (dotted path, e.g., serve.host)")
    p_config.add_argument(
        "value",
        nargs="?",
        help="Value to use (for 'set', 'append', 'remove')",
    )
    p_config.add_argument(
        "--json", action="store_true",
        help="JSON output (currently used by 'tag-prefixes')",
    )
    p_config.set_defaults(func=cmd_config)

    # adapters
    p_adapters = subparsers.add_parser("adapters", help="List discovered adapters")
    p_adapters.add_argument("--json", action="store_true", help="Output as JSON")
    p_adapters.set_defaults(func=cmd_adapters)
