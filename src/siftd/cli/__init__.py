"""CLI for siftd - conversation log aggregator."""

import argparse
import logging
import sys

from siftd.api import SchemaUpgradeRequiredError
from siftd.cli._common import _get_version
from siftd.cli.auth import build_auth_parser
from siftd.cli.data import build_data_parser
from siftd.cli.db import build_db_parser
from siftd.cli.export import build_export_parser
from siftd.cli.id_cmd import build_id_parser
from siftd.cli.install import build_install_parser
from siftd.cli.meta import build_meta_parser
from siftd.cli.peek import build_peek_parser
from siftd.cli.query import build_query_parser
from siftd.cli.report import build_report_parser
from siftd.cli.search import build_search_parser
from siftd.cli.serve import build_serve_parser
from siftd.cli.sessions import build_sessions_parser
from siftd.cli.show import build_show_parser
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


def _relax_output_encoding() -> None:
    """Let stdout/stderr tolerate content the terminal's codec can't encode.

    Conversation bodies carry arbitrary Unicode — em-dashes, emoji, CJK. On a
    strict-ASCII stream (LANG=C, or PYTHONIOENCODING=ascii) Python's default
    ``errors='strict'`` raises ``UnicodeEncodeError`` mid-render, crashing query,
    peek, search and every other content surface. Switching the *stream's* error
    handler to ``backslashreplace`` degrades the offending character to a visible,
    reversible escape (``\\u2014``) instead of crashing.

    This is application I/O policy and belongs here, at the entry point, beside
    ``use_theme`` — a stream's error handler is owned by whoever owns the stream
    (us), so painted (correctly) never reconfigures one it's handed. It's also the
    *only* correct lever for arbitrary user content: ``prefers_ascii`` glyph
    routing governs only the decorative characters siftd's own renderer emits,
    not data passing through. ``encoding`` is left untouched (only the error
    handler changes), and machine output is unaffected — ``--format json``
    serializes ASCII-safe via ``json.dumps``' ``ensure_ascii`` default.

    Best-effort: pytest capture and some redirects replace the streams with
    objects that lack ``reconfigure`` (skipped) or that reject it (caught); either
    way the stream keeps its existing policy rather than the hardening step itself
    becoming a crash source.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


# Top-level command lanes — the story `siftd --help` tells. The lane legend
# rides the epilog; the per-command descriptions remain in the listing above it.
_LANES: tuple[tuple[str, str], ...] = (
    ("EXPLORE", "query search show report peek"),
    ("CURATE", "tag export"),
    ("INGEST", "ingest adapters"),
    ("MAINTAIN", "doctor db"),
    ("SHARE", "serve auth"),
    ("SETUP", "install config"),
)
# Plumbing: hook machinery + power-user tools. Hidden from the lane view but
# fully runnable — they stay registered; only the help listing drops them.
_PLUMBING: frozenset[str] = frozenset(
    {"register", "session-id", "id", "backfill", "migrate", "copy", "upgrade"}
)


def _lanes_epilog() -> str:
    width = max(len(name) for name, _ in _LANES)
    lines = ["lanes:"]
    lines += [f"  {name:<{width}}  {cmds.replace(' ', ' · ')}" for name, cmds in _LANES]
    lines += [
        "",
        "Run 'siftd <command> --help' for details.",
        "Advanced (hidden): " + ", ".join(sorted(_PLUMBING)),
    ]
    return "\n".join(lines)


def _hide_plumbing(subparsers) -> None:
    """Drop plumbing commands from the parent --help listing without
    unregistering them: they stay in ``choices``, so ``siftd <plumbing>`` and the
    SessionStart hook's ``siftd register ...`` still parse and run. Filters
    argparse's ``_choices_actions`` (the help-display list); guarded so a future
    argparse internal change degrades to listing them rather than crashing.
    """
    actions = getattr(subparsers, "_choices_actions", None)
    if actions is None:
        return
    subparsers._choices_actions = [
        a for a in actions if getattr(a, "dest", None) not in _PLUMBING
    ]


def main(argv=None) -> int:
    _configure_cli_logging()
    _relax_output_encoding()
    # Apply siftd's NORD palette process-wide so every painted surface — status,
    # query, search, show, peek, tables, doctor — renders in one theme. Setter
    # semantics: persists for the rest of the process. painted strips color for
    # non-TTY / NO_COLOR output, so this is inert when piped. Imported lazily to
    # keep painted off the module-import path.
    from painted import use_theme

    from siftd.output import status
    from siftd.output.theme import siftd_theme

    use_theme(siftd_theme)
    parser = argparse.ArgumentParser(
        prog="siftd",
        description="Aggregate and query LLM conversation logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_lanes_epilog(),
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

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # Registered in lane order so the help listing reads top-to-bottom as the
    # lanes do. Multi-command builders (data, meta, sessions) span lanes; the
    # epilog legend is the authoritative lane view. Plumbing verbs are hidden
    # from the listing by _hide_plumbing() below but remain fully runnable.
    build_query_parser(subparsers)
    build_show_parser(subparsers)
    build_report_parser(subparsers)
    build_search_parser(subparsers)
    build_peek_parser(subparsers)
    build_tags_parser(subparsers)
    build_export_parser(subparsers)
    build_data_parser(subparsers)  # ingest (+ backfill/migrate/copy hidden) + doctor
    build_meta_parser(subparsers)  # config + adapters
    build_db_parser(subparsers)
    build_serve_parser(subparsers)
    build_auth_parser(subparsers)
    build_install_parser(subparsers)
    build_sessions_parser(subparsers)  # register, session-id (hidden)
    build_id_parser(subparsers)  # hidden
    build_upgrade_parser(subparsers)  # hidden

    _hide_plumbing(subparsers)

    args, unknowns = parser.parse_known_args(argv)
    if unknowns:
        hint_fn = getattr(args, "_unknown_hint", None)
        suffix = hint_fn(unknowns) if hint_fn is not None else None
        msg = f"unrecognized arguments: {' '.join(unknowns)}"
        if suffix:
            msg = f"{msg}\n{suffix}"
        parser.error(msg)
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
        status.error(str(e))
        return 1

    # Post-command: passive update check (non-blocking)
    from siftd.cli.upgrade import maybe_print_notice, maybe_start_check

    maybe_print_notice()
    maybe_start_check()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
