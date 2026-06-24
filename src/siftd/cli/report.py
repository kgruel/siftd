"""CLI handler for the `report` command — run saved parameterized SQL queries.

Extracted from `query sql` (CLI UX audit, read-surface slice). A named-SQL
report composes from neither the conversation list nor the detail view; it only
*looked* unified by squatting `query`'s positional slot. `query sql [name]`
remains as a deprecated alias that routes here.
"""

from pathlib import Path


def run_report(name: str | None, var: list[str] | None, db: Path | None) -> int:
    """List or run .sql report files.

    name=None lists available reports; otherwise runs the named report with
    optional `$KEY` substitution from KEY=VALUE entries in `var`.
    """
    from siftd.api import QueryError, list_query_files, run_query_file
    from siftd.output import print_table, status
    from siftd.paths import queries_dir

    # List mode: no name provided.
    if not name:
        query_files = list_query_files()
        if not query_files:
            status.info(f"No reports found in {queries_dir()}")
            return 0
        for qf in query_files:
            suffix = f"  (vars: {', '.join(qf.variables)})" if qf.variables else "  (no vars)"
            print(f"{qf.name}{suffix}")
        return 0

    # Run mode: parse variables.
    variables = None
    if var:
        variables = {}
        for v in var:
            if "=" not in v:
                status.error(f"Invalid --var format (expected key=value): {v}")
                return 1
            key, value = v.split("=", 1)
            variables[key] = value

    try:
        result = run_query_file(name, variables, db_path=db)
    except FileNotFoundError as e:
        status.error(str(e), hint="Run 'siftd ingest' to create it.")
        return 1
    except QueryError as e:
        msg = str(e)
        if "Query file not found" in msg:
            # Report not found — the LIVE path: run_query_file raises QueryError
            # (not FileNotFoundError) for a missing report. status.error frames it;
            # the available report names ride a lines() block (a callout hint
            # flattens newlines), both to stderr so a piped stdout stays clean —
            # the cli._common.print_ambiguous_error idiom.
            import sys

            from painted import print_block

            from siftd.output.common import should_use_ansi
            from siftd.output.listing import lines

            status.error(f"Report not found: {name}", hint="Available reports:")
            names = [qf.name for qf in list_query_files()]
            if names:
                print_block(lines(names), sys.stderr, use_ansi=should_use_ansi(sys.stderr))
        elif "Missing variables" in msg:
            import re

            match = re.search(r"Missing variables: (.+)", msg)
            missing = match.group(1).split(", ") if match else []
            status.error(
                f"Report '{name}' requires variables not provided: {', '.join(missing)}",
                hint=f"siftd report {name} " + " ".join(f"--var {v}=<value>" for v in missing),
            )
        else:
            status.error(msg)
        return 1

    # Format output.
    if result.rows:
        str_rows = [[str(v) if v is not None else "" for v in row] for row in result.rows]
        print_table(result.columns, str_rows)
    else:
        status.info("No rows returned.")

    return 0


def cmd_report(args) -> int:
    """Run a saved SQL report (or list available reports)."""
    from siftd.cli._common import resolve_db

    db = resolve_db(args) if args.name else (Path(args.db) if args.db else None)
    return run_report(args.name, args.var, db)


def build_report_parser(subparsers) -> None:
    """Add the 'report' subparser."""
    import argparse

    p = subparsers.add_parser(
        "report",
        help="Run saved SQL reports (parameterized .sql queries)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Run saved SQL reports. Built-in reports work out of the box;
.sql files in ~/.config/siftd/queries/ add your own or override a built-in
(same filename wins).

A report is a named .sql file with optional $KEY placeholders. Run without a
name to list available reports. To customize a built-in, copy it first:
  siftd copy query cost            # copy the 'cost' report to your queries dir

examples:
  siftd report                          # list available reports
  siftd report cost                     # run the 'cost' report
  siftd report cost --var ws=proj       # run with variable substitution""",
    )
    p.add_argument("name", nargs="?", help="Report name (omit to list available reports)")
    p.add_argument(
        "--var", action="append", metavar="KEY=VALUE", help="Substitute $KEY with VALUE in the report SQL"
    )
    p.set_defaults(func=cmd_report)
