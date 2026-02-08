"""CLI for siftd - conversation log aggregator."""

import argparse
import sys

from siftd.cli_common import _get_version
from siftd.cli_data import build_data_parser
from siftd.cli_export import build_export_parser
from siftd.cli_install import build_install_parser
from siftd.cli_meta import build_meta_parser
from siftd.cli_peek import build_peek_parser
from siftd.cli_query import build_query_parser
from siftd.cli_search import build_search_parser
from siftd.cli_sessions import build_sessions_parser
from siftd.cli_tags import build_tags_parser
from siftd.paths import db_path


def main(argv=None) -> int:
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
    build_tags_parser(subparsers)
    build_query_parser(subparsers)
    build_data_parser(subparsers)
    build_search_parser(subparsers)
    build_install_parser(subparsers)
    build_peek_parser(subparsers)
    build_export_parser(subparsers)

    args = parser.parse_args(argv)
    if not hasattr(args, "func") or args.func is None:
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Exit cleanly on Ctrl+C (130 = 128 + SIGINT)
        return 130


if __name__ == "__main__":
    sys.exit(main())
