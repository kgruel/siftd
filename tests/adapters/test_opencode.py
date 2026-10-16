"""Tests for the OpenCode adapter: the decoder, the row reader, the content revision, and peek's deferral.

The golden cases under tests/fixtures/adapters/opencode/ hold the real captures;
these tests pin the decisions the captures do not reach (malformed fields, child
sessions, responses with no prompt, legacy schemas, interim tool output).
"""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from siftd.adapters import opencode
from siftd.adapters.contract import adapt
from siftd.adapters.records import CONTENT_REVISION_ATTRIBUTE, Disposition
from siftd.adapters.sdk import AdapterParseError
from siftd.domain.source import Source
from siftd.peek.scanner import DiscoveredFile, _scan_session_file

_TS = 1710079200000  # 2024-03-10T14:00:00Z, in milliseconds


def _iso(seconds_after: float) -> str:
    """The ISO time ``_TS`` plus whole seconds reads as, the way the adapter spells it."""
    return datetime.fromtimestamp((_TS + seconds_after * 1000) / 1000, tz=UTC).isoformat()


def _db(path: Path, *, parent_column: bool = True) -> sqlite3.Connection:
    """An OpenCode database with the three tables the adapter reads."""
    conn = sqlite3.connect(str(path))
    parent = "parent_id TEXT, " if parent_column else ""
    conn.execute(
        f"CREATE TABLE session (id TEXT, project_id TEXT, {parent}directory TEXT, title TEXT, version TEXT, "
        "time_created INTEGER, time_updated INTEGER)"
    )
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute(
        "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    return conn


def _session(conn, sid, *, created=_TS, updated=_TS + 60000, parent=None, directory="/ws"):
    if "parent_id" in {r[1] for r in conn.execute("PRAGMA table_info(session)")}:
        conn.execute(
            "INSERT INTO session (id, project_id, parent_id, directory, title, version, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, "proj", parent, directory, "Test", "1", created, updated),
        )
    else:
        conn.execute(
            "INSERT INTO session (id, project_id, directory, title, version, time_created, time_updated) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, "proj", directory, "Test", "1", created, updated),
        )


def _message(conn, sid, mid, at, data, updated=None):
    raw = data if isinstance(data, str) else json.dumps(data)
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, at, updated or at, raw))


def _part(conn, sid, mid, pid, at, data, updated=None):
    raw = data if isinstance(data, str) else json.dumps(data)
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, sid, at, updated or at, raw))


def _source(path: Path) -> Source:
    return Source(kind="sqlite", location=path)


def _assembly(path: Path):
    return adapt(opencode).assemble(_source(path))


def _reasons(assembly, disposition):
    return {reason for (d, reason) in assembly.ledger.reasons if d == disposition}


class TestDecoding:
    def test_a_session_reads_as_one_conversation_of_prompt_response_and_tool(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1", directory="/work/repo")
        _message(conn, "ses_1", "m1", _TS + 10000, {"role": "user", "summary": {"diffs": []}})
        _part(conn, "ses_1", "m1", "p1", _TS + 10000, {"type": "text", "text": "Run the tests please"})
        _message(conn, "ses_1", "m2", _TS + 20000, {
            "role": "assistant", "modelID": "claude-x", "cost": 0.025, "finish": "tool-calls",
            "tokens": {"total": 680, "input": 500, "output": 120, "reasoning": 60, "cache": {"read": 50, "write": 10}},
        })
        _part(conn, "ses_1", "m2", "p2", _TS + 20100, {"type": "step-start"})
        _part(conn, "ses_1", "m2", "p3", _TS + 20200, {"type": "reasoning", "text": "Plan first."})
        _part(conn, "ses_1", "m2", "p4", _TS + 20300, {"type": "text", "text": "I will run them."})
        _part(conn, "ses_1", "m2", "p5", _TS + 21000, {"type": "tool", "callID": "c1", "tool": "bash", "state": {
            "status": "completed", "input": {"command": "pytest"}, "output": "5 passed",
            "time": {"start": _TS + 21000, "end": _TS + 24000}}})
        _part(conn, "ses_1", "m2", "p6", _TS + 24100, {"type": "step-finish", "reason": "tool-calls"})
        _part(conn, "ses_1", "m2", "p7", _TS + 24200, {"type": "patch", "hash": "h", "files": ["/work/repo/a.py"]})
        conn.commit()
        conn.close()

        (conv,) = adapt(opencode).parse(_source(path))
        assert conv.external_id == "opencode::ses_1"
        assert (conv.started_at, conv.ended_at, conv.workspace_path) == (_iso(0), _iso(60), "/work/repo")
        prompt = conv.prompts[0]
        assert prompt.external_id == "opencode::m1" and prompt.content[0].content == {"text": "Run the tests please"}
        (resp,) = prompt.responses
        assert resp.external_id == "opencode::m2" and resp.model == "claude-x"
        assert (resp.usage.input_tokens, resp.usage.output_tokens) == (500, 120)
        assert resp.attributes == {
            "cost": "0.025", "reasoning_output_tokens": "60",
            "cache_read_input_tokens": "50", "cache_creation_input_tokens": "10",
        }
        assert [b.block_type for b in resp.content] == ["thinking", "text", "tool_use"]
        (call,) = resp.tool_calls
        assert (call.tool_name, call.external_id, call.status) == ("bash", "c1", "success")
        assert call.result == {"output": "5 passed"} and call.timestamp == _iso(21)
        assert call.attributes["completed_at"] == _iso(24)

        assembly = _assembly(path)
        assert _reasons(assembly, Disposition.IGNORED) == {"part:step-start", "part:step-finish", "part:patch"}
        assert assembly.ledger.disposed == assembly.ledger.records

    def test_each_session_is_its_own_conversation_in_start_order(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_late", created=_TS + 5000, updated=_TS + 6000)
        _session(conn, "ses_early", created=_TS, updated=_TS + 1000)
        for sid in ("ses_late", "ses_early"):
            _message(conn, sid, f"{sid}-u", _TS + 100, {"role": "user"})
            _part(conn, sid, f"{sid}-u", f"{sid}-p", _TS + 100, {"type": "text", "text": sid})
        conn.commit()
        conn.close()
        assert [c.external_id for c in adapt(opencode).parse(_source(path))] == ["opencode::ses_early", "opencode::ses_late"]

    def test_a_pending_call_has_no_result_and_a_running_call_keeps_its_interim_output(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1")
        _message(conn, "ses_1", "m1", _TS, {"role": "user"})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "text", "text": "go"})
        _message(conn, "ses_1", "m2", _TS + 1000, {"role": "assistant"})
        _part(conn, "ses_1", "m2", "p2", _TS + 1000, {"type": "tool", "callID": "queued", "tool": "bash",
                                                      "state": {"status": "pending", "input": {"command": "a"}}})
        _part(conn, "ses_1", "m2", "p3", _TS + 1100, {"type": "tool", "callID": "running", "tool": "bash",
                                                      "state": {"status": "running", "input": {"command": "b"},
                                                                "output": "partial", "time": {"start": _TS + 1100}}})
        conn.commit()
        conn.close()

        calls = {c.external_id: c for c in adapt(opencode).parse(_source(path))[0].prompts[0].responses[0].tool_calls}
        assert calls["queued"].status == "pending" and calls["queued"].result is None
        assert calls["running"].status == "pending" and calls["running"].result == {"output": "partial"}

    def test_a_user_message_with_no_text_takes_its_summary_title_and_one_with_text_does_not(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1")
        _message(conn, "ses_1", "m1", _TS, {"role": "user", "summary": {"title": "Attachment only"}})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "file", "url": "x"})
        _message(conn, "ses_1", "m2", _TS + 1000, {"role": "user", "summary": {"title": "Ignored title"}})
        _part(conn, "ses_1", "m2", "p2", _TS + 1000, {"type": "text", "text": "The real text"})
        conn.commit()
        conn.close()
        first, second = adapt(opencode).parse(_source(path))[0].prompts
        assert [b.content["text"] for b in first.content] == ["Attachment only"]
        assert [b.content["text"] for b in second.content] == ["The real text"]

    def test_a_child_session_names_the_session_it_ran_in(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_child", parent="ses_parent")
        _message(conn, "ses_child", "m1", _TS, {"role": "user"})
        _part(conn, "ses_child", "m1", "p1", _TS, {"type": "text", "text": "sub task"})
        conn.commit()
        conn.close()
        (conv,) = adapt(opencode).parse(_source(path))
        assert conv.parent("subagent").parent == "opencode::ses_parent"

    def test_a_response_with_no_prompt_before_it_is_kept_in_the_preamble(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "assistant_only")
        _message(conn, "assistant_only", "a1", _TS, {"role": "assistant", "modelID": "m"})
        _part(conn, "assistant_only", "a1", "p1", _TS, {"type": "text", "text": "I was already working."})
        _session(conn, "ses_2", created=_TS + 9000, updated=_TS + 9500)
        _message(conn, "ses_2", "a2", _TS + 9000, {"role": "assistant"})
        _part(conn, "ses_2", "a2", "p2", _TS + 9000, {"type": "text", "text": "Before the prompt."})
        _message(conn, "ses_2", "u2", _TS + 9100, {"role": "user"})
        _part(conn, "ses_2", "u2", "p3", _TS + 9100, {"type": "text", "text": "Now the prompt."})
        conn.commit()
        conn.close()

        only, mixed = adapt(opencode).parse(_source(path))
        assert only.prompts == [] and [r.content[0].content["text"] for r in only.preamble] == ["I was already working."]
        assert [r.content[0].content["text"] for r in mixed.preamble] == ["Before the prompt."]
        assert mixed.prompts[0].content[0].content == {"text": "Now the prompt."}

    def test_a_malformed_field_costs_only_that_field_and_the_source_still_parses(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "bad")
        _message(conn, "bad", "m1", _TS, {"role": "user"})
        _part(conn, "bad", "m1", "p1", _TS, {"type": "text", "text": "keep me"})
        _message(conn, "bad", "m2", _TS + 1000, {"role": "assistant", "modelID": 5, "tokens": "oops"})
        _message(conn, "bad", "m3", _TS + 2000, {"role": "assistant", "tokens": {"input": "x", "output": 5, "cache": "y"}})
        _message(conn, "bad", "m4", _TS + 3000, "{not json")
        _message(conn, "bad", "m5", _TS + 3500, {"role": "system"})
        _part(conn, "bad", "m2", "p2", _TS + 1000, {"type": "tool", "callID": "c", "tool": "x", "state": "nope"})
        _part(conn, "bad", "m2", "p3", _TS + 1100, {"type": "made-up"})
        _part(conn, "bad", "m2", "p4", _TS + 1200, "{bad json")
        _part(conn, "bad", "gone", "p5", _TS + 1300, {"type": "text", "text": "orphan"})
        _part(conn, "bad", "m4", "p6", _TS + 3100, {"type": "text", "text": "under a broken message"})
        _session(conn, "good", created=_TS + 10000, updated=_TS + 11000)
        _message(conn, "good", "g1", _TS + 10000, {"role": "user"})
        _part(conn, "good", "g1", "g1p", _TS + 10000, {"type": "text", "text": "still here"})
        conn.commit()
        conn.close()

        assembly = _assembly(path)
        assert [c.external_id for c in assembly.conversations] == ["opencode::bad", "opencode::good"]
        bad = assembly.conversations[0]
        assert bad.prompts[0].content[0].content == {"text": "keep me"}
        assert {r.model for p in bad.prompts for r in p.responses} == {None}
        assert _reasons(assembly, Disposition.UNKNOWN) >= {
            "message:modelID", "message:tokens", "message:tokens.input", "message:tokens.cache",
            "message:data", "message:role:system", "part:tool:state", "part:made-up", "part:data",
            "part:no-message",
        }
        assert assembly.ledger.disposed == assembly.ledger.records

    @pytest.mark.parametrize("kind", [[], {}, [1], {"a": 1}, 5, True])
    def test_a_part_whose_type_is_not_a_string_is_unknown_and_the_source_still_parses(self, tmp_path, kind):
        """An unhashable discriminator (``{"type": []}``) once raised TypeError and lost every session."""
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "odd")
        _message(conn, "odd", "m1", _TS, {"role": "user"})
        _part(conn, "odd", "m1", "p1", _TS, {"type": "text", "text": "kept"})
        _message(conn, "odd", "m2", _TS + 1000, {"role": "assistant"})
        _part(conn, "odd", "m2", "p2", _TS + 1000, {"type": kind})
        _session(conn, "other", created=_TS + 10000, updated=_TS + 11000)
        _message(conn, "other", "o1", _TS + 10000, {"role": "user"})
        _part(conn, "other", "o1", "op", _TS + 10000, {"type": "text", "text": "other kept"})
        conn.commit()
        conn.close()

        assembly = _assembly(path)
        assert [c.external_id for c in assembly.conversations] == ["opencode::odd", "opencode::other"]
        assert assembly.conversations[1].prompts[0].content[0].content == {"text": "other kept"}
        assert _reasons(assembly, Disposition.UNKNOWN) >= {"part:no-type"}
        assert assembly.ledger.disposed == assembly.ledger.records

    def test_a_token_count_beyond_the_sqlite_integer_range_costs_only_that_count(self, tmp_path):
        """A count SQLite cannot store once aborted the whole source at store time."""
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1")
        _message(conn, "ses_1", "m1", _TS, {"role": "user"})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "text", "text": "go"})
        _message(conn, "ses_1", "m2", _TS + 1000, {"role": "assistant", "tokens": {"input": 2**80, "output": 7}})
        conn.commit()
        conn.close()

        assembly = _assembly(path)
        (resp,) = assembly.conversations[0].prompts[0].responses
        assert (resp.usage.input_tokens, resp.usage.output_tokens) == (None, 7)
        assert _reasons(assembly, Disposition.UNKNOWN) >= {"message:tokens.input"}

    def test_a_session_with_no_usable_start_time_is_unknown_and_so_is_everything_in_it(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "undated", created="soon")
        _message(conn, "undated", "u1", _TS, {"role": "user"})
        _part(conn, "undated", "u1", "up", _TS, {"type": "text", "text": "lost"})
        _session(conn, "dated", created=_TS + 5000, updated=_TS + 6000)
        _message(conn, "dated", "d1", _TS + 5000, {"role": "user"})
        _part(conn, "dated", "d1", "dp", _TS + 5000, {"type": "text", "text": "kept"})
        conn.commit()
        conn.close()

        assembly = _assembly(path)
        assert [c.external_id for c in assembly.conversations] == ["opencode::dated"]
        assert _reasons(assembly, Disposition.UNKNOWN) >= {"session:no-id-or-time", "message:no-session", "part:no-session"}

    def test_a_database_without_the_child_session_column_still_parses(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path, parent_column=False)
        _session(conn, "ses_old")
        _message(conn, "ses_old", "m1", _TS, {"role": "user"})
        _part(conn, "ses_old", "m1", "p1", _TS, {"type": "text", "text": "old schema"})
        conn.commit()
        conn.close()
        (conv,) = adapt(opencode).parse(_source(path))
        assert conv.prompts[0].content[0].content == {"text": "old schema"} and "lineage.kind" not in conv.attributes


class TestSourceErrors:
    def test_a_missing_table_is_a_named_error(self, tmp_path):
        no_session = tmp_path / "no_session.db"
        sqlite3.connect(str(no_session)).close()
        with pytest.raises(AdapterParseError, match="missing the session table"):
            list(adapt(opencode).parse(_source(no_session)))

        no_message = tmp_path / "no_message.db"
        conn = _db(no_message)
        _session(conn, "s")
        conn.execute("DROP TABLE message")
        conn.commit()
        conn.close()
        with pytest.raises(AdapterParseError, match="no readable message table"):
            list(adapt(opencode).parse(_source(no_message)))

        no_part = tmp_path / "no_part.db"
        conn = _db(no_part)
        _session(conn, "s")
        conn.execute("DROP TABLE part")
        conn.commit()
        conn.close()
        with pytest.raises(AdapterParseError, match="no readable part table"):
            list(adapt(opencode).parse(_source(no_part)))

    def test_a_missing_or_non_file_source_is_a_named_error(self, tmp_path):
        (tmp_path / "isdir.db").mkdir()
        with pytest.raises(AdapterParseError, match="is not a file"):
            list(adapt(opencode).parse(_source(tmp_path / "isdir.db")))
        with pytest.raises(AdapterParseError, match="does not exist"):
            list(adapt(opencode).parse(_source(tmp_path / "nope.db")))

    def test_an_empty_database_has_no_conversations(self, tmp_path):
        path = tmp_path / "empty.db"
        _db(path).close()
        assert list(adapt(opencode).parse(_source(path))) == []


class TestDecoderTable:
    def test_the_decoder_table_names_exactly_the_ignored_part_types(self):
        """The docstring table is the unit ledger; it must not drift from what the code ignores."""
        rows = []
        for line in opencode.OpencodeDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((re.match(r"`([^`]+)`", cells[0]).group(1), cells[1]))
        assert len(rows) >= 10
        ignored = {name for name, becomes in rows if becomes.startswith("Ignored")}
        assert ignored == {f"part:{t}" for t in opencode._IGNORED_PART_TYPES}

    @pytest.mark.parametrize("kind", sorted(opencode._IGNORED_PART_TYPES))
    def test_each_ignored_part_type_is_ignored_by_name(self, kind):
        decoder = opencode.OpencodeDecoder()
        decoder.decode({"table": "session", "row": {"id": "s", "time_created": _TS, "time_updated": _TS}})
        decoder.decode({"table": "message", "row": {"id": "m", "session_id": "s", "time_created": _TS,
                                                     "data": json.dumps({"role": "assistant"})}})
        (record,) = decoder.decode({"table": "part", "row": {"id": "p", "message_id": "m", "session_id": "s",
                                                             "time_created": _TS,
                                                             "data": json.dumps({"type": kind})}})
        assert record.reason == f"part:{kind}"

    def test_an_unnamed_table_is_unknown(self):
        (record,) = opencode.OpencodeDecoder().decode({"table": "event", "row": {}})
        assert record.reason == "table:event"


class TestChangeSignal:
    def test_the_change_files_are_the_database_and_its_write_ahead_log(self, tmp_path):
        db = tmp_path / "opencode.db"
        assert opencode.change_paths(_source(db)) == [db, tmp_path / "opencode.db-wal"]


class TestContentRevision:
    """The revision is the container's replacement detector: it moves when any row's content moves."""

    def test_the_same_rows_have_the_same_revision(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1")
        _message(conn, "ses_1", "m1", _TS, {"role": "user"})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "text", "text": "go"})
        conn.commit()
        conn.close()
        (first,) = adapt(opencode).parse(_source(path))
        (second,) = adapt(opencode).parse(_source(path))
        assert first.attributes[CONTENT_REVISION_ATTRIBUTE]
        assert first.attributes[CONTENT_REVISION_ATTRIBUTE] == second.attributes[CONTENT_REVISION_ATTRIBUTE]

    def test_a_message_appended_before_the_session_end_moves_the_revision_and_not_the_end(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1", created=_TS, updated=_TS + 60000)
        _message(conn, "ses_1", "m1", _TS, {"role": "user"})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "text", "text": "go"})
        conn.commit()
        (before,) = adapt(opencode).parse(_source(path))
        _message(conn, "ses_1", "m2", _TS + 30000, {"role": "assistant"})
        _part(conn, "ses_1", "m2", "p2", _TS + 30000, {"type": "text", "text": "reply"})
        conn.commit()
        conn.close()
        (after,) = adapt(opencode).parse(_source(path))
        assert after.ended_at == before.ended_at
        assert after.attributes[CONTENT_REVISION_ATTRIBUTE] != before.attributes[CONTENT_REVISION_ATTRIBUTE]

    def test_an_edit_that_moves_no_time_moves_the_revision(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1", created=_TS, updated=_TS + 60000)
        _message(conn, "ses_1", "m1", _TS, {"role": "user"})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "text", "text": "go"})
        conn.commit()
        (before,) = adapt(opencode).parse(_source(path))
        conn.execute("UPDATE part SET data = ? WHERE id = 'p1'", (json.dumps({"type": "text", "text": "go again"}),))
        conn.commit()
        conn.close()
        (after,) = adapt(opencode).parse(_source(path))
        assert after.ended_at == before.ended_at
        assert after.attributes[CONTENT_REVISION_ATTRIBUTE] != before.attributes[CONTENT_REVISION_ATTRIBUTE]

    def test_a_row_updated_after_the_session_end_does_not_move_the_end(self, tmp_path):
        """A metadata edit after the session's last update is a change, not a longer session."""
        path = tmp_path / "opencode.db"
        conn = _db(path)
        _session(conn, "ses_1", created=_TS, updated=_TS + 60000)
        _message(conn, "ses_1", "m1", _TS, {"role": "user"})
        _part(conn, "ses_1", "m1", "p1", _TS, {"type": "text", "text": "go"})
        conn.commit()
        (before,) = adapt(opencode).parse(_source(path))
        conn.execute("UPDATE part SET data = ?, time_updated = ? WHERE id = 'p1'", (json.dumps({"type": "text", "text": "go again"}), _TS + 3600000))
        conn.commit()
        conn.close()
        (after,) = adapt(opencode).parse(_source(path))
        assert before.ended_at == _iso(60)
        assert after.ended_at == before.ended_at
        assert after.attributes[CONTENT_REVISION_ATTRIBUTE] != before.attributes[CONTENT_REVISION_ATTRIBUTE]


class TestPeekIsDeferred:
    """Peek reads one conversation per file, and an OpenCode database holds one per session.

    Peek is therefore deferred for this adapter: it offers no hooks, and the
    scanner lists the database with no preview rather than failing on it.
    """

    def test_the_adapter_offers_no_peek_hooks(self):
        for hook in ("peek_scan", "peek_exchanges", "peek_tail"):
            assert not hasattr(opencode, hook)

    def test_the_scanner_lists_a_many_session_database_without_a_preview(self, tmp_path):
        path = tmp_path / "opencode.db"
        conn = _db(path)
        for sid in ("a", "b"):
            _session(conn, sid)
            _message(conn, sid, f"{sid}m", _TS, {"role": "user"})
            _part(conn, sid, f"{sid}m", f"{sid}p", _TS, {"type": "text", "text": sid})
        conn.commit()
        conn.close()
        info = _scan_session_file(DiscoveredFile(path=path, mtime=0.0, adapter_module=adapt(opencode), adapter_name="opencode"))
        assert info is not None and info.preview_available is False
