"""CLI-layer argparse tests for anchor + window flags on siftd query <id>.

Exercises parse_args() directly — not SimpleNamespace hand-builds — so that
pre-parse bugs (mutex enforcement, prefix heuristics, type coercion) are caught
by CI rather than escaping to runtime.
"""

import argparse

import pytest

from siftd.cli.query import build_query_parser


@pytest.fixture()
def parser():
    p = argparse.ArgumentParser(prog="siftd")
    subs = p.add_subparsers(dest="command")
    build_query_parser(subs)
    return p


def parse(parser, args):
    return parser.parse_args(["query", "conv1"] + args)


class TestAnchors:
    def test_from_start(self, parser):
        ns = parse(parser, ["--from-start"])
        assert ns.from_start is True
        assert ns.from_end is False
        assert ns.at_turn is None
        assert ns.around is None

    def test_from_end(self, parser):
        ns = parse(parser, ["--from-end"])
        assert ns.from_end is True
        assert ns.from_start is False

    def test_at_turn(self, parser):
        ns = parse(parser, ["--at-turn", "4"])
        assert ns.at_turn == 4

    def test_around(self, parser):
        ns = parse(parser, ["--around", "error message"])
        assert ns.around == "error message"

    def test_at_turn_nonint_fails(self, parser):
        with pytest.raises(SystemExit) as exc:
            parse(parser, ["--at-turn", "abc"])
        assert exc.value.code == 2

    def test_anchor_mutex_from_start_and_from_end(self, parser):
        with pytest.raises(SystemExit) as exc:
            parse(parser, ["--from-start", "--from-end"])
        assert exc.value.code == 2

    def test_anchor_mutex_at_turn_and_from_start(self, parser):
        with pytest.raises(SystemExit) as exc:
            parse(parser, ["--at-turn", "3", "--from-start"])
        assert exc.value.code == 2


class TestWindowFlags:
    def test_exchanges_with_anchor(self, parser):
        ns = parse(parser, ["--from-end", "--exchanges", "5"])
        assert ns.exchanges == 5
        assert ns.turns_range is None

    def test_turns_spaced_negative(self, parser):
        """--turns -2:+2 (spaced, negative-prefixed) must parse without '=' syntax."""
        ns = parse(parser, ["--at-turn", "4", "--turns", "-2:+2"])
        assert ns.at_turn == 4
        assert ns.turns_range == "-2:+2"
        assert ns.exchanges is None

    def test_turns_eq_form(self, parser):
        """--turns=-2:+2 (= form) continues to work."""
        ns = parse(parser, ["--at-turn", "4", "--turns=-2:+2"])
        assert ns.turns_range == "-2:+2"

    def test_turns_spaced_and_eq_form_identical(self, parser):
        spaced = parse(parser, ["--at-turn", "4", "--turns", "-2:+2"])
        eq = parse(parser, ["--at-turn", "4", "--turns=-2:+2"])
        assert spaced.turns_range == eq.turns_range

    def test_turns_negative_start_zero_end(self, parser):
        ns = parse(parser, ["--at-turn", "5", "--turns", "-3:+0"])
        assert ns.turns_range == "-3:+0"

    def test_window_mutex_exchanges_and_turns(self, parser):
        with pytest.raises(SystemExit) as exc:
            parse(parser, ["--from-start", "--exchanges", "3", "--turns=-1:+1"])
        assert exc.value.code == 2

    def test_no_anchor_no_window_parses_ok(self, parser):
        """Parser accepts this; dispatch layer enforces anchor requirement."""
        ns = parse(parser, [])
        assert ns.exchanges is None
        assert ns.turns_range is None

    def test_exchanges_no_anchor_parses_ok(self, parser):
        """Parser accepts this; dispatch layer enforces anchor requirement."""
        ns = parse(parser, ["--exchanges", "3"])
        assert ns.exchanges == 3
