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


# The one-line summary the masthead carries. The root parser sets no
# ``description``, so the summary appears exactly once (in the masthead) —
# single-sourced here for both the ``--help`` masthead and the ``--version``
# sentence.
_SUMMARY = "Aggregate and query LLM conversation logs"


def _render_block_to_str(block) -> str:
    """Render a painted ``Block`` to a string, ANSI-gated on the real stdout.

    The root ``--help`` is assembled by wrapping argparse's plain body with
    rendered brand blocks (masthead above, lane legend + footer below), so the
    blocks must become strings. ``should_use_ansi(sys.stdout)`` keys the colour
    to the eventual destination even though the block renders into a buffer.
    """
    import io

    from painted import print_block

    from siftd.output.common import should_use_ansi

    buf = io.StringIO()
    print_block(block, buf, use_ansi=should_use_ansi(sys.stdout))
    return buf.getvalue()


def _lane_command_segments(cmds: str):
    """Style a lane's command list: names ``secondary``, ``·`` separators muted."""
    from painted import current_palette

    from siftd.output.theme import domain_styles

    p = current_palette()
    secondary = domain_styles().summary
    segments: list[tuple[str, object]] = []
    for i, cmd in enumerate(cmds.split()):
        if i:
            segments.append((" · ", p.muted))
        segments.append((cmd, secondary))
    return segments


def _root_masthead_block(*, as_ascii: bool):
    """The masthead: the ``sift▪d`` mark + version + summary on one line.

    The mark leads, the version recedes (muted), the summary follows (secondary)
    set off by an em-dash (``-`` on a non-Unicode stream). The only place the
    summary appears, so a ``grep`` for it stays single-hit.
    """
    from painted import current_palette

    from siftd.output.mark import wordmark_segments
    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles

    p = current_palette()
    dash = " - " if as_ascii else " — "
    segments = wordmark_segments(as_ascii=as_ascii) + [
        (" ", None),
        (_get_version(), p.muted),
        (dash, p.muted),
        (_SUMMARY, domain_styles().summary),
    ]
    line = row_line(segments)
    return line.to_block(line.width)


def _root_lanes_block():
    """The lane legend: a muted ``lanes:`` intro over the label→commands grid.

    Lane labels take the structure weight (bold cream); the grid recurses the
    same ``definitions`` atom the report surfaces use. The plain text is
    byte-identical to the former epilog, so the listing reads unchanged when
    colour is stripped.
    """
    from painted import current_palette, join_vertical

    from siftd.output.listing import definitions, lines

    p = current_palette()
    letters = p.text.merge(p.accent)  # bold cream — the structure role
    intro = lines(["lanes:"], indent=0, style=p.muted)
    grid = definitions(
        [(name, _lane_command_segments(cmds)) for name, cmds in _LANES],
        indent=2,
        gutter=2,
        label_style=letters,
    )
    return join_vertical(intro, grid)


def _root_footer_block():
    """The footer: the ``--help`` pointer (typed command in the literal hue) over
    the hidden-plumbing line, both muted — plain text unchanged from the epilog.
    """
    from painted import current_palette, join_vertical

    from siftd.output.listing import lines
    from siftd.output.theme import domain_styles

    p = current_palette()
    pointer = lines(
        [[
            ("Run '", p.muted),
            ("siftd <command> --help", domain_styles().code),
            ("' for details.", p.muted),
        ]],
        indent=0,
    )
    hidden = lines(
        ["Advanced (hidden): " + ", ".join(sorted(_PLUMBING))],
        indent=0,
        style=p.muted,
    )
    return join_vertical(pointer, hidden)


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


class _RootParser(argparse.ArgumentParser):
    """Root parser whose ``--help`` leads with the brand mark.

    argparse owns the body (usage, the command listing, options); this wraps it
    with the rendered masthead above and the styled lane legend + footer below —
    the Phase-2 brand layer. Leaf/branch help stays stock argparse for now. All
    three help entry points (``siftd``, ``siftd -h``, ``siftd --help``) route
    through ``format_help``.
    """

    def format_help(self) -> str:
        from siftd.output.common import prefers_ascii

        as_ascii = prefers_ascii(sys.stdout)
        masthead = _render_block_to_str(_root_masthead_block(as_ascii=as_ascii))
        body = super().format_help()
        lanes = _render_block_to_str(_root_lanes_block())
        footer = _render_block_to_str(_root_footer_block())
        return (
            masthead.rstrip("\n") + "\n\n"
            + body.strip("\n") + "\n\n"
            + lanes.rstrip("\n") + "\n\n"
            + footer.rstrip("\n") + "\n"
        )


class _VersionAction(argparse.Action):
    """``--version`` → the branded lockup + capability line, then exit."""

    def __init__(
        self,
        option_strings,
        dest=argparse.SUPPRESS,
        default=argparse.SUPPRESS,
        help=None,  # noqa: A002 — argparse Action contract uses `help`
    ):
        super().__init__(
            option_strings=option_strings, dest=dest, default=default, nargs=0, help=help
        )

    def __call__(self, parser, namespace, values, option_string=None):
        _render_version()
        parser.exit()


def _render_version() -> None:
    """Print the ``--version`` surface to stdout.

    The ``sift▪d`` lockup (interactive only — piped/ASCII output goes straight to
    the facts, where the collapsed mark would just double the sentence's "siftd"),
    the version sentence, and a capability line (python · sqlite · embeddings).
    """
    import sqlite3

    from painted import current_palette, join_vertical, print_block

    from siftd.api import embeddings_available
    from siftd.output.common import prefers_ascii, should_use_ansi
    from siftd.output.mark import wordmark
    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles

    as_ascii = prefers_ascii()
    p = current_palette()
    ds = domain_styles()
    dash = " - " if as_ascii else " — "

    sentence = row_line([
        ("siftd ", ds.summary),
        (_get_version(), p.muted),
        (dash, p.muted),
        (_SUMMARY, ds.summary),
    ])
    ready = embeddings_available()
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    capability = row_line([
        ("python ", p.muted), (py, p.muted), (" · ", p.muted),
        ("sqlite ", p.muted), (sqlite3.sqlite_version, p.muted), (" · ", p.muted),
        ("embeddings ", p.muted),
        ("ready" if ready else "not installed", p.success if ready else p.muted),
    ])

    blocks = []
    if not as_ascii:
        wm = wordmark()
        blocks.append(wm.to_block(wm.width))
    blocks.extend([
        sentence.to_block(sentence.width),
        capability.to_block(capability.width),
    ])
    print_block(join_vertical(*blocks), use_ansi=should_use_ansi())


def main(argv=None) -> int:
    _configure_cli_logging()
    _relax_output_encoding()
    # Apply siftd's NORD palette process-wide so every painted surface — status,
    # query, search, show, peek, tables, doctor — renders in one theme. Setter
    # semantics: persists for the rest of the process. The print_block sites
    # honour TTY + NO_COLOR (output/common.should_use_ansi) — painted itself only
    # checks isatty — so the palette is inert when piped or colour is disabled.
    # Imported lazily to keep painted off the module-import path.
    from painted import use_theme

    from siftd.output import status
    from siftd.output.theme import siftd_theme

    use_theme(siftd_theme)
    # One glyph-degradation control point (the icon twin of the theme lever):
    # use_theme installed the ambient IconSet (the default Unicode set); override
    # it to ASCII when stdout can't render Unicode (a pipe or a LANG=C TTY) so
    # every glyph consumer — the search rank rail, spinners, rules — degrades from
    # here rather than threading an ascii flag through each call. prefers_ascii
    # keys off the live sys.stdout; a per-stream exception (a stderr surface)
    # scope-overrides with its own use_icons.
    from siftd.output.common import prefers_ascii

    if prefers_ascii():
        from painted import ASCII_ICONS, use_icons

        use_icons(ASCII_ICONS)
    # No description/epilog: the brand masthead (the summary's only home) and the
    # styled lane legend + footer are rendered by _RootParser.format_help, which
    # wraps argparse's body. _SUMMARY is single-sourced for the masthead so a
    # grep for it stays single-hit.
    parser = _RootParser(prog="siftd")
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="show program's version number and exit",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"Database path (default: {db_path()})",
    )

    # parser_class is pinned to vanilla ArgumentParser: add_subparsers defaults it
    # to the parent's class, which would make every subcommand a _RootParser and
    # leak the brand masthead/lanes onto leaf/branch --help. Root-only is the
    # Phase-2 scope; leaf/branch help stays stock argparse until the help adapter.
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", parser_class=argparse.ArgumentParser
    )

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
