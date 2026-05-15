"""Argparse-layer tests for siftd search --around and --turns flags.

All tests run through the actual argparse entry point (build_search_parser),
not _search(_args(...)) shortcuts, per the cli-argparse-test-gap memory.
"""

import argparse

import pytest

from siftd.cli._common import _parse_turns_range
from siftd.cli.search import _validate_search_axes, build_search_parser


def _make_parser():
    """Build a standalone parser that includes the search subcommand."""
    parser = argparse.ArgumentParser(prog="siftd")
    subparsers = parser.add_subparsers(dest="command")
    build_search_parser(subparsers)
    return parser


# ---------------------------------------------------------------------------
# Flag wiring: --around and --turns register correctly
# ---------------------------------------------------------------------------


class TestAroundAndTurnsFlags:
    def test_around_sets_attr(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "X", "--around", "phrase"])
        assert args.around == "phrase"
        assert args.turns_range is None

    def test_around_with_turns_spaced_form(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "X", "--around", "phrase", "--turns", "-2:+2"])
        assert args.around == "phrase"
        assert args.turns_range == "-2:+2"

    def test_around_with_turns_equals_form(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "X", "--around", "phrase", "--turns=-2:+2"])
        assert args.around == "phrase"
        assert args.turns_range == "-2:+2"

    def test_around_with_turns_positive_only(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "X", "--around", "phrase", "--turns", "0:3"])
        assert args.turns_range == "0:3"

    def test_fts_mode_with_around_and_turns(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "X", "--fts", "--around", "phrase", "--turns", "-2:+2"])
        assert args.fts is True
        assert args.around == "phrase"
        assert args.turns_range == "-2:+2"

    def test_no_around_flag_by_default(self):
        parser = _make_parser()
        args = parser.parse_args(["search", "X"])
        assert args.around is None
        assert args.turns_range is None

    def test_query_only_anchors_not_on_search(self):
        """--from-start is a query-only anchor; search parser must not register it."""
        parser = _make_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "X", "--from-start"])
        assert exc.value.code == 2

    def test_at_turn_not_on_search(self):
        """--at-turn is a query-only anchor; search parser must not register it."""
        parser = _make_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "X", "--at-turn", "3"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# --context removal: must exit 2 (unrecognized flag)
# ---------------------------------------------------------------------------


class TestContextRemoved:
    def test_context_flag_exits_2(self):
        """--context was removed; parser should not recognize it."""
        parser = _make_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "X", "--context", "2"])
        assert exc.value.code == 2

    def test_epilog_mentions_context_removal(self):
        """Parser epilog should mention --context removal for discoverability."""
        parent = argparse.ArgumentParser(prog="siftd")
        subparsers = parent.add_subparsers(dest="command")
        build_search_parser(subparsers)
        # Access the search subparser's epilog via subparsers choices dict
        choices = subparsers.choices  # type: ignore[attr-defined]
        assert choices is not None
        search_parser = choices["search"]
        epilog = search_parser.epilog or ""
        assert "--context" in epilog
        assert "--around" in epilog


# ---------------------------------------------------------------------------
# _parse_turns_range validation (called at command execution, not parse time)
# ---------------------------------------------------------------------------


class TestParseTurnsRangeValidation:
    def test_valid_signed_range(self):
        assert _parse_turns_range("-2:+2") == (-2, 2)

    def test_valid_positive_range(self):
        assert _parse_turns_range("0:3") == (0, 3)

    def test_valid_equals_start_end(self):
        assert _parse_turns_range("2:2") == (2, 2)

    def test_no_colon_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_turns_range("0")
        assert exc.value.code == 2
        assert "A:B format" in capsys.readouterr().err

    def test_non_integer_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_turns_range("a:b")
        assert exc.value.code == 2
        assert "integers" in capsys.readouterr().err

    def test_end_less_than_start_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_turns_range("3:1")
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# --turns without --around is rejected (axis validation, not parse time)
# ---------------------------------------------------------------------------


class TestTurnsRequiresAround:
    """--turns without --around must be rejected with a useful message.

    _validate_search_axes() is called early in cmd_search() before any I/O;
    these tests verify the validation logic directly since the error fires
    at execution time, not argparse parse time.
    """

    def _ns(self, **kwargs):
        """Minimal argparse.Namespace for _validate_search_axes."""
        defaults = {"mode": "chunks", "sort": "score", "around": None, "turns_range": None}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_turns_without_around_returns_error(self):
        err = _validate_search_axes(self._ns(turns_range="-2:+2"))
        assert err is not None
        assert "--turns" in err
        assert "--around" in err

    def test_turns_with_around_is_valid(self):
        err = _validate_search_axes(self._ns(around="phrase", turns_range="-2:+2"))
        assert err is None

    def test_neither_flag_is_valid(self):
        err = _validate_search_axes(self._ns())
        assert err is None

    def test_around_without_turns_is_valid(self):
        err = _validate_search_axes(self._ns(around="phrase"))
        assert err is None

    def test_turns_parser_parses_ok_but_validate_rejects(self):
        """--turns is syntactically valid; the error fires in cmd_search, not the parser."""
        parser = _make_parser()
        args = parser.parse_args(["search", "X", "--turns", "-2:+2"])
        assert args.turns_range == "-2:+2"
        assert args.around is None
        # Axis validation should reject this combination
        err = _validate_search_axes(args)
        assert err is not None
