"""One help grammar for every CLI surface — root, branch, and leaf.

The unifier behind the brand: a single ``render_help`` over a ``HelpPage`` that is
*derived* from a live argparse parser (``HelpPage.from_argparse``). Because
argparse already holds the groups, options, metavars, help and sub-commands, every
command — present and future — renders in the same grammar without hand-authoring,
and the bespoke root masthead/lanes wrapper dissolves into this one renderer.

The grammar (the contract already in ``theme.py`` — see the design concept):

- **the mark leads every surface** — the ``sift▪d`` lockup with the gold grain,
  carrying the version at the root and a ``›`` breadcrumb on every command below;
- **structure pops by weight** — group labels, the breadcrumb's command, and a
  sub-command name take bold cream (``palette.text.merge(palette.accent)``), never
  a hue;
- **literal tokens take the warm hue** — flags and the typed ``Run '… --help'``
  command in terracotta (``domain_styles().code``);
- **metavars and help recede** — secondary (``domain_styles().summary``);
- **connective tissue is muted** — ``usage:``, the breadcrumb chevron, the footer
  framing. (The design concept spends a *fainter* grey still on defaults/comments;
  here that folds into ``muted`` — the theme's "muted is the dim floor" law admits
  no fourth grey tier.)

The grain is the only colour the surface spends on identity; the rest is the
existing role vocabulary, so a help page reads as one designed system with the
status/query/search surfaces rather than as argparse's flat text. No new colour,
no new role. The two-column option/command rows reuse ``output.row.wrap_segments``
(the aligned-continuation word-wrap shared with the markdown body), so a long help
string wraps under its column instead of running off the line.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from typing import TextIO

    from painted import Block


@dataclass(frozen=True)
class OptionRow:
    """One option/positional: the invocation token, its metavar, and the help.

    ``token`` is the comma-joined option strings (``-w, --workspace``) or, for a
    positional, its metavar (``conversation_id``). ``metavar`` is the value
    placeholder for an option that takes one (``SUBSTR``, ``[FILTER]``), else
    empty. They are styled apart — token terracotta, metavar secondary — so the
    left column reads ``flag METAVAR`` in two roles.
    """

    token: str
    metavar: str
    help: str


@dataclass(frozen=True)
class CommandRow:
    """One sub-command in a branch's command listing (or a root lane): name + help."""

    name: str
    help: str


@dataclass(frozen=True)
class Group:
    """A titled group of rows — either option rows or command rows, never both.

    Option groups are argparse's argument groups (``filtering``, ``output``, …);
    command groups are a branch's sub-verbs or a root lane. ``is_command`` keys the
    label weight and the inter-group spacing (lanes/commands pack tight; option
    groups are blank-separated).
    """

    label: str
    options: tuple[OptionRow, ...] = ()
    commands: tuple[CommandRow, ...] = ()

    @property
    def is_command(self) -> bool:
        return bool(self.commands)


@dataclass(frozen=True)
class HelpPage:
    """The structure of any help surface — declared shape, no colour.

    ``path`` is the full command path (``("siftd",)`` / ``("siftd", "db",
    "restore")``); its tail drives the breadcrumb. ``version`` is set for the root
    only (the masthead carries it; below the root the breadcrumb leads instead).
    ``footer`` is the ``Run '… <command> --help'`` pointer (root + branch);
    ``hidden`` names the de-listed plumbing verbs (root only). ``epilog`` is
    argparse's raw epilog (the prose + hand-aligned examples), rendered verbatim.
    """

    path: tuple[str, ...]
    summary: str | None
    usage: str
    groups: tuple[Group, ...]
    version: str | None = None
    epilog: str | None = None
    footer: str | None = None
    hidden: tuple[str, ...] = field(default_factory=tuple)
    # Root only: the lane legend as (label, command-names), rendered terse and
    # inline (``EXPLORE  query · search · …``). The root spends no per-command
    # descriptions or OPTIONS block — the breadcrumb summary carries each
    # command's one-liner one level down.
    lanes: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)

    @classmethod
    def from_argparse(
        cls,
        parser: argparse.ArgumentParser,
        *,
        version: str | None = None,
        summary: str | None = None,
        lanes: tuple[tuple[str, str], ...] | None = None,
        hidden: tuple[str, ...] = (),
    ) -> HelpPage:
        """Derive a ``HelpPage`` from a live parser.

        Three shapes off one parser:

        - **root** (``lanes`` given): a terse inline lane legend + the version +
          the hidden-plumbing line. No per-command descriptions, no OPTIONS block
          — the breadcrumb summary carries each command's one-liner one level down.
        - **branch** (owns sub-commands): a ``commands`` listing (name → one-liner)
          + a footer pointing one level in.
        - **leaf**: its argument groups as option groups + the epilog.

        Argument groups skip suppressed actions and the universal ``-h`` (it lives
        in usage). ``summary`` overrides ``parser.description`` (the root has no
        description; its summary is single-sourced by the caller).
        """
        import argparse

        path = tuple(parser.prog.split())
        summary = summary if summary is not None else (parser.description or None)
        usage = parser.format_usage()

        sub_action = next(
            (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )

        groups: list[Group] = []
        page_lanes: tuple[tuple[str, tuple[str, ...]], ...] = ()
        footer: str | None = None
        if lanes is not None:  # root — terse inline lanes; no command-groups/OPTIONS
            page_lanes = tuple(
                (label, tuple(c for c in cmds.split() if sub_action and c in sub_action.choices))
                for label, cmds in lanes
            )
            footer = f"Run '{' '.join(path)} <command> --help' for details."
        elif sub_action is not None:  # branch — a command listing + a deeper pointer
            rows = tuple(
                CommandRow(ca.dest, ca.help or "") for ca in sub_action._choices_actions
            )
            if rows:
                groups.append(Group(label="commands", commands=rows))
            groups.extend(_option_groups(parser))
            footer = f"Run '{' '.join(path)} <command> --help' for details."
        else:  # leaf — argument groups + epilog
            groups.extend(_option_groups(parser))

        return cls(
            path=path,
            summary=summary,
            usage=usage,
            groups=tuple(groups),
            version=version,
            epilog=parser.epilog or None,
            footer=footer,
            hidden=tuple(hidden),
            lanes=page_lanes,
        )


def _option_groups(parser: argparse.ArgumentParser) -> list[Group]:
    """Argparse argument groups → option ``Group``s (empty groups dropped).

    Skips suppressed actions, the auto ``-h`` (universal; shown in usage), and the
    sub-parsers action (rendered as a command group instead). Groups that share a
    title are MERGED into one section, in first-seen order — so a command can title
    a couple of argparse groups the same (e.g. the shared ``fidelity`` and
    ``navigation`` helpers both titled ``view``) and they read as one purpose
    rather than two adjacent headers. A title left with no rows contributes nothing.
    """
    import argparse

    fmt = parser._get_formatter()
    merged: dict[str, list[OptionRow]] = {}
    order: list[str] = []
    for g in parser._action_groups:
        # argparse's default positional title reads heavy; the house label is the
        # terser "arguments" (uppercased to ARGUMENTS by the group renderer).
        title = "arguments" if g.title == "positional arguments" else (g.title or "")
        for action in g._group_actions:
            if action.help is argparse.SUPPRESS:
                continue
            if isinstance(action, argparse._HelpAction | argparse._SubParsersAction):
                continue
            if title not in merged:
                merged[title] = []
                order.append(title)
            merged[title].append(_option_row(action, fmt))
    return [Group(label=t, options=tuple(merged[t])) for t in order]


def _option_row(action: argparse.Action, fmt: argparse.HelpFormatter) -> OptionRow:
    """Build an ``OptionRow`` from an argparse action, reusing argparse's own
    metavar derivation so the placeholder matches what argparse would print.

    Optionals: ``token`` = the comma-joined option strings, ``metavar`` = the
    formatted args (``SUBSTR``, ``[FILTER]``, ``A:B``; empty for a flag). A
    positional carries its metavar in ``token`` and no separate metavar. ``help``
    is expanded through argparse's own ``_expand_help`` so an escaped ``%%`` reads
    as a literal ``%`` (and any ``%(default)s`` substitutes) exactly as argparse
    would render it — taking ``action.help`` raw would leak the escape.
    """
    help_text = fmt._expand_help(action) if action.help else ""
    if action.option_strings:
        token = ", ".join(action.option_strings)
        if action.nargs == 0:
            metavar = ""
        else:
            default_mv = fmt._get_default_metavar_for_optional(action)
            metavar = fmt._format_args(action, default_mv)
        return OptionRow(token=token, metavar=metavar, help=help_text)
    default_mv = fmt._get_default_metavar_for_positional(action)
    token = fmt._metavar_formatter(action, default_mv)(1)[0]
    return OptionRow(token=token, metavar="", help=help_text)


# --- rendering --------------------------------------------------------------


class _AnsiBuffer(io.StringIO):
    """A ``StringIO`` that reports as a TTY so painted detects the *real* terminal's
    colour depth.

    argparse needs ``format_help`` to return a string, so the page is rendered into
    a buffer. painted keys colour depth off the buffer's ``isatty()`` — a plain
    buffer reports ``False`` → ``ColorDepth.NONE`` → forced-ANSI downsamples cream
    to white(37), gold to yellow(33), and mid-tones to a malformed ``\\x1b[38m`` that
    terminals render as the default (grey) foreground — the washed-out help. We only
    use this when the *destination* is already a colour TTY (``should_use_ansi``), so
    deferring to painted's env-based detection (``COLORTERM``/``TERM``) yields the
    same truecolour the rest of the CLI emits when writing straight to stdout.
    """

    def isatty(self) -> bool:
        return True


def render_help(page: HelpPage, *, stream: TextIO | None = None) -> str:
    """Render a ``HelpPage`` to a string, ANSI/ASCII keyed to ``stream``.

    ``stream`` (default ``sys.stdout``) decides colour (``should_use_ansi`` — TTY
    and not ``NO_COLOR``) and glyph form (``prefers_ascii`` — the grain/chevron
    degrade on a pipe or a ``LANG=C`` TTY). Width follows ``term_width`` (honoring
    ``COLUMNS``), matching argparse's own usage wrapping. Trailing whitespace is
    stripped per line so the block's right-pad never leaks into the output.
    """
    from painted import print_block

    from siftd.output.common import prefers_ascii, should_use_ansi, term_width

    out = stream if stream is not None else sys.stdout
    use_ansi = should_use_ansi(out)
    block = _compose(page, term_width(), prefers_ascii(out))
    # A tty-reporting buffer for the colour path so painted emits the terminal's
    # true depth (see _AnsiBuffer); a plain buffer for the piped/plain path.
    buf: io.StringIO = _AnsiBuffer() if use_ansi else io.StringIO()
    print_block(block, buf, use_ansi=use_ansi)
    text = "\n".join(line.rstrip() for line in buf.getvalue().split("\n"))
    return text.rstrip("\n") + "\n"


def _compose(page: HelpPage, width: int, as_ascii: bool):
    """Compose the page into one painted ``Block`` — sections, one blank between."""
    from painted import Block, join_vertical

    sections: list[Block] = [_masthead(page, width, as_ascii)]
    sections.append(_usage_block(page, width))
    if page.lanes:
        sections.append(_lanes_block(page.lanes))
    if page.groups:
        sections.append(_groups_block(page.groups, width))
    if page.epilog:
        sections.append(_epilog_block(page.epilog))
    if page.footer:
        sections.append(_footer_block(page, width))

    parts: list[Block] = []
    for sec in sections:
        if sec.height == 0:
            continue
        if parts:
            parts.append(Block.empty(0, 1))
        parts.append(sec)
    return join_vertical(*parts) if parts else Block.empty(0, 0)


def _lb(line):
    """A ``Line`` → a natural-width ``Block`` (no right-pad; see ``render_help``)."""
    return line.to_block(line.width)


def _masthead(page: HelpPage, width: int, as_ascii: bool):
    """The root masthead (mark · version · summary) or a command breadcrumb + summary."""
    from painted import current_palette, join_vertical

    from siftd.output.mark import breadcrumb_segments, wordmark_segments
    from siftd.output.row import row_line, wrap_segments
    from siftd.output.theme import domain_styles

    p = current_palette()
    ds = domain_styles()

    if page.version is not None:  # root — the mark carries the version + summary
        dash = " - " if as_ascii else " — "
        prefix = wordmark_segments(as_ascii=as_ascii) + [
            (" ", None),
            (page.version, p.muted),
            (dash, ds.faint),
        ]
        # Wrap the summary after the mark+version so the masthead honors width too
        # (it fits one line at normal widths; the summary wraps to column 0 on a
        # narrow terminal, consistent with the breadcrumb summary and every section).
        lines = wrap_segments([(page.summary or "", ds.summary)], width, prefix)
        return join_vertical(*[_lb(ln) for ln in lines])

    parts = [_lb(row_line(breadcrumb_segments(page.path[1:], as_ascii=as_ascii)))]
    if page.summary:
        parts += [_lb(ln) for ln in wrap_segments([(page.summary, ds.summary)], width)]
    return join_vertical(*parts)


def _lanes_block(lanes: tuple[tuple[str, tuple[str, ...]], ...]):
    """The terse inline lane legend (root): a muted ``lanes:`` intro over aligned
    ``LABEL  name · name · …`` rows — label bold cream, names secondary, ``·`` faint.

    The lane labels share one column (aligned to the widest), so the command lists
    start flush; the legend reads as one unit the way the design concept's root does.
    """
    from painted import current_palette, join_vertical
    from painted.core._text_width import display_width

    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles, structure_style

    p = current_palette()
    ds = domain_styles()
    structure = structure_style()
    labelw = max((display_width(label) for label, _ in lanes), default=0)

    rows = [_lb(row_line([("lanes:", p.muted)]))]
    for label, names in lanes:
        segs: list = [("  ", None), (label, structure), (" " * (labelw - display_width(label) + 2), None)]
        for i, name in enumerate(names):
            if i:
                segs.append((" · ", ds.faint))
            segs.append((name, ds.summary))
        rows.append(_lb(row_line(segs)))
    return join_vertical(*rows)


def _usage_block(page: HelpPage, width: int):
    """The usage line(s), rendered muted, re-wrapped part-aware for stability.

    argparse's ``format_usage`` wraps differently across Python versions (3.12 vs
    3.13+), which would make the snapshots version-specific. So the raw usage is
    flattened to its logical parts and re-wrapped here: each top-level ``[...]`` /
    ``(...)`` group stays whole (a mutex group never splits across a line), and
    continuations indent under the program name. Deterministic across versions,
    and the one place usage styling could grow.
    """
    from painted import current_palette, join_vertical

    from siftd.output.row import row_line

    p = current_palette()
    prog = " ".join(page.path)
    flat = " ".join(page.usage.split())  # collapse argparse's wrapping to one line
    lead = "usage: "
    body = flat[len(lead):] if flat.startswith(lead) else flat
    parts_str = body[len(prog):].lstrip() if body.startswith(prog) else body
    parts = _split_usage_parts(parts_str)

    prefix = f"{lead}{prog} "
    lines = _pack_usage(parts, prefix, " " * len(prefix), width)
    return join_vertical(*[_lb(row_line([(ln, p.muted)])) for ln in lines])


def _pack_usage(parts: list[str], prefix: str, cont: str, width: int) -> list[str]:
    """Greedily pack usage ``parts`` into width-respecting lines.

    Each part (a bracket group or bare token) joins the current line if it fits,
    else starts a fresh ``cont``-indented line. A part *wider than a whole line*
    (a mutex group on a narrow terminal) is the escape hatch: it word-wraps onto
    ``cont``-indented lines rather than overrunning the edge — so the usage never
    overflows, while a group that fits still stays whole.
    """
    from painted import Span, Style
    from painted.core._text_width import display_width

    from siftd.output.row import wrap_spans

    if not parts:
        return [prefix.rstrip()]
    plain = Style()
    lines = [prefix]
    for part in parts:
        last = lines[-1]
        sep = "" if last.endswith(" ") else " "
        if display_width(last) + len(sep) + display_width(part) <= width:
            lines[-1] = last + sep + part
        elif len(cont) + display_width(part) <= width or " " not in part:
            lines.append(cont + part)
        else:  # oversized part — wrap its words onto continuation lines
            avail = max(1, width - len(cont))
            for wl in wrap_spans([Span(part, plain)], avail):
                lines.append(cont + "".join(s.text for s in wl.spans))
    return [ln.rstrip() for ln in lines]


def _split_usage_parts(s: str) -> list[str]:
    """Split a flat usage string into top-level parts, keeping bracket groups whole.

    Splits on spaces only at bracket depth 0, so ``[-h]``, ``[-w SUBSTR]`` and a
    whole mutex group ``[--from-start | --from-end | --at-turn N]`` each stay a
    single part that the packer never breaks across a line.
    """
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch == " " and depth == 0:
            if cur:
                parts.append(cur)
                cur = ""
            continue
        if ch in "[(":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        cur += ch
    if cur:
        parts.append(cur)
    return parts


def _groups_block(groups: tuple[Group, ...], width: int):
    """All groups, one blank between — except consecutive command groups pack tight.

    Root lanes (and a branch's command listing) are command groups; they sit flush
    so the lane legend reads as one unit. Option groups are blank-separated for
    scanning. The single rule: a blank precedes a group unless it follows another
    command group.
    """
    from painted import Block, join_vertical

    parts: list[Block] = []
    for i, g in enumerate(groups):
        if i and not (g.is_command and groups[i - 1].is_command):
            parts.append(Block.empty(0, 1))
        parts.append(_group_block(g, width))
    return join_vertical(*parts)


def _group_block(group: Group, width: int):
    """One group: a bold-cream label over its aligned, wrapping rows."""
    from painted import join_vertical

    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles, structure_style

    ds = domain_styles()
    label = _lb(row_line([(group.label.upper(), structure_style())]))

    rows: list[tuple[list, str, list]] = []
    if group.is_command:
        for c in group.commands:
            rows.append(([(c.name, structure_style())], c.name, [(c.help, ds.summary)]))
    else:
        for o in group.options:
            left: list = [(o.token, ds.code)]
            left_text = o.token
            if o.metavar:
                left += [(" ", None), (o.metavar, ds.summary)]
                left_text = f"{o.token} {o.metavar}"
            rows.append((left, left_text, _style_help(o.help, ds)))
    return join_vertical(label, _rows_block(rows, width))


def _style_help(text: str, ds) -> list[tuple[str, object]]:
    """Split a help string into styled segments: flags terracotta, ``(default …)``
    faint, the rest secondary.

    A literal token a user could type back (a long ``--flag``) takes the warm
    literal hue inside prose; a trailing ``(default …)`` recedes to the faint tier;
    everything else is plain help. Conservative — only a *whole-word* long flag
    (one bounded by whitespace, so a glued token like ``--mode=thread`` is left
    intact and the wrap points never shift) and a bracketed default are recoloured.
    """
    import re

    pattern = re.compile(r"(?<!\S)--[a-z][\w-]+(?!\S)|\(default[^)]*\)")
    segs: list[tuple[str, object]] = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            segs.append((text[pos : m.start()], ds.summary))
        tok = m.group(0)
        segs.append((tok, ds.code if tok.startswith("--") else ds.faint))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:], ds.summary))
    return segs or [(text, ds.summary)]


def _rows_block(rows, width: int, *, indent: int = 2, gutter: int = 2):
    """Two-column rows: a styled left column, a wrapping help column aligned under it.

    Each row is ``(left_segments, left_text, help_segments)``. The left column is
    padded to the widest entry's display width; the help (already styled — flags,
    defaults) wraps to the remaining width with continuation lines indented to the
    column. Reuses ``output.row.wrap_segments`` (the aligned-continuation word-wrap),
    which preserves the help's inline styling across wrap boundaries.
    """
    from painted import Style, join_vertical
    from painted.core._text_width import display_width

    from siftd.output.row import wrap_segments

    plain = Style()
    leftw = max((display_width(lt) for _, lt, _ in rows), default=0)
    blocks = []
    for left_segs, left_text, help_segs in rows:
        align = " " * (leftw - display_width(left_text))
        first = [(" " * indent, plain), *left_segs, (align + " " * gutter, plain)]
        cont = [(" " * (indent + leftw + gutter), plain)]
        for ln in wrap_segments(help_segs, width, first, cont):
            blocks.append(_lb(ln))
    return join_vertical(*blocks)


def _epilog_block(epilog: str):
    """The epilog (prose + hand-aligned examples), kept verbatim but colorized.

    Layout is preserved (the examples are hand-tuned with aligned ``#`` comments —
    re-wrapping would break them). A line that *is* an example command (it starts
    with ``siftd`` or ``$``) is tokenized — the command cream, flags terracotta, a
    trailing ``# comment`` faint, the ``$`` prompt teal; every other line (prose,
    an ``examples:`` label) recedes to secondary. One leading/trailing blank line
    is trimmed so it spaces like any other section.
    """
    from painted import join_vertical

    from siftd.output.row import row_line
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    blocks = []
    for ln in epilog.strip("\n").split("\n"):
        stripped = ln.lstrip()
        if stripped.startswith(("siftd ", "$ ")):
            blocks.append(_lb(row_line(_style_example(ln, ds))))
        else:
            blocks.append(_lb(row_line([(ln, ds.summary if ln else None)])))
    return join_vertical(*blocks)


def _style_example(ln: str, ds) -> list[tuple[str, object]]:
    """Tokenize one example command line into styled segments.

    ``$`` prompt teal, a trailing ``# comment`` (set off by 2+ spaces) faint, flags
    (``--long`` / ``-x``) terracotta, and everything else — the command, its args,
    quoted strings — cream. Whitespace is preserved verbatim so the hand-aligned
    comment columns stay put.
    """
    import re

    from painted import current_palette

    p = current_palette()
    m = re.search(r"\s\s+#.*$", ln)
    cmd, comment = (ln[: m.start()], ln[m.start() :]) if m else (ln, "")
    segs: list[tuple[str, object]] = []
    for tok in re.findall(r"\S+|\s+", cmd):
        if tok.isspace():
            segs.append((tok, None))
        elif tok == "$":
            segs.append((tok, p.success))
        elif tok.startswith("--") or (len(tok) >= 2 and tok[0] == "-" and tok[1].isalpha()):
            segs.append((tok, ds.code))
        else:
            segs.append((tok, ds.assistant))  # command, args, strings — cream
    if comment:
        segs.append((comment, ds.faint))
    return segs


def _footer_block(page: HelpPage, width: int):
    """The ``Run '… <command> --help'`` pointer, with the hidden-plumbing line under it."""
    from painted import current_palette, join_vertical

    from siftd.output.row import row_line, wrap_segments
    from siftd.output.theme import domain_styles

    p = current_palette()
    ds = domain_styles()
    pointer = _lb(
        row_line([
            ("Run '", p.muted),
            (f"{' '.join(page.path)} <command> --help", ds.code),
            ("' for details.", p.muted),
        ])
    )
    if not page.hidden:
        return pointer
    hidden_text = "Advanced (hidden): " + ", ".join(page.hidden)
    hidden = [_lb(ln) for ln in wrap_segments([(hidden_text, p.muted)], width)]
    return join_vertical(pointer, *hidden)
