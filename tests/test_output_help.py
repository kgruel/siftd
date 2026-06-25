"""Unit tests for siftd.output.help — the one help grammar.

The snapshot suite (tests/snapshots/test_help.py) pins the rendered TEXT for a
fixed command set; these pin the BEHAVIOUR the rendering rests on, which a snapshot
can't isolate:

* the argparse introspection contract (metavar derivation, -h/SUPPRESS filtering,
  mutex-group non-duplication, %% expansion);
* the version-stability machinery (the part-aware usage re-wrap) and the
  narrow-width invariant (no rendered line exceeds the width — the mutex-overflow
  fix);
* the design law — the brand layer spends only existing theme roles (no new hue),
  the grain is gold, structure pops by weight, and the glyphs degrade to ASCII.

Run piped, the snapshots only ever exercise the ASCII/no-colour path; the role and
glyph assertions here cover the interactive surface the snapshots can't.
"""

from __future__ import annotations

import argparse
import dataclasses
import io

import pytest
from painted import use_theme
from painted.core._text_width import display_width

from siftd.output import help as help_mod
from siftd.output.help import (
    HelpPage,
    OptionRow,
    _pack_usage,
    _split_usage_parts,
    render_help,
)
from siftd.output.mark import CHEVRON, GRAIN, breadcrumb_segments, wordmark_segments
from siftd.output.theme import domain_styles, siftd_theme, structure_style


@pytest.fixture(autouse=True)
def _theme():
    """Install the siftd theme for the process duration of each test (role checks)."""
    with use_theme(siftd_theme):
        yield


# --- introspection ---------------------------------------------------------


def _leaf() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="siftd demo", description="Demo command")
    g = p.add_argument_group("filtering")
    g.add_argument("-w", "--workspace", metavar="SUBSTR", help="ws")
    g.add_argument("--flag", action="store_true", help="a flag")
    g.add_argument("--opt", help="no metavar (dest-uppercased)")
    g.add_argument("--maybe", nargs="?", metavar="X", help="optional value")
    g.add_argument("--many", nargs="*", metavar="Y", help="zero or more")
    g.add_argument("--two", nargs=2, metavar="N", help="exactly two")
    g.add_argument("--pct", help="mild 15%% boost")
    mx = p.add_mutually_exclusive_group()
    mx.add_argument("--a", action="store_true", help="alpha")
    mx.add_argument("--b", action="store_true", help="beta")
    p.add_argument("--hidden", help=argparse.SUPPRESS)
    p.add_argument("target", nargs="?", help="the target")
    return p


def _rows(page: HelpPage) -> dict[str, OptionRow]:
    return {o.token: o for g in page.groups for o in g.options}


def test_metavar_derivation_across_nargs():
    rows = _rows(HelpPage.from_argparse(_leaf()))
    assert rows["-w, --workspace"].metavar == "SUBSTR"
    assert rows["--flag"].metavar == ""  # store_true → no value placeholder
    assert rows["--opt"].metavar == "OPT"  # dest, uppercased
    assert rows["--maybe"].metavar.startswith("[")  # nargs='?' → optional
    assert "Y" in rows["--many"].metavar and "..." in rows["--many"].metavar
    assert rows["--two"].metavar == "N N"  # nargs=2 → two metavars


def test_help_action_and_suppressed_are_filtered():
    rows = _rows(HelpPage.from_argparse(_leaf()))
    assert "-h, --help" not in rows  # universal; lives in usage, not the body
    assert "--hidden" not in rows  # help=SUPPRESS


def test_mutex_members_appear_exactly_once():
    page = HelpPage.from_argparse(_leaf())
    tokens = [o.token for g in page.groups for o in g.options]
    assert tokens.count("--a") == 1
    assert tokens.count("--b") == 1


def test_double_percent_expands_to_single():
    rows = _rows(HelpPage.from_argparse(_leaf()))
    assert rows["--pct"].help == "mild 15% boost"


def test_positional_renders_metavar_token():
    rows = _rows(HelpPage.from_argparse(_leaf()))
    assert "target" in rows
    assert rows["target"].metavar == ""


def test_description_is_the_summary():
    assert HelpPage.from_argparse(_leaf()).summary == "Demo command"


# --- branch / root ---------------------------------------------------------


def _root_and_choices():
    from siftd.cli import _build_parser

    root = _build_parser()
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    return root, sub.choices


def test_branch_yields_command_group_and_footer():
    _root, choices = _root_and_choices()
    page = HelpPage.from_argparse(choices["db"])
    cmd_groups = [g for g in page.groups if g.is_command]
    assert cmd_groups, "a branch should expose a command listing"
    names = {c.name for g in cmd_groups for c in g.commands}
    assert {"info", "stats", "restore"} <= names
    assert page.footer and page.footer.startswith("Run 'siftd db <command> --help'")


def test_root_carries_terse_inline_lanes():
    from siftd.cli import _LANES, _PLUMBING, _build_parser

    root = _build_parser()
    page = HelpPage.from_argparse(
        root, version="9.9.9", summary="S", lanes=_LANES, hidden=tuple(sorted(_PLUMBING))
    )
    assert page.version == "9.9.9"
    lanes = dict(page.lanes)
    assert {"EXPLORE", "CURATE", "MAINTAIN"} <= set(lanes)
    assert "query" in lanes["EXPLORE"] and "search" in lanes["EXPLORE"]
    assert page.groups == ()  # root spends no option groups (no OPTIONS block)
    assert page.hidden and "register" in page.hidden


# --- usage parsing & packing ----------------------------------------------


def test_split_usage_parts_keeps_groups_whole():
    assert _split_usage_parts("[-h] [-w SUBSTR]") == ["[-h]", "[-w SUBSTR]"]
    # A whole mutex group is one indivisible part (never split across a line).
    assert _split_usage_parts("[--a | --b | --c]") == ["[--a | --b | --c]"]
    # Nested brackets stay together.
    assert _split_usage_parts("[--refs [FILES]]") == ["[--refs [FILES]]"]
    # Bare tokens split at depth 0.
    assert _split_usage_parts("<command> ...") == ["<command>", "..."]


def test_split_usage_parts_tolerates_stray_brackets():
    # Depth never goes negative — a stray close bracket can't crash the splitter.
    assert _split_usage_parts("a ] b") == ["a", "]", "b"]


def test_pack_usage_fits_on_one_line():
    lines = _pack_usage(["[-h]", "[-w X]"], "usage: siftd q ", " " * 15, 80)
    assert lines == ["usage: siftd q [-h] [-w X]"]


def test_pack_usage_empty_is_just_the_prefix():
    assert _pack_usage([], "usage: siftd q ", " " * 15, 80) == ["usage: siftd q"]


def test_pack_usage_wraps_an_oversized_part():
    # A single group wider than the line is word-wrapped, never overrun.
    wide = "[--from-start | --from-end | --at-turn N | --around PHRASE]"
    cont = " " * 19
    lines = _pack_usage([wide], "usage: siftd query ", cont, 50)
    assert len(lines) > 1
    assert all(display_width(ln) <= 50 for ln in lines)


# --- mark / breadcrumb role fidelity & degradation -------------------------


def test_wordmark_grain_is_gold_letters_are_structure():
    segs = wordmark_segments(as_ascii=False)
    grain = [s for s in segs if s[0] == GRAIN]
    assert len(grain) == 1
    assert grain[0][1] == domain_styles().metric_strong  # the one gold speck
    letters = [s for s in segs if s[0] in ("sift", "d")]
    assert letters and all(st == structure_style() for _, st in letters)


def test_wordmark_drops_grain_in_ascii():
    assert all(s[0] != GRAIN for s in wordmark_segments(as_ascii=True))


def test_breadcrumb_uses_grain_and_chevron_unicode():
    texts = [t for t, _ in breadcrumb_segments(("db", "restore"), as_ascii=False)]
    assert GRAIN in texts
    assert any(CHEVRON in t for t in texts)
    steps = {
        t: st for t, st in breadcrumb_segments(("db", "restore"), as_ascii=False)
    }
    assert steps["db"] == structure_style() and steps["restore"] == structure_style()


def test_breadcrumb_degrades_to_ascii():
    segs = breadcrumb_segments(("db", "restore"), as_ascii=True)
    texts = [t for t, _ in segs]
    assert GRAIN not in texts
    assert any(">" in t for t in texts)
    assert all(CHEVRON not in t for t in texts)


# --- render_help invariants ------------------------------------------------


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("width", [60, 80, 120, 200])
def test_generated_layout_never_exceeds_width(width, monkeypatch):
    # The width contract covers the GENERATED layout (masthead, usage, groups,
    # footer) — the part the renderer wraps. The epilog is dropped here: it is the
    # author's raw text (hand-aligned examples), rendered verbatim like argparse's
    # RawDescriptionHelpFormatter, so its line lengths are the author's call.
    #
    # Widths >= 60: below that the usage indent (~20 cols under a long prog) plus a
    # single unbreakable flag token (e.g. [--no-exclude-active]) can exceed the
    # width — inherent, and exactly what stock argparse does too (a lone option
    # token has no wrap point; hard-splitting a flag name reads worse than a
    # one-column overrun). 60 still exercises the mutex-group wrap.
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.delenv("NO_COLOR", raising=False)
    _root, choices = _root_and_choices()
    for name in ("query", "search", "db"):
        page = dataclasses.replace(HelpPage.from_argparse(choices[name]), epilog=None)
        text = render_help(page, stream=io.StringIO())  # non-tty → plain ASCII
        for line in text.split("\n"):
            assert display_width(line) <= width, f"{name}@{width}: {line!r}"


def test_no_trailing_whitespace(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    _root, choices = _root_and_choices()
    text = render_help(HelpPage.from_argparse(choices["search"]), stream=io.StringIO())
    assert all(line == line.rstrip() for line in text.split("\n"))


def test_ansi_emitted_to_tty_stripped_when_piped(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.delenv("NO_COLOR", raising=False)
    _root, choices = _root_and_choices()
    page = HelpPage.from_argparse(choices["query"])
    assert "\x1b[" in render_help(page, stream=_FakeTTY())  # colour on a TTY
    assert "\x1b[" not in render_help(page, stream=io.StringIO())  # stripped when piped


def test_no_color_env_strips_ansi_on_tty(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    _root, choices = _root_and_choices()
    page = HelpPage.from_argparse(choices["query"])
    assert "\x1b[" not in render_help(page, stream=_FakeTTY())


def test_color_renders_at_terminal_depth_not_downsampled(monkeypatch):
    """Colour must emit the terminal's true depth, not downsample.

    Regression: render_help renders into a buffer (format_help must return a str);
    a plain buffer reports isatty()=False, so painted forced-ANSI to ColorDepth.NONE
    and downsampled truecolour to 16-colour (cream→37) plus a malformed bare-38 SGR
    that terminals show as default grey. The tty-reporting buffer must defer to the
    terminal's COLORTERM, emitting 38;2;r;g;b on a truecolour TTY.
    """
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.delenv("NO_COLOR", raising=False)
    _root, choices = _root_and_choices()
    out = render_help(HelpPage.from_argparse(choices["query"]), stream=_FakeTTY())
    assert "\x1b[38;2;" in out  # truecolour SGR
    assert "\x1b[38m" not in out  # not the malformed bare-38 downsample artifact


# --- the design law: only existing roles, no new hue -----------------------


def test_help_body_spends_only_existing_roles():
    """Every styled cell in a rendered page uses a colour the theme already owns —
    the brand layer names no new hue (the dropped blue accent must never reappear)."""
    p_styles = domain_styles()
    palette = __import__("painted").current_palette()
    allowed = {
        None,
        palette.muted.fg,  # connective tissue
        p_styles.summary.fg,  # metavars + help
        p_styles.code.fg,  # literal tokens
        p_styles.metric_strong.fg,  # the grain (gold)
        p_styles.faint.fg,  # defaults / comments / chevron — the tertiary tier
        p_styles.assistant.fg,  # example commands (cream)
        structure_style().fg,  # bold cream — structure
        palette.text.fg,  # the cream substrate
    }
    _root, choices = _root_and_choices()
    for name in ("query", "db"):
        page = HelpPage.from_argparse(choices[name])
        block = help_mod._compose(page, 80, as_ascii=False)
        seen = {
            cell.style.fg
            for r in range(block.height)
            for cell in block.row(r)
        }
        assert seen <= allowed, f"{name}: unexpected fg {seen - allowed}"
