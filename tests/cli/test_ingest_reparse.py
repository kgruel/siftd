"""``siftd ingest --reparse`` through the argparse layer.

A parser fix only reaches stored rows if an unchanged source is re-parsed, so
these run the real ``main()`` -> ``run_ingest`` -> ``ingest_all`` path against a
test adapter whose parse output is fixed. What they observe is the conversation
row's identity: a replacement mints a new conversation ULID, so an unchanged
source that keeps its ULID was skipped, and one that gets a new ULID was
re-parsed. Tags and ownership are checked on the replacement.

Parametrized over both dedup strategies, because the two branches of ingest
each have their own skip logic and their own replacement site.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from conftest import make_conversation, make_test_adapter

from siftd.adapters.assembly import Assembler
from siftd.adapters.records import PromptRec, ResponseRec, Session, ToolCallRec
from siftd.cli import main
from siftd.domain import Harness, ToolCall
from siftd.storage.sqlite import ensure_conversation_owners_table, open_database
from siftd.storage.tags import apply_tag, get_or_create_tag

HARNESS = "reparse_test"
DEDUP = ["file", "session"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adapter(source, *, name=HARNESS, dedup="file", tool_calls=None, external_id="conv-1"):
    """A test adapter whose one source parses to one fixed conversation."""

    def parse(_source):
        yield make_conversation(
            external_id=external_id,
            harness_name=name,
            started_at="2024-01-15T10:00:00Z",
            ended_at="2024-01-15T11:00:00Z",
            tool_calls=tool_calls,
        )

    return make_test_adapter(source, name=name, dedup=dedup, harness_log_format="jsonl", parse_fn=parse)


def _write_source(path, text="{}"):
    path.write_text(text)
    return path


@pytest.fixture
def run_cli(tmp_path, monkeypatch, capsys):
    """Run ``siftd --db <tmp> ingest ...`` against the given test adapters.

    Returns ``(rc, events)``: ``events`` is the parsed ``--json`` stream.
    """
    db = tmp_path / "siftd.db"
    monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

    def run(adapters, *argv):
        monkeypatch.setattr(
            "siftd.api.ingest.load_all_adapters",
            lambda **_kw: [SimpleNamespace(name=a.NAME, module=a) for a in adapters],
        )
        capsys.readouterr()
        rc = main(["--db", str(db), "ingest", "--json", *argv])
        captured = capsys.readouterr()
        run.err = captured.err
        events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
        return rc, events

    run.db = db
    run.err = ""
    return run


def _conversation_id(db, external_id):
    conn = open_database(db)
    try:
        row = conn.execute(
            "SELECT id FROM conversations WHERE external_id = ?", (external_id,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _reparse_event(events):
    matches = [e for e in events if e.get("type") == "reparse"]
    assert len(matches) == 1, f"expected one reparse event, got {events}"
    return matches[0]


def _tag(db, target_kind, target_id, tag_name, applied_at=None):
    conn = open_database(db)
    try:
        apply_tag(
            conn, target_kind, target_id, get_or_create_tag(conn, tag_name),
            applied_at=applied_at, commit=True,
        )
    finally:
        conn.close()


def _event_id(db, conversation_id, kind, external_id):
    conn = open_database(db)
    try:
        row = conn.execute(
            "SELECT id FROM events WHERE conversation_id = ? AND kind = ? AND external_id IS ?",
            (conversation_id, kind, external_id),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def _event_tags(db, conversation_id, kind, external_id):
    """``(tag name, applied_at)`` pairs on the event with this kind and external_id."""
    conn = open_database(db)
    try:
        return sorted(
            (row["name"], row["applied_at"])
            for row in conn.execute(
                "SELECT t.name, ta.applied_at FROM events e "
                "JOIN tag_assignments ta ON ta.target_id = e.id "
                "JOIN tags t ON t.id = ta.tag_id "
                "WHERE e.conversation_id = ? AND e.kind = ? AND e.external_id IS ?",
                (conversation_id, kind, external_id),
            ).fetchall()
        )
    finally:
        conn.close()


def _conversation_tags(db, conversation_id):
    conn = open_database(db)
    try:
        return sorted(
            row["name"]
            for row in conn.execute(
                "SELECT t.name FROM tag_assignments ta JOIN tags t ON t.id = ta.tag_id "
                "WHERE ta.target_kind = 'conversation' AND ta.target_id = ?",
                (conversation_id,),
            ).fetchall()
        )
    finally:
        conn.close()


def _owners(db, conversation_id):
    conn = open_database(db)
    try:
        ensure_conversation_owners_table(conn)
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT user_id, push_id, assigned_at FROM conversation_owners "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Re-parsing unchanged sources
# ---------------------------------------------------------------------------


class TestReparseUnchangedSource:
    @pytest.mark.parametrize("dedup", DEDUP)
    def test_plain_ingest_skips_unchanged_source(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup=dedup)
        run_cli([adapter])
        first = _conversation_id(run_cli.db, "conv-1")

        run_cli([adapter])

        assert _conversation_id(run_cli.db, "conv-1") == first, "control: an unchanged source must be skipped"

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_reparse_re_parses_unchanged_source(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup=dedup)
        run_cli([adapter])
        first = _conversation_id(run_cli.db, "conv-1")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") != first, (
            "--reparse left an unchanged source's conversation in place: the parser fix "
            "would not reach it"
        )
        summary = _reparse_event(events)
        assert summary["reparsed"] == 1
        assert summary["replaced"] == 1
        assert summary["skipped_for_tags"] == []

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_reparse_carries_conversation_and_event_tags_and_ownership(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup=dedup)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "conversation", cid, "keeper")
        _tag(run_cli.db, "prompt", _event_id(run_cli.db, cid, "prompt", "p1"), "marked",
             applied_at="2024-02-02T00:00:00Z")
        conn = open_database(run_cli.db)
        ensure_conversation_owners_table(conn)
        conn.execute(
            "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at)"
            " VALUES (?, 'alice', 'push-1', '2024-01-01T00:00:00Z')",
            (cid,),
        )
        conn.commit()
        conn.close()

        rc, _events = run_cli([adapter], "--reparse")

        assert rc == 0
        new = _conversation_id(run_cli.db, "conv-1")
        assert new != cid, "no replacement happened"
        assert _conversation_tags(run_cli.db, new) == ["keeper"]
        assert _event_tags(run_cli.db, new, "prompt", "p1") == [("marked", "2024-02-02T00:00:00Z")], (
            "an event tag keyed by external_id must follow the event, keeping applied_at"
        )
        assert _owners(run_cli.db, new) == [("alice", "push-1", "2024-01-01T00:00:00Z")]


# ---------------------------------------------------------------------------
# Tag safety
# ---------------------------------------------------------------------------


def _tool_call_adapter(source, *, dedup, tool_name, command, external_id=None):
    """Adapter whose conversation holds one tool call with no external_id.

    A tool call without an id is the case a reparse cannot carry a person's tag
    onto, so it is where the tag-safety rule has to decide.
    """
    call = ToolCall(
        tool_name=tool_name,
        input={"command": command} if command is not None else {},
        external_id=external_id,
        status="success",
    )
    return _adapter(source, dedup=dedup, tool_calls=[call])


def _tool_call_event(db, conversation_id):
    conn = open_database(db)
    try:
        return conn.execute(
            "SELECT id FROM events WHERE conversation_id = ? AND kind = 'tool_call'",
            (conversation_id,),
        ).fetchone()["id"]
    finally:
        conn.close()


class TestReparseTagSafety:
    @pytest.mark.parametrize("dedup", DEDUP)
    def test_user_tag_that_cannot_carry_skips_without_force(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _tool_call_adapter(source, dedup=dedup, tool_name="file.read", command=None)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _tool_call_event(run_cli.db, cid), "needs-review")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") == cid, (
            "a reparse that would destroy a user-applied tag must not replace the conversation"
        )
        summary = _reparse_event(events)
        assert summary["skipped_for_tags"] == [
            {"path": str(source), "conversation_id": cid, "lost": 1}
        ]
        assert summary["replaced"] == 0
        assert [name for name, _ in _event_tags(run_cli.db, cid, "tool_call", None)] == ["needs-review"]

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_force_replaces_and_reports_no_skip(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _tool_call_adapter(source, dedup=dedup, tool_name="file.read", command=None)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _tool_call_event(run_cli.db, cid), "needs-review")

        rc, events = run_cli([adapter], "--reparse", "--force")

        assert rc == 0
        new = _conversation_id(run_cli.db, "conv-1")
        assert new != cid, "--force must replace a conversation the default would skip"
        assert _reparse_event(events)["skipped_for_tags"] == []
        assert _event_tags(run_cli.db, new, "tool_call", None) == [], (
            "--force replaces the conversation: a tag that could not carry is lost, as designed"
        )

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_rederived_shell_tag_does_not_block_reparse(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _tool_call_adapter(
            source, dedup=dedup, tool_name="shell.execute", command="uv run pytest",
        )
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        # Applied by the parser on insert (tag_shell_command), not by a person.
        assert [name for name, _ in _event_tags(run_cli.db, cid, "tool_call", None)] == ["shell:test"]

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        new = _conversation_id(run_cli.db, "conv-1")
        assert new != cid, "a derived tag the re-parse reproduces must not block the replacement"
        assert _reparse_event(events)["skipped_for_tags"] == []
        assert [name for name, _ in _event_tags(run_cli.db, new, "tool_call", None)] == ["shell:test"]

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_block_tag_always_blocks_reparse(self, tmp_path, run_cli, dedup):
        """Block tags have no re-pointing yet, so any replacement loses them."""
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup=dedup)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        conn = open_database(run_cli.db)
        block_id = conn.execute(
            "SELECT ec.id FROM event_content ec JOIN events e ON e.id = ec.event_id "
            "WHERE e.conversation_id = ? LIMIT 1",
            (cid,),
        ).fetchone()["id"]
        conn.close()
        _tag(run_cli.db, "block", block_id, "quoted")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") == cid
        assert _reparse_event(events)["skipped_for_tags"] == [
            {"path": str(source), "conversation_id": cid, "lost": 1}
        ]

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_hand_applied_shell_tag_counts_as_user_data(self, tmp_path, run_cli, dedup):
        """A ``shell:`` name the categorizer would not produce is a person's tag."""
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _tool_call_adapter(
            source, dedup=dedup, tool_name="shell.execute", command="ls -la",
        )
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _tool_call_event(run_cli.db, cid), "shell:test")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") == cid
        assert _reparse_event(events)["skipped_for_tags"] == [
            {"path": str(source), "conversation_id": cid, "lost": 1}
        ]


# ---------------------------------------------------------------------------
# The Gemini recovery
# ---------------------------------------------------------------------------


class TestReparseRecoversLegacyGeminiRows:
    """The release-note recovery, end to end: the real gemini_cli parser.

    Before the chat-file key, the sessionId-keyed conversation was stored once,
    and every other chat file of that session left only a session marker (a
    ``ingested_files`` row with no conversation pointer). A plain ingest skips
    such a file because its stat still matches. ``--reparse`` must adopt it.
    """

    def test_reparse_adopts_legacy_marker_and_stores_the_sibling(self, tmp_path, run_cli):
        from test_ingest_gemini_chat_identity import (
            START_A,
            START_B,
            _gemini_adapter_at,
            _legacy_pair,
            _store_superseded_row,
        )

        from siftd.adapters import gemini_cli
        from siftd.storage.sqlite import compute_file_hash, create_database, get_or_create_harness, record_session_file

        project, a, b, key = _legacy_pair(tmp_path)
        conn = create_database(run_cli.db)
        legacy_id = _store_superseded_row(conn, a, key)  # A's legacy conversation
        harness_id = get_or_create_harness(conn, gemini_cli.NAME, source=gemini_cli.HARNESS_SOURCE)
        st = b.stat()
        record_session_file(  # B: the marker the previous release left, no conversation
            conn, str(b), compute_file_hash(b), harness_id,
            file_mtime=st.st_mtime, file_size=st.st_size, commit=True,
        )
        conn.close()
        adapter = _gemini_adapter_at(tmp_path)

        run_cli([adapter])  # plain ingest: both markers match, nothing happens
        assert _conversation_id(run_cli.db, key) == legacy_id

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _reparse_event(events)["reparsed"] == 2
        conn = open_database(run_cli.db)
        rows = conn.execute("SELECT id, external_id, started_at FROM conversations").fetchall()
        conn.close()
        by_start = {r["started_at"]: r for r in rows}
        assert set(by_start) == {START_A, START_B}, "the sibling chat file must become a conversation"
        assert by_start[START_A]["id"] != legacy_id, "the legacy row must be replaced by its chat file"
        assert by_start[START_A]["external_id"].endswith(a.stem)
        assert by_start[START_B]["external_id"].endswith(b.stem)


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


class TestReparseFlags:
    def test_adapter_filter_limits_reparse(self, tmp_path, run_cli):
        alpha_src = _write_source(tmp_path / "alpha.jsonl")
        beta_src = _write_source(tmp_path / "beta.jsonl")
        alpha = _adapter(alpha_src, name="alpha", external_id="a-1")
        beta = _adapter(beta_src, name="beta", external_id="b-1")
        run_cli([alpha, beta])
        a_before = _conversation_id(run_cli.db, "a-1")
        b_before = _conversation_id(run_cli.db, "b-1")

        rc, events = run_cli([alpha, beta], "--reparse", "-a", "alpha")

        assert rc == 0
        assert _conversation_id(run_cli.db, "a-1") != a_before
        assert _conversation_id(run_cli.db, "b-1") == b_before, (
            "-a alpha must not re-parse beta"
        )
        assert _reparse_event(events)["reparsed"] == 1

    def test_force_without_reparse_is_a_usage_error(self, tmp_path, run_cli, capsys):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")

        rc, _events = run_cli([adapter], "--force")

        assert rc == 2
        assert _conversation_id(run_cli.db, "conv-1") == cid
        assert "--force applies only with --reparse" in run_cli.err

    def test_text_mode_names_each_declined_conversation(self, tmp_path, run_cli, monkeypatch, capsys):
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _tool_call_adapter(source, dedup="file", tool_name="file.read", command=None)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _tool_call_event(run_cli.db, cid), "needs-review")

        monkeypatch.setattr(
            "siftd.api.ingest.load_all_adapters",
            lambda **_kw: [SimpleNamespace(name=adapter.NAME, module=adapter)],
        )
        capsys.readouterr()
        rc = main(["--db", str(run_cli.db), "ingest", "--reparse", "--quiet"])
        captured = capsys.readouterr()

        assert rc == 0
        assert cid in captured.err, "a declined replacement must be named, even under --quiet"
        assert "--force" in captured.err

    def test_quiet_keeps_the_reparse_totals(self, tmp_path, run_cli, monkeypatch, capsys):
        """Totals are how a reparse says what it did; --quiet silences the rest, not them."""
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source)
        run_cli([adapter])

        monkeypatch.setattr(
            "siftd.api.ingest.load_all_adapters",
            lambda **_kw: [SimpleNamespace(name=adapter.NAME, module=adapter)],
        )
        capsys.readouterr()
        rc = main(["--db", str(run_cli.db), "ingest", "--reparse", "--quiet"])
        captured = capsys.readouterr()

        assert rc == 0
        assert "1 source(s) re-parsed, 1 conversation(s) replaced, 0 skipped for tags" in captured.err

    def test_bare_reparse_says_so_when_another_ingest_holds_the_lock(self, tmp_path, monkeypatch, capsys):
        """A reparse that did nothing because the lock was held must not exit silently on a pipe."""
        from siftd.api import ingest as ingest_api

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)
        db = tmp_path / "siftd.db"
        with ingest_api._ingest_lock(db):
            rc = main(["--db", str(db), "ingest", "--reparse"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "Another ingest is already running" in err
        assert "reparse was not run" in err


# ---------------------------------------------------------------------------
# Tag safety on every replacement path (not only the bookkeeping-pointer one)
# ---------------------------------------------------------------------------


def _first_block_id(db, conversation_id):
    conn = open_database(db)
    try:
        return conn.execute(
            "SELECT ec.id FROM event_content ec JOIN events e ON e.id = ec.event_id "
            "WHERE e.conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()["id"]
    finally:
        conn.close()


def _toggled_adapter(source, state, *, dedup="file"):
    """Adapter whose one source parses to nothing while ``state["empty"]`` is set."""

    def parse(_source):
        if state["empty"]:
            return
        yield make_conversation(
            external_id="conv-1",
            harness_name=HARNESS,
            started_at="2024-01-15T10:00:00Z",
            ended_at="2024-01-15T11:00:00Z",
        )

    return make_test_adapter(source, name=HARNESS, dedup=dedup, harness_log_format="jsonl", parse_fn=parse)


class TestReparseTagSafetyOnEveryPath:
    def test_orphan_with_a_blocking_tag_is_skipped_without_force(self, tmp_path, run_cli):
        """A conversation the bookkeeping lost track of is replaced by the file branch's orphan path."""
        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup="file")
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "block", _first_block_id(run_cli.db, cid), "quoted")
        conn = open_database(run_cli.db)
        conn.execute("UPDATE ingested_files SET conversation_id = NULL WHERE conversation_id = ?", (cid,))
        conn.commit()
        conn.close()

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") == cid, (
            "an orphan replacement that loses a block tag must be declined like any other"
        )
        assert _reparse_event(events)["skipped_for_tags"] == [
            {"path": str(source), "conversation_id": cid, "lost": 1}
        ]

    def test_legacy_marker_with_a_blocking_tag_is_skipped_without_force(self, tmp_path, run_cli):
        from test_ingest_gemini_chat_identity import _gemini_adapter_at, _legacy_pair, _store_superseded_row

        from siftd.storage.sqlite import compute_file_hash, create_database, get_or_create_harness, record_session_file
        from siftd.adapters import gemini_cli

        project, a, b, key = _legacy_pair(tmp_path)
        conn = create_database(run_cli.db)
        legacy_id = _store_superseded_row(conn, a, key)
        harness_id = get_or_create_harness(conn, gemini_cli.NAME, source=gemini_cli.HARNESS_SOURCE)
        st = b.stat()
        record_session_file(
            conn, str(b), compute_file_hash(b), harness_id,
            file_mtime=st.st_mtime, file_size=st.st_size, commit=True,
        )
        conn.close()
        adapter = _gemini_adapter_at(tmp_path)
        _tag(run_cli.db, "block", _first_block_id(run_cli.db, legacy_id), "quoted")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, key) == legacy_id, (
            "a legacy marker's replacement must be declined when it would lose a block tag"
        )
        assert {"path": str(a), "conversation_id": legacy_id, "lost": 1} in _reparse_event(events)["skipped_for_tags"]


class TestReparseWithoutAReplacement:
    def test_conversation_tag_blocks_a_reparse_that_parses_to_nothing(self, tmp_path, run_cli):
        """A parse that yields no conversation stores none to carry a conversation tag onto."""
        source = _write_source(tmp_path / "s.jsonl")
        state = {"empty": False}
        adapter = _toggled_adapter(source, state)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "conversation", cid, "keeper")
        state["empty"] = True

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") == cid
        assert _conversation_tags(run_cli.db, cid) == ["keeper"]
        assert _reparse_event(events)["skipped_for_tags"] == [
            {"path": str(source), "conversation_id": cid, "lost": 1}
        ]

    def test_force_deletes_the_conversation_and_its_tag_with_it(self, tmp_path, run_cli):
        source = _write_source(tmp_path / "s.jsonl")
        state = {"empty": False}
        adapter = _toggled_adapter(source, state)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "conversation", cid, "keeper")
        state["empty"] = True

        rc, events = run_cli([adapter], "--reparse", "--force")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") is None, "--force replaces with nothing, as designed"
        assert _reparse_event(events)["skipped_for_tags"] == []


class TestReparseSessionLatestWins:
    @pytest.mark.parametrize("reparse_order", [("newer", "older"), ("older", "newer")])
    def test_reparse_does_not_let_an_older_container_overwrite_a_newer_session(
        self, tmp_path, run_cli, reparse_order
    ):
        newer = _write_source(tmp_path / "newer.jsonl")
        older = _write_source(tmp_path / "older.jsonl")
        texts = {
            "newer.jsonl": ("newer transcript", "2024-01-15T11:00:00Z"),
            "older.jsonl": ("older transcript", "2024-01-10T11:00:00Z"),
        }

        def parse(source):
            text, ended = texts[source.as_path.name]
            yield make_conversation(
                external_id="conv-1",
                harness_name=HARNESS,
                started_at="2024-01-01T10:00:00Z",
                ended_at=ended,
                prompt_text=text,
            )

        def adapter_in(order):
            paths = {"newer": newer, "older": older}
            return make_test_adapter(
                [paths[name] for name in order], name=HARNESS, dedup="session",
                harness_log_format="jsonl", parse_fn=parse,
            )

        run_cli([adapter_in(("newer", "older"))])
        assert _conversation_ended_at(run_cli.db, "conv-1") == "2024-01-15T11:00:00Z", "control"

        rc, _events = run_cli([adapter_in(reparse_order)], "--reparse")

        assert rc == 0
        assert _conversation_ended_at(run_cli.db, "conv-1") == "2024-01-15T11:00:00Z", (
            "--reparse replaced the newer session with the older container's transcript"
        )
        assert _conversation_prompt_texts(run_cli.db, "conv-1") == ["newer transcript"]


def _conversation_ended_at(db, external_id):
    conn = open_database(db)
    try:
        return conn.execute(
            "SELECT ended_at FROM conversations WHERE external_id = ?", (external_id,)
        ).fetchone()["ended_at"]
    finally:
        conn.close()


def _conversation_prompt_texts(db, external_id):
    conn = open_database(db)
    try:
        return sorted(
            json.loads(row[0])["text"]
            for row in conn.execute(
                "SELECT ec.content FROM conversations c JOIN events e ON e.conversation_id = c.id "
                "JOIN event_content ec ON ec.event_id = e.id "
                "WHERE c.external_id = ? AND e.kind = 'prompt' AND ec.block_type = 'text'",
                (external_id,),
            ).fetchall()
        )
    finally:
        conn.close()


class TestReparseTotals:
    def test_replaced_total_counts_a_replacement_in_a_container_that_also_ingests(self, tmp_path, run_cli):
        """One container: a moved session is replaced while a new one arrives. The file counts as ingested."""
        source = _write_source(tmp_path / "s.jsonl")
        state = {"second": False}

        def parse(_source):
            yield make_conversation(
                external_id="conv-1", harness_name=HARNESS,
                started_at="2024-01-15T10:00:00Z", ended_at="2024-01-15T11:00:00Z",
            )
            if state["second"]:
                yield make_conversation(
                    external_id="conv-2", harness_name=HARNESS,
                    started_at="2024-01-16T10:00:00Z", ended_at="2024-01-16T11:00:00Z",
                )

        adapter = make_test_adapter(
            source, name=HARNESS, dedup="session", harness_log_format="jsonl", parse_fn=parse,
        )
        run_cli([adapter])
        before = _conversation_id(run_cli.db, "conv-1")
        state["second"] = True

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, "conv-1") != before, "control: conv-1 must be replaced"
        assert _conversation_id(run_cli.db, "conv-2") is not None
        assert _reparse_event(events)["replaced"] == 1


# ---------------------------------------------------------------------------
# Round 2: every door that removes assignments asks the one tag-safety question
# ---------------------------------------------------------------------------


class TestReparseGatesTheNewFileRoute:
    """A source with no bookkeeping row reaches ``_ingest_file``, which can adopt a legacy row.

    The markers the earlier tests clear or pre-seed put a row on the existing-file
    route; here the bookkeeping is gone entirely, so the file is *new* to ingest
    while the conversation it supersedes is not.
    """

    def _legacy_rows_without_bookkeeping(self, tmp_path, run_cli):
        from test_ingest_gemini_chat_identity import _gemini_adapter_at, _legacy_pair, _store_superseded_row

        from siftd.storage.sqlite import create_database

        _project, a, b, key = _legacy_pair(tmp_path)
        conn = create_database(run_cli.db)
        legacy_id = _store_superseded_row(conn, a, key)
        conn.execute("DELETE FROM ingested_files")  # the marker the old release left, cleared
        conn.commit()
        conn.close()
        return _gemini_adapter_at(tmp_path), a, b, key, legacy_id

    def test_a_tagged_legacy_row_is_not_adopted_without_force(self, tmp_path, run_cli):
        adapter, a, _b, key, legacy_id = self._legacy_rows_without_bookkeeping(tmp_path, run_cli)
        _tag(run_cli.db, "block", _first_block_id(run_cli.db, legacy_id), "quoted")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, key) == legacy_id, (
            "--reparse adopted a legacy row through the new-file route and deleted its block tag"
        )
        summary = _reparse_event(events)
        assert {"path": str(a), "conversation_id": legacy_id, "lost": 1} in summary["skipped_for_tags"]
        assert summary["replaced"] == 0

    def test_force_adopts_the_legacy_row_and_counts_the_replacement(self, tmp_path, run_cli):
        adapter, _a, _b, key, legacy_id = self._legacy_rows_without_bookkeeping(tmp_path, run_cli)
        _tag(run_cli.db, "block", _first_block_id(run_cli.db, legacy_id), "quoted")

        rc, events = run_cli([adapter], "--reparse", "--force")

        assert rc == 0
        assert _conversation_id(run_cli.db, key) != legacy_id
        summary = _reparse_event(events)
        assert summary["skipped_for_tags"] == []
        assert summary["replaced"] == 1, "an adoption through the new-file route is a replacement"

    def test_an_untagged_legacy_row_is_adopted_and_counted(self, tmp_path, run_cli):
        adapter, _a, _b, key, legacy_id = self._legacy_rows_without_bookkeeping(tmp_path, run_cli)

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _conversation_id(run_cli.db, key) != legacy_id
        assert _reparse_event(events)["replaced"] == 1


def _claimed_legacy_row(tmp_path, run_cli):
    """A legacy row ``old`` that path B claims, and the new file A that supersedes it.

    A parses to ``new-a``: it owns the row's prompt, and names ``old`` as its
    superseded key. B's bookkeeping points at the row but B parses to a different
    conversation, so the row is A's to take by transcript and the claim is not a
    duplicate. The row's response ``r1`` carries a tag; ``new-a`` produces the
    response as ``r-new``, so the tag has no event to follow.

    Returns ``(adapter, source_a, legacy_id)``.
    """
    from siftd.storage.sqlite import compute_file_hash, create_database, harness_id_for_conversation

    path_a = _write_source(tmp_path / "a.jsonl", "a")
    path_b = _write_source(tmp_path / "b.jsonl", "b")

    def build(external_id, *, prompt_id, prompt_text, response_id, superseded=()):
        conv = make_conversation(
            external_id=external_id, harness_name=HARNESS,
            started_at="2024-01-15T10:00:00Z", ended_at="2024-01-15T11:00:00Z",
            prompt_text=prompt_text,
        )
        conv.prompts[0].external_id = prompt_id
        conv.prompts[0].responses[0].external_id = response_id
        conv.superseded_external_ids = superseded
        return conv

    def parse(source):
        if source.as_path.name == "a.jsonl":
            yield build("new-a", prompt_id="p1", prompt_text="Hello", response_id="r-new", superseded=("old",))
        else:
            yield build("b-conv", prompt_id="pb", prompt_text="Different", response_id="rb")

    conn = create_database(run_cli.db)
    legacy = build("old", prompt_id="p1", prompt_text="Hello", response_id="r1")
    from siftd.storage.sqlite import record_ingested_file, store_conversation

    legacy_id = store_conversation(conn, legacy, filter_binary=False)
    st = path_b.stat()
    record_ingested_file(
        conn, str(path_b), compute_file_hash(path_b), legacy_id,
        file_mtime=st.st_mtime, file_size=st.st_size, commit=True,
    )
    assert harness_id_for_conversation(conn, legacy)
    conn.close()
    adapter = make_test_adapter(
        path_a, name=HARNESS, dedup="file", harness_log_format="jsonl", parse_fn=parse,
    )
    return adapter, path_a, legacy_id


class TestReparseGatesAssignmentsASplitWouldRemove:
    """A row another path claims is never deleted, but releasing its assignments removes them too."""

    def test_a_split_that_would_drop_a_response_tag_is_declined_without_force(self, tmp_path, run_cli):
        adapter, a, legacy_id = _claimed_legacy_row(tmp_path, run_cli)
        _tag(run_cli.db, "response", _event_id(run_cli.db, legacy_id, "response", "r1"), "keeper")
        before = _event_tags(run_cli.db, legacy_id, "response", "r1")
        assert [name for name, _ in before] == ["keeper"], "control"

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _event_tags(run_cli.db, legacy_id, "response", "r1") == before, (
            "the split released a response tag the incoming parse cannot restore"
        )
        assert _conversation_id(run_cli.db, "new-a") is None, "a declined door stores nothing"
        summary = _reparse_event(events)
        assert summary["skipped_for_tags"] == [{"path": str(a), "conversation_id": legacy_id, "lost": 1}]

    def test_force_splits_anyway(self, tmp_path, run_cli):
        adapter, _a, legacy_id = _claimed_legacy_row(tmp_path, run_cli)
        _tag(run_cli.db, "response", _event_id(run_cli.db, legacy_id, "response", "r1"), "keeper")

        rc, events = run_cli([adapter], "--reparse", "--force")

        assert rc == 0
        assert _conversation_id(run_cli.db, "new-a") is not None
        assert _conversation_id(run_cli.db, "old") == legacy_id, "the claimed row itself is never deleted"
        assert _reparse_event(events)["skipped_for_tags"] == []

    def test_a_split_that_can_restore_every_tag_proceeds(self, tmp_path, run_cli):
        """Control: the same split with a tag on an event the incoming parse does produce."""
        adapter, _a, legacy_id = _claimed_legacy_row(tmp_path, run_cli)
        _tag(run_cli.db, "prompt", _event_id(run_cli.db, legacy_id, "prompt", "p1"), "marked")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _reparse_event(events)["skipped_for_tags"] == []
        new = _conversation_id(run_cli.db, "new-a")
        assert new is not None
        assert [name for name, _ in _event_tags(run_cli.db, new, "prompt", "p1")] == ["marked"]

    def test_a_split_leaves_block_tags_on_the_row_so_they_do_not_block_it(self, tmp_path, run_cli):
        """A release takes only event assignments off the row; a block tag stays where it is.

        Asked as a delete, the same block tag would count as lost and decline a
        split that removes nothing of the kind.
        """
        adapter, _a, legacy_id = _claimed_legacy_row(tmp_path, run_cli)
        block = _first_block_id(run_cli.db, legacy_id)
        _tag(run_cli.db, "block", block, "quoted")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _reparse_event(events)["skipped_for_tags"] == []
        assert _conversation_id(run_cli.db, "new-a") is not None
        conn = open_database(run_cli.db)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM tag_assignments WHERE target_kind = 'block' AND target_id = ?", (block,)
        ).fetchone()[0]
        conn.close()
        assert remaining == 1, "the split must not touch a block assignment"


class TestReparseTotalsFollowTheCommit:
    def test_a_rolled_back_container_replaces_nothing(self, tmp_path, run_cli):
        """Session one replaces cleanly, session two fails the event uniqueness constraint.

        The container is one transaction, so both original conversations come
        back; the summary must not report the replacement that was undone.
        """
        source = _write_source(tmp_path / "s.jsonl")
        state = {"broken": False}

        def parse(_source):
            yield make_conversation(
                external_id="conv-1", harness_name=HARNESS,
                started_at="2024-01-15T10:00:00Z", ended_at="2024-01-15T11:00:00Z",
            )
            second = make_conversation(
                external_id="conv-2", harness_name=HARNESS,
                started_at="2024-01-16T10:00:00Z", ended_at="2024-01-16T11:00:00Z",
            )
            if state["broken"]:
                second.prompts.append(
                    make_conversation(external_id="x", harness_name=HARNESS).prompts[0]
                )  # a second prompt with external_id "p1": UNIQUE (conversation_id, kind, external_id)
            yield second

        adapter = make_test_adapter(
            source, name=HARNESS, dedup="session", harness_log_format="jsonl", parse_fn=parse,
        )
        run_cli([adapter])
        first, second = _conversation_id(run_cli.db, "conv-1"), _conversation_id(run_cli.db, "conv-2")
        state["broken"] = True

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert (_conversation_id(run_cli.db, "conv-1"), _conversation_id(run_cli.db, "conv-2")) == (first, second), (
            "control: the failed container must roll back"
        )
        summary = _reparse_event(events)
        assert summary["replaced"] == 0, "the summary counted a replacement the rollback undid"


def _writable_from_another_connection(db) -> bool:
    """Whether a second connection could commit a write right now (a ``siftd tag``)."""
    other = sqlite3.connect(str(db), timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


class TestTagDecisionsHoldTheWriteLock:
    """A tag committed between reading a row's assignments and deleting the row would be lost.

    The ingest lock excludes other ingests, not tagging, and the sqlite3 module
    begins its transaction only at the first write. So the gate (under --reparse)
    and the carryover snapshot (every replacement) must hold the write lock from
    the read through the delete; another connection's tag then waits for the
    commit instead of landing in the gap.
    """

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_the_reparse_gate_reads_under_the_write_lock(self, tmp_path, run_cli, monkeypatch, dedup):
        from siftd.ingestion import orchestration

        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup=dedup)
        run_cli([adapter])
        writable = []
        gate = orchestration._declined_for_tags

        def spy(conn, *args, **kwargs):
            declined = gate(conn, *args, **kwargs)
            writable.append(_writable_from_another_connection(run_cli.db))
            return declined

        monkeypatch.setattr(orchestration, "_declined_for_tags", spy)
        rc, _events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert writable == [False], "another connection could tag the row between the gate and the replacement"

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_the_carryover_snapshot_is_taken_under_the_write_lock(self, tmp_path, run_cli, monkeypatch, dedup):
        from siftd.ingestion import orchestration

        source = _write_source(tmp_path / "s.jsonl")
        adapter = _adapter(source, dedup=dedup)
        run_cli([adapter])
        writable = []
        snapshot = orchestration.snapshot_conversation

        def spy(conn, conversation_id):
            writable.append(_writable_from_another_connection(run_cli.db))
            return snapshot(conn, conversation_id)

        monkeypatch.setattr(orchestration, "snapshot_conversation", spy)
        rc, _events = run_cli([adapter], "--reparse", "--force")

        assert rc == 0
        assert writable == [False], "another connection could tag the row between the snapshot and the delete"


# ---------------------------------------------------------------------------
# A tag on a later chunk of a streamed message
# ---------------------------------------------------------------------------


def _chunk_adapter(source, *, dedup, shape):
    """One conversation holding a message streamed as chunks ``c1`` and ``c2``.

    ``shape["merged"]`` False stores each chunk as its own response, as a parser
    did before one message became one event; True assembles them into one.
    """

    def parse(_source):
        records = [
            Session(external_id="conv-1", started_at="2024-01-15T10:00:00Z", ended_at="2024-01-15T11:00:00Z"),
            PromptRec(at="2024-01-15T10:00:00Z", external_id="p1"),
            ResponseRec(at="2024-01-15T10:00:01Z", external_id="c1", key="m" if shape["merged"] else "m1"),
            ResponseRec(at="2024-01-15T10:00:02Z", external_id="c2", key="m" if shape["merged"] else "m2"),
        ]
        assembler = Assembler(Harness(name=HARNESS, source="test", log_format="jsonl"))
        for record in records:
            assembler.feed(record)
        yield from assembler.finish().conversations

    return make_test_adapter(source, name=HARNESS, dedup=dedup, harness_log_format="jsonl", parse_fn=parse)


class TestReparseCarriesATagOnAMergedChunk:
    @pytest.mark.parametrize("dedup", DEDUP)
    def test_the_tag_lands_on_the_merged_response(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        shape = {"merged": False}
        adapter = _chunk_adapter(source, dedup=dedup, shape=shape)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "response", _event_id(run_cli.db, cid, "response", "c2"), "later-chunk")

        shape["merged"] = True
        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        summary = _reparse_event(events)
        assert summary["skipped_for_tags"] == [], "the gate declined a tag the replacement can carry"
        assert summary["replaced"] == 1
        new = _conversation_id(run_cli.db, "conv-1")
        assert new != cid
        assert [name for name, _ in _event_tags(run_cli.db, new, "response", "c1")] == ["later-chunk"]
        assert _event_tags(run_cli.db, new, "response", "c2") == []


# ---------------------------------------------------------------------------
# Two calls that share a call_id are two events, each with its own tag
# ---------------------------------------------------------------------------


def _shared_call_id_adapter(source, *, dedup, shape):
    """One conversation whose calls all carry ``call_id="dup"``; ``shape["calls"]`` says how many."""

    def parse(_source):
        records = [
            Session(external_id="conv-1", started_at="2024-01-15T10:00:00Z", ended_at="2024-01-15T11:00:00Z"),
            PromptRec(at="2024-01-15T10:00:00Z", external_id="p1"),
            ResponseRec(at="2024-01-15T10:00:01Z", external_id="r1"),
        ]
        records += [
            ToolCallRec(at=f"2024-01-15T10:00:0{n + 2}Z", name="Read", input={"n": n}, call_id="dup")
            for n in range(shape["calls"])
        ]
        assembler = Assembler(Harness(name=HARNESS, source="test", log_format="jsonl"))
        for record in records:
            assembler.feed(record)
        yield from assembler.finish().conversations

    return make_test_adapter(source, name=HARNESS, dedup=dedup, harness_log_format="jsonl", parse_fn=parse)


class TestReparseKeepsDistinctCallsApart:
    """``dup:2`` names a second call, not a legacy alias of ``dup``, so a tag on it never moves to the first."""

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_removing_the_tagged_second_call_is_declined(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        shape = {"calls": 2}
        adapter = _shared_call_id_adapter(source, dedup=dedup, shape=shape)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _event_id(run_cli.db, cid, "tool_call", "dup:2"), "second")

        shape["calls"] = 1
        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        summary = _reparse_event(events)
        assert [e["lost"] for e in summary["skipped_for_tags"]] == [1], "the gate reported nothing to lose"
        assert _conversation_id(run_cli.db, "conv-1") == cid, "a declined door stores nothing"
        assert _event_tags(run_cli.db, cid, "tool_call", "dup") == []

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_force_drops_the_tag_rather_than_moving_it(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        shape = {"calls": 2}
        adapter = _shared_call_id_adapter(source, dedup=dedup, shape=shape)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _event_id(run_cli.db, cid, "tool_call", "dup:2"), "second")

        shape["calls"] = 1
        rc, _events = run_cli([adapter], "--reparse", "--force")

        assert rc == 0
        new = _conversation_id(run_cli.db, "conv-1")
        assert new != cid
        assert _event_tags(run_cli.db, new, "tool_call", "dup") == []

    @pytest.mark.parametrize("dedup", DEDUP)
    def test_a_kept_second_call_carries_its_tag(self, tmp_path, run_cli, dedup):
        source = _write_source(tmp_path / "s.jsonl")
        shape = {"calls": 2}
        adapter = _shared_call_id_adapter(source, dedup=dedup, shape=shape)
        run_cli([adapter])
        cid = _conversation_id(run_cli.db, "conv-1")
        _tag(run_cli.db, "tool_call", _event_id(run_cli.db, cid, "tool_call", "dup:2"), "second")

        rc, events = run_cli([adapter], "--reparse")

        assert rc == 0
        assert _reparse_event(events)["skipped_for_tags"] == []
        new = _conversation_id(run_cli.db, "conv-1")
        assert [name for name, _ in _event_tags(run_cli.db, new, "tool_call", "dup:2")] == ["second"]
        assert _event_tags(run_cli.db, new, "tool_call", "dup") == []
