"""Tests for the lane-grouped top-level help (CLI UX audit, presentation slice).

The six-lane `siftd --help` hides plumbing verbs from the listing but keeps them
fully runnable. The SessionStart hook calls `siftd register`, so delisting must
never break the invocation — these tests pin that contract.
"""

import re

import pytest

from siftd.cli import _LANES, _PLUMBING, main


def _root_help(capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def _listed_commands(help_text: str) -> set[str]:
    """Command names offered in the listing (above the lanes legend)."""
    listing = help_text.split("lanes:")[0]
    return {m.group(1) for line in listing.splitlines() if (m := re.match(r"    (\S+)  ", line))}


class TestLaneGrouping:
    def test_lane_legend_present(self, capsys):
        out = _root_help(capsys)
        assert "lanes:" in out
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
