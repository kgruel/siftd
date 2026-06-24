"""Tests for the lane-grouped top-level help (CLI UX audit, presentation slice).

The six-lane `siftd --help` hides plumbing verbs from the listing but keeps them
fully runnable. The SessionStart hook calls `siftd register`, so delisting must
never break the invocation — these tests pin that contract.
"""

import re

import pytest

from siftd.cli import _LANES, _PLUMBING, _build_parser, main


def _root_help(capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def _listed_commands(help_text: str) -> set[str]:
    """Command names offered in the terse inline lane legend.

    Each lane is one row — ``  EXPLORE   query · search · …`` — so the names follow
    the (uppercase) lane label and are ``·``-separated. Collect them across lanes.
    """
    names: set[str] = set()
    for line in help_text.splitlines():
        m = re.match(r"  [A-Z][A-Z]+ {2,}(.+)$", line)
        if m:
            names.update(p.strip() for p in m.group(1).split("·") if p.strip())
    return names


class TestLaneGrouping:
    def test_lane_legend_present(self, capsys):
        out = _root_help(capsys)
        # Each lane is a weighted group label heading its commands (no "lanes:" intro).
        for lane, _cmds in _LANES:
            assert lane in out

    def test_explore_verbs_listed(self, capsys):
        listed = _listed_commands(_root_help(capsys))
        assert {"query", "search", "show", "report", "peek", "tag", "export"} <= listed

    def test_plumbing_hidden_from_listing(self, capsys):
        out = _root_help(capsys)
        listed = _listed_commands(out)
        # Not offered as commands in the listing...
        assert _PLUMBING.isdisjoint(listed), f"plumbing leaked into listing: {_PLUMBING & listed}"
        # ...but named in the 'Advanced (hidden)' line so they remain discoverable.
        assert "Advanced (hidden):" in out
        for cmd in _PLUMBING:
            assert cmd in out

    def test_usage_line_is_clean(self, capsys):
        """No giant {cmd,cmd,...} brace in usage — just <command>.

        The brand masthead now leads the surface, so find the ``usage:`` line
        rather than assuming it is first.
        """
        out = _root_help(capsys)
        usage = next(line for line in out.splitlines() if line.startswith("usage:"))
        assert "<command>" in usage
        assert "{" not in usage

    def test_every_command_is_laned_or_plumbing(self):
        """Every registered sub-command must be in a lane or in _PLUMBING.

        Otherwise it is invisible in `siftd --help` (neither listed under a lane
        nor advertised on the 'Advanced (hidden)' line) while still being runnable.
        Pinning the partition forces a lane/plumbing decision when a command is
        added, and catches a lane referencing a removed/renamed command.
        """
        import argparse

        parser = _build_parser()
        sub = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        registered = set(sub.choices)
        laned = {c for _label, cmds in _LANES for c in cmds.split()}
        assert registered == laned | _PLUMBING, (
            f"unlaned (vanish from --help): {registered - laned - _PLUMBING}; "
            f"stale lane/plumbing refs: {(laned | _PLUMBING) - registered}"
        )

    @pytest.mark.parametrize("cmd", sorted(_PLUMBING))
    def test_hidden_command_still_runnable(self, cmd, capsys):
        """Delisting is cosmetic: each plumbing verb still parses its own --help."""
        with pytest.raises(SystemExit) as exc:
            main([cmd, "--help"])
        assert exc.value.code == 0


def test_session_start_hook_contract(tmp_path, capsys):
    """The SessionStart hook's `siftd register ...` invocation must keep working
    even though `register` is hidden from the lane listing.

    Uses an isolated throwaway DB (not the shared conversation fixture) so this
    registration can't pollute state other tests read.
    """
    db = tmp_path / "hook.db"
    rc = main([
        "--db", str(db), "register",
        "--session", "hook-sess", "--adapter", "claude_code", "--workspace", str(tmp_path),
    ])
    assert rc == 0
