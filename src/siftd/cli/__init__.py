"""CLI for siftd - conversation log aggregator."""

import argparse
import logging
import sys

from siftd.api import SchemaUpgradeRequiredError
from siftd.cli._common import _get_version
from siftd.cli.data import build_data_parser
from siftd.cli.db import build_db_parser
from siftd.cli.export import build_export_parser
from siftd.cli.install import build_install_parser
from siftd.cli.meta import build_meta_parser
from siftd.cli.peek import build_peek_parser
from siftd.cli.query import build_query_parser
from siftd.cli.search import build_search_parser
from siftd.cli.serve import build_serve_parser
from siftd.cli.sessions import build_sessions_parser
from siftd.cli.tags import build_tags_parser
from siftd.cli.upgrade import build_upgrade_parser
from siftd.paths import db_path


def _configure_cli_logging() -> None:
    """Route siftd.* INFO logs to stderr so users see auto-upgrade / migration progress.

    Idempotent: if a handler is already attached (e.g. main() called twice in
    the same process by tests), don't add another. Uses %(message)s so the
    output blends with siftd's plain print() lines.
    """
    logger = logging.getLogger("siftd")
    if not any(getattr(h, "_siftd_cli", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._siftd_cli = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)


def main(argv=None) -> int:
    _configure_cli_logging()
    parser = argparse.ArgumentParser(
        prog="siftd",
        description="Aggregate and query LLM conversation logs",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"siftd {_get_version()}",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"Database path (default: {db_path()})",
    )

    subparsers = parser.add_subparsers(dest="command")

    build_sessions_parser(subparsers)
    build_meta_parser(subparsers)
    build_db_parser(subparsers)
    build_tags_parser(subparsers)
    build_query_parser(subparsers)
    build_data_parser(subparsers)
    build_search_parser(subparsers)
    build_install_parser(subparsers)
    build_peek_parser(subparsers)
    build_export_parser(subparsers)
    build_serve_parser(subparsers)
    build_upgrade_parser(subparsers)

    args = parser.parse_args(argv)
    if not hasattr(args, "func") or args.func is None:
        parser.print_help()
        return 0
    try:
        exit_code = args.func(args)
    except KeyboardInterrupt:
        # Exit cleanly on Ctrl+C (130 = 128 + SIGINT)
        return 130
    except SchemaUpgradeRequiredError as e:
        # Auto-upgrade path can fire from any read-only subcommand. Catch here
        # so users see the friendly message rather than a Python traceback.
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Post-command: passive update check (non-blocking)
    from siftd.cli.upgrade import maybe_print_notice, maybe_start_check

    maybe_print_notice()
    maybe_start_check()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
