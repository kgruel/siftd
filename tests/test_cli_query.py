"""Tests for siftd query CLI error handling."""

import pytest


class TestQuerySearchHint:
    """The 'did you mean siftd search' hint fires on -s / --search / --fts / --semantic."""

    def _run(self, argv):
        from siftd.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(argv)
        return exc_info.value.code

    def test_dash_s_exits_2_with_hint(self, capsys):
        code = self._run(["query", "-s", "dissolution test"])
        assert code == 2
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err
        assert "siftd search" in err

    def test_search_flag_exits_2_with_hint(self, capsys):
        code = self._run(["query", "--search", "foo"])
        assert code == 2
        err = capsys.readouterr().err
        assert "siftd search" in err

    def test_fts_flag_exits_2_with_hint(self, capsys):
        code = self._run(["query", "--fts", "foo"])
        assert code == 2
        err = capsys.readouterr().err
        assert "siftd search" in err

    def test_semantic_flag_exits_2_with_hint(self, capsys):
        code = self._run(["query", "--semantic", "foo"])
        assert code == 2
        err = capsys.readouterr().err
        assert "siftd search" in err

    def test_unrelated_unknown_flag_no_hint(self, capsys):
        code = self._run(["query", "--some-really-unknown-flag"])
        assert code == 2
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err
        assert "siftd search" not in err
