"""Tests for anchor + window axes on siftd show <id>.

The handler (`_query_detail`) still lives in `siftd.cli.query` — it's shared
by `show`, which is the only verb exposing these flags on its parser now that
`query` lost its detail-view positional (docs/dev/cli-verb-coherence-2026-07-07.md).
"""

import argparse
import sqlite3
from types import SimpleNamespace

import pytest
from painted import Fidelity

from siftd.cli.query import _parse_turns_range, _query_detail
from siftd.cli.show import build_show_parser
from siftd.storage.fts import fts5_first_event_in_conversation


def _args(**kwargs):
    base = {
        "db": None,
        "json": False,
        "conversation_id": "conv1",
        "summary": False,
        "neighbors": False,
        # anchor axis
        "from_start": False,
        "from_end": False,
        "at_turn": None,
        "around": None,
        # window axis
        "exchanges": None,
        "turns_range": None,
        # fidelity
        "brief": False,
        "full": False,
        "chars": None,
        "thinking": False,
        "tools": None,
        "tool_chars": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _detail_stub(num_turns=5):
    turns = [SimpleNamespace() for _ in range(num_turns)]
    return SimpleNamespace(
        id="conv1",
        workspace_path="/w",
        started_at="2024-01-01",
        model="claude",
        total_input_tokens=10,
        total_output_tokens=20,
        tags=[],
        turns=turns,
    )


# ---------------------------------------------------------------------------
# _parse_turns_range unit tests
# ---------------------------------------------------------------------------


class TestParseTurnsRange:
    def test_positive_offsets(self):
        assert _parse_turns_range("0:3") == (0, 3)

    def test_signed_offsets(self):
        assert _parse_turns_range("-2:+2") == (-2, 2)

    def test_plus_prefix_stripped(self):
        assert _parse_turns_range("+1:+4") == (1, 4)

    def test_negative_start_and_end(self):
        assert _parse_turns_range("-5:-1") == (-5, -1)

    def test_end_less_than_start_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            _parse_turns_range("3:1")
        assert exc.value.code == 2

    def test_equal_start_end_ok(self):
        assert _parse_turns_range("2:2") == (2, 2)

    def test_bad_format_no_colon_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_turns_range("bad")
        assert exc.value.code == 2
        assert "A:B format" in capsys.readouterr().err

    def test_non_integer_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_turns_range("a:b")
        assert exc.value.code == 2
        assert "integers" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Anchor/window param threading through execute()
# ---------------------------------------------------------------------------


class TestAnchorParamThreading:
    """Verify that CLI translates flags into correct get_conversation params."""

    def _captured_params(self, monkeypatch, args):
        """Run _query_detail and return the Operation.params dict."""
        captured = {}

        def _fake_execute(op):
            captured.update(op.params)
            return _detail_stub()

        monkeypatch.setattr("siftd.api.dispatch.execute", _fake_execute)
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr(
            "siftd.output.format_registry.select_format",
            lambda **k: SimpleNamespace(render_detail=lambda *a, **k2: ""),
        )
        monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda _: None)

        rc = _query_detail(args)
        assert rc == 0
        return captured

    def test_no_anchor_no_window(self, monkeypatch):
        params = self._captured_params(monkeypatch, _args())
        assert params["anchor"] is None
        assert params["window_start"] is None
        assert params["window_end"] is None

    def test_from_start_exchanges(self, monkeypatch):
        params = self._captured_params(monkeypatch, _args(from_start=True, exchanges=3))
        assert params["anchor"] == "from_start"
        assert params["window_start"] == 0
        assert params["window_end"] == 2  # exchanges=3 → 0..2

    def test_from_end_exchanges(self, monkeypatch):
        params = self._captured_params(monkeypatch, _args(from_end=True, exchanges=5))
        assert params["anchor"] == "from_end"
        assert params["window_start"] == -4  # -(5-1)
        assert params["window_end"] == 0

    def test_at_turn_no_window(self, monkeypatch):
        params = self._captured_params(monkeypatch, _args(at_turn=4))
        assert params["anchor"] == "at_turn"
        assert params["anchor_value"] == 4
        assert params["window_start"] is None
        assert params["window_end"] is None

    def test_at_turn_with_turns_range(self, monkeypatch):
        params = self._captured_params(monkeypatch, _args(at_turn=4, turns_range="-1:+2"))
        assert params["anchor"] == "at_turn"
        assert params["anchor_value"] == 4
        assert params["window_start"] == -1
        assert params["window_end"] == 2

    def test_around_with_turns_range(self, monkeypatch):
        params = self._captured_params(
            monkeypatch, _args(around="error message", turns_range="-2:+2")
        )
        assert params["anchor"] == "around"
        assert params["anchor_value"] == "error message"
        assert params["window_start"] == -2
        assert params["window_end"] == 2

    def test_from_start_no_window(self, monkeypatch):
        params = self._captured_params(monkeypatch, _args(from_start=True))
        assert params["anchor"] == "from_start"
        assert params["window_start"] is None
        assert params["window_end"] is None


# ---------------------------------------------------------------------------
# Force-explicit: window without anchor exits 2
# ---------------------------------------------------------------------------


class TestForceExplicit:
    def test_exchanges_without_anchor_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _query_detail(_args(exchanges=3))
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--exchanges" in err
        assert "--from-start" in err
        assert "--from-end" in err
        assert "--at-turn" in err
        assert "--around" in err

    def test_turns_without_anchor_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _query_detail(_args(turns_range="0:2"))
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--turns" in err
        assert "--from-start" in err

    def test_exchanges_zero_with_anchor_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _query_detail(_args(from_end=True, exchanges=0))
        assert exc.value.code == 2
        assert "--exchanges must be at least 1" in capsys.readouterr().err


class TestParserWindowMutualExclusion:
    def test_exchanges_and_turns_are_mutually_exclusive(self, capsys):
        # Anchor/window flags live on `show`'s parser (query lost its detail-view
        # positional; see docs/dev/cli-verb-coherence-2026-07-07.md).
        parser = argparse.ArgumentParser(prog="siftd")
        subparsers = parser.add_subparsers(dest="command")
        build_show_parser(subparsers)

        with pytest.raises(SystemExit) as exc:
            parser.parse_args(
                ["show", "conv1", "--from-end", "--exchanges", "3", "--turns=-1:+1"]
            )
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--turns" in err
        assert "--exchanges" in err


# ---------------------------------------------------------------------------
# AnchorOutOfRange and AnchorNotFound propagation
# ---------------------------------------------------------------------------


class TestAnchorErrorPropagation:
    def _setup_execute_raises(self, monkeypatch, exc_factory):
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr(
            "siftd.api.dispatch.execute",
            lambda op: (_ for _ in ()).throw(exc_factory()),
        )

    def test_at_turn_out_of_range_exits_2(self, monkeypatch, capsys):
        from siftd.api.conversations import AnchorOutOfRange

        self._setup_execute_raises(monkeypatch, lambda: AnchorOutOfRange(3))
        with pytest.raises(SystemExit) as exc:
            _query_detail(_args(at_turn=99))
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "out of range" in err
        assert "3 turns" in err

    def test_around_not_found_exits_2(self, monkeypatch, capsys):
        from siftd.api.conversations import AnchorNotFound

        self._setup_execute_raises(monkeypatch, lambda: AnchorNotFound("missing phrase"))
        with pytest.raises(SystemExit) as exc:
            _query_detail(_args(around="missing phrase"))
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "not found" in err
        assert "missing phrase" in err
        assert "siftd search" in err

    def test_around_invalid_phrase_exits_2(self, monkeypatch, capsys):
        from siftd.api.conversations import AnchorPhraseInvalid

        self._setup_execute_raises(monkeypatch, lambda: AnchorPhraseInvalid('bad " phrase'))
        with pytest.raises(SystemExit) as exc:
            _query_detail(_args(around='bad " phrase'))
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "not a valid FTS5 phrase" in err


# ---------------------------------------------------------------------------
# _resolve_anchor unit tests (via get_conversation-level helper)
# ---------------------------------------------------------------------------


class TestResolveAnchor:
    def _make_turns(self, n):
        """Create n stub Turn-like objects with prompt_id and response_ids."""
        turns = []
        for i in range(n):
            turns.append(
                SimpleNamespace(
                    prompt_id=f"prompt-{i}",
                    response_ids=[f"resp-{i}"],
                    tool_call_ids=[],
                )
            )
        return turns

    def test_from_start(self):
        from siftd.api.conversations import _resolve_anchor

        turns = self._make_turns(5)
        assert _resolve_anchor(turns, "from_start", None, None, "c") == 0

    def test_from_end(self):
        from siftd.api.conversations import _resolve_anchor

        turns = self._make_turns(5)
        assert _resolve_anchor(turns, "from_end", None, None, "c") == 4

    def test_at_turn_valid(self):
        from siftd.api.conversations import _resolve_anchor

        turns = self._make_turns(5)
        assert _resolve_anchor(turns, "at_turn", 2, None, "c") == 2

    def test_at_turn_out_of_range(self):
        from siftd.api.conversations import AnchorOutOfRange, _resolve_anchor

        turns = self._make_turns(3)
        with pytest.raises(AnchorOutOfRange) as exc:
            _resolve_anchor(turns, "at_turn", 5, None, "c")
        assert exc.value.turn_count == 3

    def test_at_turn_negative_raises(self):
        from siftd.api.conversations import AnchorOutOfRange, _resolve_anchor

        turns = self._make_turns(3)
        with pytest.raises(AnchorOutOfRange):
            _resolve_anchor(turns, "at_turn", -1, None, "c")

    def test_around_matches_prompt(self, monkeypatch):
        from siftd.api.conversations import _resolve_anchor

        turns = self._make_turns(4)
        monkeypatch.setattr(
            "siftd.api.conversations.fts5_first_event_in_conversation",
            lambda conn, phrase, conversation_id: "prompt-2",
        )
        assert _resolve_anchor(turns, "around", "hello", None, "c") == 2

    def test_around_matches_response(self, monkeypatch):
        from siftd.api.conversations import _resolve_anchor

        turns = self._make_turns(4)
        monkeypatch.setattr(
            "siftd.api.conversations.fts5_first_event_in_conversation",
            lambda conn, phrase, conversation_id: "resp-3",
        )
        assert _resolve_anchor(turns, "around", "hello", None, "c") == 3

    def test_around_not_found(self, monkeypatch):
        from siftd.api.conversations import AnchorNotFound, _resolve_anchor

        turns = self._make_turns(3)
        monkeypatch.setattr(
            "siftd.api.conversations.fts5_first_event_in_conversation",
            lambda conn, phrase, conversation_id: None,
        )
        with pytest.raises(AnchorNotFound) as exc:
            _resolve_anchor(turns, "around", "ghost", None, "c")
        assert "ghost" in exc.value.phrase

    def test_around_invalid_phrase_raises(self, monkeypatch):
        from siftd.api.conversations import AnchorPhraseInvalid, _resolve_anchor

        turns = self._make_turns(3)

        def _raise(*args, **kwargs):
            raise sqlite3.OperationalError("malformed MATCH expression")

        monkeypatch.setattr("siftd.api.conversations.fts5_first_event_in_conversation", _raise)
        with pytest.raises(AnchorPhraseInvalid):
            _resolve_anchor(turns, "around", 'bad " phrase', None, "c")

    def test_from_end_empty_turns(self):
        from siftd.api.conversations import _resolve_anchor

        turns = self._make_turns(0)
        # max(0, -1) = 0; no crash on empty conversation
        assert _resolve_anchor(turns, "from_end", None, None, "c") == 0


class TestAroundPhraseSemantics:
    def test_around_uses_literal_phrase_not_token_and(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE VIRTUAL TABLE content_fts USING fts5(
                text_content,
                event_content_id UNINDEXED,
                event_id UNINDEXED,
                conversation_id UNINDEXED
            )
            """
        )
        # e1 contains both terms but not as an adjacent phrase.
        conn.execute(
            "INSERT INTO content_fts VALUES (?, ?, ?, ?)",
            ("error then message", "ec1", "e1", "conv1"),
        )
        # e2 contains the literal phrase.
        conn.execute(
            "INSERT INTO content_fts VALUES (?, ?, ?, ?)",
            ("error message", "ec2", "e2", "conv1"),
        )
        conn.commit()
        try:
            event_id = fts5_first_event_in_conversation(
                conn,
                "error message",
                conversation_id="conv1",
            )
            assert event_id == "e2"
        finally:
            conn.close()


class TestGetConversationConnectionSafety:
    def test_anchor_error_still_closes_connection(self, monkeypatch, tmp_path):
        from siftd.api.conversations import AnchorNotFound, get_conversation

        class _Conn:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        fake_conn = _Conn()
        db = tmp_path / "db.sqlite"
        db.write_text("")

        monkeypatch.setattr("siftd.api.conversations.open_database", lambda *a, **k: fake_conn)
        monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda conn, kind, _id, **kw: "conv1")
        monkeypatch.setattr(
            "siftd.api.conversations.fetch_conversation_by_id_or_prefix",
            lambda conn, cid: {"id": "conv1", "workspace": "/w", "started_at": "2024-01-01"},
        )
        monkeypatch.setattr("siftd.api.conversations.fetch_conversation_model", lambda *a, **k: None)
        monkeypatch.setattr(
            "siftd.api.conversations.fetch_conversation_token_totals",
            lambda *a, **k: (0, 0),
        )
        monkeypatch.setattr("siftd.api.conversations.fetch_prompts_for_conversation", lambda *a, **k: [])
        monkeypatch.setattr("siftd.api.conversations.fetch_prompt_text_contents", lambda *a, **k: {})
        monkeypatch.setattr("siftd.api.conversations.fetch_responses_for_conversation", lambda *a, **k: [])
        monkeypatch.setattr("siftd.api.conversations.fetch_response_content_blocks", lambda *a, **k: {})
        monkeypatch.setattr("siftd.api.conversations.fetch_tool_calls_for_conversation", lambda *a, **k: [])
        monkeypatch.setattr("siftd.api.conversations.fetch_conversation_tags", lambda *a, **k: [])
        monkeypatch.setattr("siftd.api.conversations._fetch_conversation_event_tags", lambda *a, **k: {})
        monkeypatch.setattr(
            "siftd.api.conversations._resolve_anchor",
            lambda *a, **k: (_ for _ in ()).throw(AnchorNotFound("missing phrase")),
        )

        with pytest.raises(AnchorNotFound):
            get_conversation(
                "conv1",
                fidelity=Fidelity(depth=1),
                db_path=db,
                anchor="around",
                anchor_value="missing phrase",
            )
        assert fake_conn.closed is True


# ---------------------------------------------------------------------------
# Ambiguous-match pre-pass: matched N turns emitted to stderr
# ---------------------------------------------------------------------------


class TestAroundAmbiguousMatchPrePass:
    """Verify that the pre-pass emits the matched-turns message when --around
    finds more than one match, and is silent when there's exactly one match."""

    def _make_fake_conn(self):
        class _FakeConn:
            closed = False

            def close(self):
                self.closed = True

        return _FakeConn()

    def _setup_stubs(self, monkeypatch, fake_conn, all_events, turn_indices):
        monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: fake_conn)
        monkeypatch.setattr(
            "siftd.api.conversations.resolve_entity_id",
            lambda conn, kind, _id, **kw: "conv1",
        )
        monkeypatch.setattr(
            "siftd.api.search.phrase_events_in_conversation",
            lambda conn, phrase, conversation_id: all_events,
        )
        monkeypatch.setattr(
            "siftd.api.search._events_to_turn_indices",
            lambda conn, eids, conv_id: turn_indices,
        )
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: _detail_stub())
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr(
            "siftd.output.format_registry.select_format",
            lambda **k: SimpleNamespace(render_detail=lambda *a, **k2: ""),
        )
        monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda _: None)

    def test_multiple_matches_emits_message(self, monkeypatch, capsys):
        fake_conn = self._make_fake_conn()
        self._setup_stubs(
            monkeypatch,
            fake_conn,
            all_events=["e1", "e2", "e3"],
            turn_indices=[2, 5, 8],
        )
        rc = _query_detail(_args(around="common phrase"))
        assert rc == 0
        err = capsys.readouterr().err
        assert "matched 3 turns" in err
        assert "showing first (turn 2)" in err
        assert "[5, 8]" in err

    def test_single_match_no_message(self, monkeypatch, capsys):
        fake_conn = self._make_fake_conn()
        self._setup_stubs(
            monkeypatch,
            fake_conn,
            all_events=["e1"],
            turn_indices=[2],
        )
        rc = _query_detail(_args(around="rare phrase"))
        assert rc == 0
        err = capsys.readouterr().err
        assert "matched" not in err

    def test_no_around_skips_pre_pass(self, monkeypatch, capsys):
        """Pre-pass should not run when --around is not set."""
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: _detail_stub())
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr(
            "siftd.output.format_registry.select_format",
            lambda **k: SimpleNamespace(render_detail=lambda *a, **k2: ""),
        )
        monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda _: None)
        rc = _query_detail(_args())  # no around
        assert rc == 0
        err = capsys.readouterr().err
        assert "matched" not in err
