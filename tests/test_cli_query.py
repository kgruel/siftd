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


class TestShowDetail4xxSurface:
    """show <id> --around surfaces server 4xx instead of falling back locally."""

    def test_4xx_from_try_serve_exits_1_with_named_server_message(self, monkeypatch, tmp_path, capsys):
        """P6 path: server returns 4xx on --around phrase-not-found; CLI must surface it."""
        from siftd.serve.client import ServeRequest4xx

        serve_url = "http://homelab:8484"
        exc = ServeRequest4xx(
            400,
            "phrase not found in conversation: 'no-such-phrase'",
            f"{serve_url}/api/v1/conversations/FAKEID01234567890123456789",
        )

        # Patch try_serve in the delegation module so the import inside _query_detail sees it.
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: (_ for _ in ()).throw(exc))

        db = tmp_path / "siftd.db"
        # DB does not exist → _dispatch_detail routes to _query_detail which calls try_serve.

        from siftd.cli import main
        rc = main(["--db", str(db), "show", "FAKEID01234567890123456789", "--around", "no-such-phrase"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "siftd-serve returned HTTP 400" in err
        assert "phrase not found in conversation" in err
        assert serve_url in err
