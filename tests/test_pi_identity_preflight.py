"""Disposable P1–P4 fixtures for the private Pi identity diagnostic."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from siftd.adapters import pi_agent
from siftd.adapters._jsonl import load_jsonl
from siftd.api import _pi_identity_preflight as api
from siftd.domain import Source
from siftd.storage import _pi_identity_preflight as storage
from siftd.storage.sqlite import SCHEMA_VERSION, open_database, store_conversation

TIME = "2026-01-01T00:00:00Z"
SECRET = "SECRET_SENTINEL_NEVER_REPORT"


def records():
    return [
        {"type": "session", "id": SECRET, "timestamp": TIME},
        {"type": "message", "id": "u", "timestamp": TIME, "message": {"role": "user", "content": [SECRET]}},
        {"type": "message", "id": "a", "timestamp": TIME, "message": {"role": "assistant", "content": [
            {"type": "text", "text": SECRET}, {"type": "toolCall", "id": "tool", "name": "read", "arguments": {"secret": SECRET}},
        ]}},
        {"type": "message", "timestamp": TIME, "message": {"role": "toolResult", "toolCallId": "tool", "content": [{"type": "text", "text": SECRET}]}},
    ]


def write_source(path, items):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items), encoding="utf-8")


def link(conn, path, cid, *, harness="pi", error=None, evidence=None):
    conn.execute(
        "INSERT INTO ingested_files(id,path,file_hash,harness_id,conversation_id,ingested_at,error) VALUES(?,?,?,?,?,?,?)",
        (str(path), str(path), evidence if evidence is not None else hashlib.sha256(path.read_bytes()).hexdigest(), harness, cid, TIME, error),
    )


@pytest.fixture
def fixture(tmp_path):
    db = tmp_path / "fixture.db"
    source = tmp_path / "source.jsonl"
    write_source(source, records())
    conn = open_database(db)
    conn.execute("INSERT INTO harnesses(id,name) VALUES('pi','pi_agent'),('other','non_pi')")
    conv = list(pi_agent.parse(Source("file", source)))[0]
    cid = store_conversation(conn, conv, filter_binary=False)
    link(conn, source, cid)
    conn.commit()
    yield db, source, conn, cid
    conn.close()


def inspect(fixture, *, paths=None, orphans=()):
    db, source, _, _ = fixture
    before = logical(fixture[2])
    try:
        return api.inspect_pi_identity(db_path=db, source_paths=(source,) if paths is None else paths, orphan_conversation_ids=orphans)
    finally:
        # All fixture-backed successes, refusals and propagated errors preserve
        # schema, rows, bookkeeping, queue, owners and blob reference metadata.
        assert logical(fixture[2]) == before


def codes(report):
    return {f["code"] for f in report["findings"]}


def logical(conn):
    return "\n".join(conn.iterdump()), conn.execute("PRAGMA user_version").fetchone()[0]


def annotate(conn, cid):
    events = {r["kind"]: r["id"] for r in conn.execute("SELECT id,kind FROM events WHERE conversation_id=?", (cid,))}
    block = conn.execute("SELECT id FROM event_content WHERE event_id=? LIMIT 1", (events["prompt"],)).fetchone()[0]
    targets = [("prompt", events["prompt"]), ("response", events["response"]), ("exchange", events["prompt"]),
               ("block", block), ("tool_call", events["tool_call"]), ("conversation", cid)]
    conn.execute("INSERT INTO tags(id,name,created_at) VALUES('tag',?,?)", (SECRET, TIME))
    for n, (kind, target) in enumerate(targets):
        conn.execute("INSERT INTO tag_assignments VALUES(?,?,?,?,?)", (str(n), kind, target, "tag", TIME))
    conn.execute("INSERT INTO conversation_owners VALUES(?,?,NULL,?)", (cid, SECRET, TIME))
    conn.commit()
    return events


def test_p2_cardinality_dedup_and_secret_free(fixture, caplog):
    db, source, conn, cid = fixture
    annotate(conn, cid)
    copy = source.with_name("copy.jsonl")
    copy.write_bytes(source.read_bytes())
    link(conn, copy, cid)
    conn.commit()
    before = logical(conn)
    report = inspect(fixture, paths=(copy, source, source), orphans=())
    assert report["scope"] == {"source_input_occurrences": 3, "orphan_input_occurrences": 0, "selected_paths": 2,
                               "matched_rows": 2, "valid_links": 2, "conversations": 1, "explicit_orphans": 0, "outside_references": 0}
    inv = report["inventory"]
    assert inv["assignment_rows"] == 6 and inv["owners"] == 1
    assert sum(c["null_key"] for c in inv["assignments"].values()) == 3
    assert inv["block_assignments"] == inv["conversation_assignments"] == 1
    assert inv["assignments"]["tool_call"] == {"null_key": 0, "nonnull_key": 1}
    assert inv["keyed_exposure"]["tool_call"]["key_present_in_all_parses"] == 1
    assert [s["graph"] for s in report["sources"]] == ["equal", "equal"]
    assert report["conversations"][0]["source_bytes"] == "identical"
    assert report["status"] == "partial" and report["migration_safe"] is False
    reordered = inspect(fixture, paths=(source, copy))
    reordered["scope"]["source_input_occurrences"] = 3
    assert json.dumps(report, sort_keys=True) == json.dumps(reordered, sort_keys=True)
    assert logical(conn) == before
    output = json.dumps(report, sort_keys=True) + caplog.text
    for value in (SECRET, str(source), str(db), cid, hashlib.sha256(source.read_bytes()).hexdigest()):
        assert value not in output


def test_empty_scope_is_not_all(fixture):
    report = inspect(fixture, paths=())
    assert report["scope"]["conversations"] == 0
    assert report["sources"] == report["conversations"] == []
    assert report["status"] == "measured"


@pytest.mark.parametrize("version", [0, SCHEMA_VERSION - 1, SCHEMA_VERSION + 1])
def test_version_refusal_unchanged(fixture, version):
    _, source, conn, _ = fixture
    conn.execute(f"PRAGMA user_version={version}")
    before, data = logical(conn), source.read_bytes()
    report = inspect(fixture)
    assert report["status"] == "refused" and report["inventory"] is None
    assert "unsupported_version" in codes(report)
    assert logical(conn) == before and source.read_bytes() == data


@pytest.mark.parametrize("table", list(storage._COLUMNS))
def test_missing_required_tables_never_empty(fixture, table):
    _, _, conn, _ = fixture
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"DROP TABLE {table}")
    conn.commit()
    before = logical(conn)
    report = inspect(fixture)
    assert report["status"] == "refused" and "unsupported_schema" in codes(report)
    assert logical(conn) == before


@pytest.mark.parametrize("substitution", ["view", "columns"])
def test_schema_shape_refusal(fixture, substitution):
    _, _, conn, _ = fixture
    conn.execute("DROP TABLE conversation_owners")
    conn.execute("CREATE VIEW conversation_owners AS SELECT id AS conversation_id FROM conversations" if substitution == "view"
                 else "CREATE TABLE conversation_owners (wrong TEXT)")
    conn.commit()
    assert "unsupported_schema" in codes(inspect(fixture))


@pytest.mark.parametrize("name", ["literal?db", "literal#db", "literal%41db"])
def test_db_uri_names_refused_before_open(tmp_path, monkeypatch, name):
    literal = tmp_path / name
    literal.write_bytes(b"literal")
    decoded = tmp_path / name.replace("%41", "A")
    if decoded != literal:
        decoded.write_bytes(b"different decoded")
    before = literal.read_bytes(), decoded.read_bytes()
    monkeypatch.setattr(storage, "open_database", lambda *a, **k: pytest.fail("must refuse before open"))
    report = api.inspect_pi_identity(db_path=literal, source_paths=(), orphan_conversation_ids=())
    assert "database_invalid_path" in codes(report)
    assert (literal.read_bytes(), decoded.read_bytes()) == before


@pytest.mark.parametrize("path", ["//authority/db", "relative.db", "/not/../lexical.db", "/$HOME/db", "/~/db"])
def test_lexical_db_refusal(path, monkeypatch):
    monkeypatch.setattr(storage, "open_database", lambda *a, **k: pytest.fail("must not open"))
    assert api.inspect_pi_identity(db_path=Path(path), source_paths=(), orphan_conversation_ids=())["status"] == "refused"


@pytest.mark.parametrize("kind", ["missing", "corrupt", "directory", "symlink", "ancestor", "unreadable"])
def test_db_filesystem_refusals(tmp_path, kind):
    db = tmp_path / "db"
    if kind == "corrupt":
        db.write_bytes(b"not sqlite")
    elif kind == "directory":
        db.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"target")
        db.symlink_to(target)
    elif kind == "ancestor":
        real = tmp_path / "real"
        real.mkdir()
        (real / "db").write_bytes(b"target")
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        db = alias / "db"
    elif kind == "unreadable":
        db.write_bytes(b"no access")
        db.chmod(0)
    try:
        report = api.inspect_pi_identity(db_path=db, source_paths=(), orphan_conversation_ids=())
        assert report["status"] == "refused" and report["inventory"] is None
    finally:
        if kind == "unreadable":
            db.chmod(0o600)


def test_locked_rollback_database_refuses_with_default_timeout(fixture):
    db, _, conn, _ = fixture
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("BEGIN EXCLUSIVE")
    try:
        assert "database_unavailable" in codes(inspect(fixture))
    finally:
        conn.rollback()
    assert db.exists()


def test_wal_committed_rows_visible(fixture):
    db, _, conn, cid = fixture
    conn.execute("PRAGMA wal_autocheckpoint=0")
    annotate(conn, cid)
    assert db.with_name(db.name + "-wal").stat().st_size > 32
    assert inspect(fixture)["inventory"]["assignment_rows"] == 6


def test_readonly_sql_and_forbidden_collaborators(fixture, monkeypatch):
    _, source, conn, _ = fixture
    original = storage.open_database
    trace = []
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_PRAGMA}

    def opened(path, **kwargs):
        assert kwargs == {"read_only": True, "auto_upgrade": False}
        db = original(path, **kwargs)

        def authorize(action, first, second, *_):
            assert action in allowed
            if action == sqlite3.SQLITE_PRAGMA:
                assert first in ("user_version", "table_info")
                assert first == "table_info" or second is None
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        db.set_trace_callback(trace.append)
        return db

    monkeypatch.setattr(storage, "open_database", opened)
    from siftd import config, plugin_discovery
    from siftd.storage import sessions, sqlite

    def forbidden(*args, **kwargs):
        pytest.fail("diagnostic reached a forbidden collaborator")

    for name in dir(sqlite):
        if name.startswith(("ensure_", "insert_", "store_", "get_or_create_")):
            monkeypatch.setattr(sqlite, name, forbidden)
    monkeypatch.setattr(sessions, "ensure_session_tables", forbidden)
    monkeypatch.setattr(pi_agent, "load_jsonl", forbidden)
    monkeypatch.setattr(pi_agent, "discover", forbidden)
    for module in (config, plugin_discovery):
        for name in dir(module):
            if name.startswith(("load_", "get_config", "discover_")):
                monkeypatch.setattr(module, name, forbidden)
    import socket
    import subprocess
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before, data = logical(conn), source.read_bytes()
    assert inspect(fixture)["sources"][0]["graph"] == "equal"
    assert logical(conn) == before and source.read_bytes() == data
    assert trace[0] == "BEGIN" and trace[-1] == "ROLLBACK"


@pytest.mark.parametrize("state", ["untracked", "unlinked", "non_pi_source", "invalid_link", "dangling", "bookkeeping_error"])
def test_bookkeeping_states(fixture, state, monkeypatch):
    _, _, conn, cid = fixture
    if state == "untracked":
        conn.execute("DELETE FROM ingested_files")
    elif state == "unlinked":
        conn.execute("UPDATE ingested_files SET conversation_id=NULL")
    elif state == "non_pi_source":
        conn.execute("UPDATE ingested_files SET harness_id='other'")
        monkeypatch.setattr(api, "_capture", lambda *a: pytest.fail("non-Pi bytes must not be read"))
        monkeypatch.setattr(pi_agent, "_parse_records", lambda *a: pytest.fail("non-Pi must not parse"))
    elif state == "invalid_link":
        conn.execute("UPDATE conversations SET harness_id='other' WHERE id=?", (cid,))
    elif state == "dangling":
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE ingested_files SET conversation_id='absent'")
    else:
        conn.execute("UPDATE ingested_files SET error=?", (SECRET,))
    conn.commit()
    report = inspect(fixture)
    assert ("invalid_link" if state == "dangling" else state) in codes(report)
    if state != "bookkeeping_error":
        assert report["scope"]["conversations"] == 0
        assert report["sources"][0]["graph"] == "unknown"
    else:
        assert report["sources"][0]["hash"] == "equal"
    if state == "non_pi_source":
        assert report["sources"][0]["candidates"] is None
        assert report["sources"][0]["hash"] == "unknown"


@pytest.mark.parametrize("kind", ["missing", "unreadable", "non_regular", "empty", "no_events", "malformed"])
def test_source_failures_are_findings(fixture, kind):
    _, source, _, _ = fixture
    if kind == "missing":
        source.unlink()
    elif kind == "unreadable":
        source.chmod(0)
    elif kind == "non_regular":
        source.unlink()
        source.mkdir()
    elif kind == "empty":
        source.write_bytes(b" \t\r\n")
    elif kind == "no_events":
        write_source(source, records()[:1])
    else:
        source.write_bytes(b"broken\n")
    try:
        report = inspect(fixture)
        assert kind in codes(report) and report["status"] == "partial"
        if kind != "no_events":
            assert report["sources"][0]["graph"] == "unknown"
    finally:
        if kind == "unreadable":
            source.chmod(0o600)


def test_orphan_selectors(fixture):
    _, source, conn, cid = fixture
    assert "invalid_orphan_selector" in codes(inspect(fixture, paths=(), orphans=(cid,)))
    assert "invalid_orphan_selector" in codes(inspect(fixture, orphans=("missing",)))
    conn.execute("UPDATE ingested_files SET harness_id='other'")
    conn.commit()
    assert "invalid_orphan_selector" in codes(inspect(fixture, paths=(), orphans=(cid,)))
    conn.execute("DELETE FROM ingested_files")
    conn.commit()
    report = inspect(fixture, paths=(source,), orphans=(cid, cid))
    assert report["scope"]["explicit_orphans"] == 1 and "orphan" in codes(report)
    assert report["sources"][0]["link"] == "untracked" and report["sources"][0]["graph"] == "unknown"
    conn.execute("UPDATE conversations SET harness_id='other'")
    conn.commit()
    assert "invalid_orphan_selector" in codes(inspect(fixture, paths=(), orphans=(cid,)))


@pytest.mark.parametrize("selected_non_pi", [False, True])
def test_shared_pi_non_pi_reference_is_unknown(fixture, selected_non_pi):
    _, source, conn, cid = fixture
    annotate(conn, cid)
    other = source.with_name("other.jsonl")
    other.write_bytes(source.read_bytes())
    link(conn, other, cid, harness="other")
    conn.commit()
    report = inspect(fixture, paths=(source, other) if selected_non_pi else (source,))
    assert report["scope"]["conversations"] == 1
    assert report["conversations"][0]["source_bytes"] == "unknown"
    assert report["inventory"]["keyed_exposure"]["tool_call"]["unknown"] == 1


@pytest.mark.parametrize("variant", ["identical", "divergent", "hardlink", "unselected"])
def test_duplicate_references(fixture, variant):
    _, source, conn, cid = fixture
    other = source.with_name("other.jsonl")
    if variant == "hardlink":
        os.link(source, other)
    else:
        other.write_bytes(source.read_bytes() + (b"\n" if variant == "divergent" else b""))
    link(conn, other, cid)
    conn.commit()
    report = inspect(fixture, paths=(source,) if variant == "unselected" else (source, other))
    expected = "unknown" if variant == "unselected" else "identical" if variant == "hardlink" else variant
    assert report["conversations"][0]["source_bytes"] == expected
    assert report["scope"]["selected_paths"] == (1 if variant == "unselected" else 2)


@pytest.mark.parametrize("evidence,expected", [("", "unverified"), ("bad", "unverified"), ("0" * 64, "different")])
def test_hash_evidence(fixture, evidence, expected):
    _, _, conn, _ = fixture
    conn.execute("UPDATE ingested_files SET file_hash=?", (evidence,))
    conn.commit()
    assert inspect(fixture)["sources"][0]["hash"] == expected


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize("embedded", ["\u2028", "\v", "\f"])
def test_loader_equivalent_boundaries(fixture, newline, embedded, monkeypatch):
    _, source, _, _ = fixture
    items = records()
    items[1]["message"]["content"] = ["x" + embedded + "y"]
    raw = newline.join(json.dumps(r, ensure_ascii=False) for r in items).encode()
    source.write_bytes(raw)
    expected = load_jsonl(source)
    original = pi_agent._parse_records
    called = []

    def parse(actual, path):
        assert actual == expected
        called.append(True)
        return original(actual, path)

    monkeypatch.setattr(pi_agent, "_parse_records", parse)
    inspect(fixture)
    assert called == [True]


@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf{}\n", b"{}\n{bad}\n", b"null\n", b"[]\n", b"3\n", b"\xff\n", b'{"x":NaN}\n', b'{"x":Infinity}\n', b'{"x":1e999}\n'])
def test_strict_decode_never_parses_survivors(fixture, raw, monkeypatch):
    _, source, _, _ = fixture
    source.write_bytes(source.read_bytes() + raw)
    monkeypatch.setattr(pi_agent, "_parse_records", lambda *a: pytest.fail("malformed must not parse"))
    report = inspect(fixture)
    assert "malformed" in codes(report)
    assert report["sources"][0]["graph"] == "unknown"
    assert report["sources"][0]["candidates"]["partial"] is True


# Each consumed-field row has accepted and rejected shapes. Opaque fields stay opaque.
FIELD_CASES = [
    (0, ("type",), "session", []), (0, ("timestamp",), TIME, 1),
    (0, ("id",), SECRET, {}), (0, ("cwd",), None, []),
    (4, ("modelId",), None, 1), (1, ("message",), {}, None),
    (1, ("message", "role"), None, {}), (1, ("message", "content"), ["x", {}], "bare"),
    (2, ("message", "content"), [{}], ["bare"]),
    (1, ("message", "content", 0, "type"), "text", None),
    (1, ("message", "content", 0, "text"), "x", None),
    (2, ("message", "content", 0, "thinking"), "thought", 1),
    (2, ("message", "content", 1, "id"), None, []),
    (2, ("message", "content", 1, "name"), "tool", None),
    (2, ("message", "model"), None, []), (2, ("message", "usage"), None, []),
    (2, ("message", "usage", "input"), None, True), (2, ("message", "usage", "output"), 1, 1.5),
    (2, ("message", "usage", "cacheRead"), 0, "1"), (2, ("message", "usage", "cacheWrite"), -1, False),
    (2, ("message", "usage", "cost"), None, 2), (2, ("message", "usage", "cost", "total"), 0.25, True),
    (3, ("message", "toolCallId"), None, []), (3, ("message", "toolName"), "tool", None),
    (3, ("message", "isError"), True, 1), (3, ("message", "content"), [1, None, "ignored", {}], None),
    (3, ("message", "content", 0, "text"), "x", []),
]


def shaped_records(index, route, value):
    items = records() + [{"type": "model_change", "modelId": "m"}]
    items[1]["message"]["content"] = [{"type": "text", "text": "text"}]
    items[2]["message"]["content"][0] = {"type": "thinking", "thinking": "x"}
    items[2]["message"]["usage"] = {"cost": {}}
    obj = items[index]
    for key in route[:-1]:
        obj = obj[key]
    obj[route[-1]] = value
    return items


@pytest.mark.parametrize("index,route,accepted,rejected", FIELD_CASES)
@pytest.mark.parametrize("valid", [True, False])
def test_consumed_field_table(fixture, monkeypatch, index, route, accepted, rejected, valid):
    _, source, _, _ = fixture
    items = shaped_records(index, route, accepted if valid else rejected)
    write_source(source, items)
    original = pi_agent._parse_records
    called = []

    def parse(*args):
        assert valid
        called.append(True)
        return original(*args)

    monkeypatch.setattr(pi_agent, "_parse_records", parse)
    report = inspect(fixture)
    assert ("malformed" not in codes(report)) == valid
    assert bool(called) == valid


@pytest.mark.parametrize("arguments", [None, True, 1, 1.5, [], ["x"], {}, "", "not json", '{"x":1}', '{"x":NaN}', '{"x":Infinity}'])
def test_argument_values_and_nonfinite_decoded_strings(fixture, arguments):
    _, source, _, _ = fixture
    items = records()
    items[2]["message"]["content"][1]["arguments"] = arguments
    write_source(source, items)
    report = inspect(fixture)
    assert "malformed" not in codes(report)
    if isinstance(arguments, str) and ("NaN" in arguments or "Infinity" in arguments):
        assert report["sources"][0]["graph"] == "unknown"


def test_argument_recursion_error_is_loud(fixture):
    _, source, _, _ = fixture
    items = records()
    items[2]["message"]["content"][1]["arguments"] = '[' * 10000 + '0' + ']' * 10000
    write_source(source, items)
    with pytest.raises(RecursionError):
        inspect(fixture)


def test_unexpected_parser_error_propagates(fixture, monkeypatch):
    def broken(*args):
        raise RuntimeError("programming bug")
    monkeypatch.setattr(pi_agent, "_parse_records", broken)
    with pytest.raises(RuntimeError, match="programming bug"):
        inspect(fixture)


@pytest.mark.parametrize("change,reason", [
    ("missing_timestamp", "missing_timestamp"), ("empty_timestamp", "missing_timestamp"),
    ("invalid_timestamp", "invalid_timestamp"), ("ignored_timestamp", "invalid_timestamp"),
    ("no_header", "invalid_session"), ("null_id", "invalid_session"), ("empty_id", "invalid_session"),
    ("disagree_id", "invalid_session"), ("disagree_cwd", "invalid_session"),
])
def test_session_and_time_gates(fixture, monkeypatch, change, reason):
    _, source, _, _ = fixture
    items = records()
    if change == "missing_timestamp":
        del items[1]["timestamp"]
    elif change == "empty_timestamp":
        items[1]["timestamp"] = ""
    elif change == "invalid_timestamp":
        items[1]["timestamp"] = "today"
    elif change == "ignored_timestamp":
        items.append({"type": "ignored", "timestamp": "2026-01-01T00:00:00z"})
    elif change == "no_header":
        items.pop(0)
    elif change in ("null_id", "empty_id"):
        items[0]["id"] = None if change == "null_id" else ""
    else:
        items.append({**items[0], "id" if change == "disagree_id" else "cwd": "different"})
    write_source(source, items)
    monkeypatch.setattr(pi_agent, "_parse_records", lambda *a: pytest.fail("gate must precede parser"))
    report = inspect(fixture)
    assert reason in codes(report) and report["sources"][0]["graph"] == "unknown"
    assert report["sources"][0]["candidates"] is not None


def test_absent_and_null_cwd_agree(fixture):
    _, source, _, _ = fixture
    items = records()
    items.insert(1, {**items[0], "cwd": None})
    write_source(source, items)
    assert inspect(fixture)["sources"][0]["graph"] == "equal"


@pytest.mark.parametrize("value,state", [(None, "missing"), ("", "unclassified"), (1, "unclassified"),
                                           (" x", "unclassified"), ("é", "unclassified"), ("x" * 257, "unclassified"),
                                           ("x" * 256, "candidate"), ("A_z-9", "candidate"), ({}, "unclassified")])
def test_candidate_classifier(fixture, value, state):
    _, source, _, _ = fixture
    items = records()
    items[1]["id"] = value
    write_source(source, items)
    candidate = inspect(fixture)["sources"][0]["candidates"]
    assert candidate["kinds"]["user"][state] == 1


def test_candidates_discarded_cross_kind_and_repeated_tools(fixture):
    _, source, _, _ = fixture
    items = records()
    early = {**items[2], "message": {"role": "assistant", "content": []}}
    items.insert(1, early)
    items[2]["id"] = "a"
    items.append({**items[2]})
    write_source(source, items)
    report = inspect(fixture)
    candidate = report["sources"][0]["candidates"]
    assert candidate["kinds"]["discarded_assistant"]["candidate"] == 1
    assert candidate["kinds"]["user"]["duplicate_excess"] == 1
    assert candidate["cross_kind_reuse"] == 1
    assert "duplicate_id" in codes(report)
    # Tool results are not candidate tool calls or assistant responses.
    assert candidate["kinds"]["tool_call"]["candidate"] == 1
    items[3]["message"]["content"].append(items[3]["message"]["content"][1])
    write_source(source, items)
    report = inspect(fixture)
    assert "repeated_tool_id" in codes(report) and report["sources"][0]["graph"] == "unknown"


@pytest.mark.parametrize("change", ["append", "truncate", "reorder", "replace", "delete", "same_stat"])
def test_source_recheck_downgrades(fixture, monkeypatch, change):
    _, source, conn, cid = fixture
    annotate(conn, cid)
    original = api._capture
    calls = 0

    def capture(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            data = source.read_bytes()
            if change == "delete":
                source.unlink()
            elif change == "replace":
                replacement = source.with_name("replacement")
                replacement.write_bytes(data)
                replacement.replace(source)
            elif change == "append":
                source.write_bytes(data + b"\n")
            elif change == "truncate":
                source.write_bytes(b"")
            elif change == "reorder":
                source.write_bytes(b"\n".join(reversed(data.split(b"\n"))))
            else:
                # Simulate unchanged stat metadata without pretending hash equality.
                captured, reason = original(path)
                changed = captured[0].replace(b"read", b"READ")
                return (changed, captured[1], hashlib.sha256(changed).hexdigest()), reason
        return original(path)

    monkeypatch.setattr(api, "_capture", capture)
    before = logical(conn)
    report = inspect(fixture)
    assert calls == 2 and "source_changed_during_read" in codes(report)
    assert report["sources"][0]["hash"] == report["sources"][0]["graph"] == "unknown"
    assert report["inventory"]["keyed_exposure"]["tool_call"]["unknown"] == 1
    assert logical(conn) == before


def test_equal_hash_does_not_prove_graph(fixture):
    _, _, conn, _ = fixture
    conn.execute("UPDATE event_content SET content='{}' WHERE block_type='text'")
    conn.commit()
    report = inspect(fixture)
    assert report["sources"][0]["hash"] == "equal" and report["sources"][0]["graph"] == "different"


@pytest.mark.parametrize("marker", [{"filtered_reason": "binary_content"}, {"filtered_reason": "base64_content"},
                                    {"source": {"type": "filtered"}}, "[binary content filtered]", "[base64 content filtered]"])
@pytest.mark.parametrize("location", ["block", "result"])
def test_binary_uncertainty_nested(fixture, marker, location):
    _, _, conn, _ = fixture
    value = json.dumps({"nested": [marker]})
    if location == "block":
        conn.execute("UPDATE event_content SET content=?", (value,))
    else:
        conn.execute("UPDATE content_blobs SET content=?", (value,))
    conn.commit()
    assert inspect(fixture)["sources"][0]["graph"] == "unknown"


@pytest.mark.parametrize("defect", ["blob", "json", "edge", "kind", "extension", "extra_extension", "block_json"])
def test_graph_unavailable_not_equal(fixture, defect):
    _, _, conn, _ = fixture
    conn.execute("PRAGMA foreign_keys=OFF")
    statements = {
        "blob": "DELETE FROM content_blobs",
        "json": "UPDATE event_tool_call SET input='bad'",
        "edge": "UPDATE events SET parent_id=NULL WHERE kind='response'",
        "kind": "UPDATE events SET kind='alien' WHERE kind='response'",
        "extension": "DELETE FROM event_tool_call",
        "extra_extension": "INSERT INTO event_tool_call(event_id) SELECT id FROM events WHERE kind='prompt'",
        "block_json": "UPDATE event_content SET content='bad'",
    }
    conn.execute(statements[defect])
    conn.commit()
    assert inspect(fixture)["sources"][0]["graph"] == "unknown"


@pytest.mark.parametrize("key_state", ["all_null", "partial", "all_nonnull", "invalid"])
def test_stored_key_states_and_annotations(fixture, key_state):
    _, _, conn, cid = fixture
    annotate(conn, cid)
    if key_state == "all_null":
        conn.execute("UPDATE events SET external_id=NULL")
    elif key_state == "all_nonnull":
        conn.execute("UPDATE events SET external_id=kind")
    elif key_state == "invalid":
        conn.execute("UPDATE events SET external_id='' WHERE kind='prompt'")
    conn.commit()
    report = inspect(fixture)
    assert report["conversations"][0]["event_key_state"] == key_state
    assert report["inventory"]["assignment_rows"] == 6 and report["inventory"]["block_assignments"] == 1
    if key_state == "all_nonnull":
        for kind in ("prompt", "exchange", "response"):
            assert report["inventory"]["keyed_exposure"][kind]["key_absent_from_all_parses"] == 1
    elif key_state == "all_null":
        assert report["inventory"]["assignments"]["tool_call"]["null_key"] == 1


def test_invalid_assignment_pairs_count_as_unknown(fixture):
    _, _, conn, cid = fixture
    events = annotate(conn, cid)
    conn.execute("INSERT INTO tag_assignments VALUES('invalid','response',?,'tag',?)", (events["prompt"], TIME))
    # Unrelated polymorphic dangling assignments cannot be scoped to this graph.
    conn.execute("INSERT INTO tag_assignments VALUES('dangling','response','absent','tag',?)", (TIME,))
    conn.commit()
    inv = inspect(fixture)["inventory"]
    assert inv["unknown_assignments"] == 1 and inv["assignment_rows"] == 7


def test_pending_directed_ambiguity_and_context(fixture):
    _, _, conn, cid = fixture
    conn.execute("INSERT INTO conversations(id,external_id,harness_id,started_at) VALUES('other-c',?,'other',?)", ("non_pi::" + SECRET, TIME))
    conn.execute("INSERT INTO conversations(id,external_id,harness_id,started_at) VALUES('subagent','pi_agent::agent::child','pi',?)", (TIME,))
    conn.execute("INSERT INTO active_sessions VALUES('registered','pi_agent',NULL,?,?)", (TIME, TIME))
    queue = [(SECRET, "exchange", "last_exchange"), ("pi_agent::" + SECRET, "prompt", None),
             ("pi_agent::agent::child", "conversation", None), ("registered", "alien", "alien"),
             ("unknown", "conversation", None), ("extra::pi_agent::" + SECRET, "response", None)]
    for n, (key, kind, marker) in enumerate(queue):
        conn.execute("INSERT INTO pending_tags VALUES(?,?,?,?,NULL,?,?)", (str(n), key, SECRET, kind, marker, TIME))
    conn.commit()
    before = logical(conn)
    pending = inspect(fixture)["pending"]
    assert pending["ambiguous_touching_scope"] == 1 and pending["unique_selected"] == 1
    assert pending["unmatched_pi_attributed"] == 2 and pending["unattributed_or_out_of_scope"] == 2
    assert sum(g["rows"] for g in pending["groups"]) == 6
    assert any(g["entity_type"] == g["last_marker"] == "other" for g in pending["groups"])
    assert logical(conn) == before


def test_projection_multisets_ignore_sibling_order_preserve_parentage():
    ev = [dict(id="p1", kind="prompt", parent_id=None), dict(id="p2", kind="prompt", parent_id=None),
          dict(id="r1", kind="response", parent_id="p1"), dict(id="r2", kind="response", parent_id="p2")]
    blocks = [dict(event_id=e["id"], block_index=0, block_type="text", content=json.dumps(e["id"])) for e in ev]
    projection, _ = api._projection(ev, blocks, [])
    assert api._projection(list(reversed(ev)), list(reversed(blocks)), [])[0] == projection
    ev[2]["parent_id"], ev[3]["parent_id"] = "p2", "p1"
    assert api._projection(ev, blocks, [])[0] != projection
    repeated = [dict(id="a", kind="prompt", parent_id=None), dict(id="b", kind="prompt", parent_id=None)]
    assert api._projection(repeated, [], [])[1] == 1


def test_findings_sample_bound_exact_total(fixture):
    _, source, _, _ = fixture
    paths = tuple(source.with_name(f"missing-{i}") for i in range(9))
    report = inspect(fixture, paths=paths)
    finding = next(f for f in report["findings"] if f["code"] == "missing")
    assert finding["total"] == 9 and finding["references"] == list(range(5))
    assert report["unassessed"]["sources"] == 9


def test_no_public_registration():
    import siftd.api
    assert not hasattr(siftd.api, "inspect_pi_identity")
    root = Path(__file__).parents[1] / "src" / "siftd"
    # Enumerable private-boundary ratchet: no CLI, Operation, HTTP or package
    # export may acquire this diagnostic. Only its two implementation files do.
    consumers = {str(p.relative_to(root)) for p in root.rglob("*.py") if "pi_identity_preflight" in p.read_text()}
    assert consumers == {"api/_pi_identity_preflight.py"}


def test_key_presence_mixed_is_not_correspondence(fixture):
    _, source, conn, cid = fixture
    annotate(conn, cid)
    other = source.with_name("other.jsonl")
    items = records()
    items[2]["message"]["content"][1]["id"] = "different_tool"
    items[3]["message"]["toolCallId"] = "different_tool"
    write_source(other, items)
    link(conn, other, cid)
    conn.commit()
    report = inspect(fixture, paths=(source, other))
    assert report["inventory"]["keyed_exposure"]["tool_call"]["mixed"] == 1
    assert report["conversations"][0]["source_bytes"] == "divergent"
    assert {s["graph"] for s in report["sources"]} == {"equal", "different"}


def test_repeated_projection_forces_key_unknown(fixture):
    _, source, conn, cid = fixture
    annotate(conn, cid)
    conn.execute("INSERT INTO events(id,kind,conversation_id,timestamp) VALUES('repeat1','prompt',?,?),('repeat2','prompt',?,?)", (cid, TIME, cid, TIME))
    conn.commit()
    report = inspect(fixture)
    assert "repeated_projection" in codes(report)
    assert report["inventory"]["keyed_exposure"]["tool_call"]["unknown"] == 1
    assert report["conversations"][0]["repeated_projection_groups"] == 1
    assert source.exists()


def test_invalid_utf8_does_not_invent_record_denominator(fixture):
    _, source, _, _ = fixture
    source.write_bytes(b'{}\n{}\n\xff\n{}\n')
    candidates = inspect(fixture)["sources"][0]["candidates"]
    assert candidates["partial"] and candidates["uninspectable_records"] is None


@pytest.mark.parametrize("separator", [b'\v', b'\f', '\u2028'.encode()])
def test_non_newline_separators_between_objects_are_malformed(fixture, separator):
    _, source, _, _ = fixture
    source.write_bytes(b'{}' + separator + b'{}\n')
    assert "malformed" in codes(inspect(fixture))


def test_strip_semantics_and_optional_absence(fixture):
    _, source, _, _ = fixture
    minimal = [records()[0], {"type": "message", "timestamp": TIME},
               {"type": "message", "timestamp": TIME, "message": {"role": "user", "content": [{}]}},
               {"type": "message", "timestamp": TIME, "message": {"role": "assistant", "content": [
                   {"type": "toolCall"}, {"type": "thinking"}, {"type": "text"}, {},
               ]}},
               {"type": "message", "timestamp": TIME, "message": {"role": "toolResult"}},
               {"type": None, "timestamp": None, "opaque": [1, {}, None]}, {"type": "model_change"}]
    source.write_bytes(b'\n'.join(b'\v' + json.dumps(r).encode() + b'\f' for r in minimal))
    assert api._decode(source.read_bytes())[0] == load_jsonl(source)
    assert "malformed" not in codes(inspect(fixture))


@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00", "2026-01-01"])
def test_timestamp_gate_does_not_require_utc_conversion(fixture, timestamp):
    _, source, _, _ = fixture
    items = records()
    for row in items:
        row["timestamp"] = timestamp
    write_source(source, items)
    assert "invalid_timestamp" not in codes(inspect(fixture))


def test_source_symlink_and_expansion_refusal(fixture):
    db, source, _, _ = fixture
    alias = source.with_name("alias")
    alias.symlink_to(source)
    for path in (alias, Path("relative"), source.with_name("wild?card")):
        assert api.inspect_pi_identity(db_path=db, source_paths=(path,), orphan_conversation_ids=())["status"] == "refused"


def test_virtual_table_is_not_an_ordinary_required_table(fixture):
    _, _, conn, _ = fixture
    conn.execute("DROP TABLE conversation_owners")
    conn.execute("CREATE VIRTUAL TABLE conversation_owners USING fts5(conversation_id)")
    conn.commit()
    assert "unsupported_schema" in codes(inspect(fixture))


def test_recheck_occurs_after_stored_graph_comparison(fixture, monkeypatch):
    _, source, _, _ = fixture
    original = api._projection
    calls = 0

    def project(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 2:  # source projection first, then stored projection
            source.write_bytes(source.read_bytes() + b'\n')
        return result

    monkeypatch.setattr(api, "_projection", project)
    report = inspect(fixture)
    assert "source_changed_during_read" in codes(report)
    assert report["sources"][0]["graph"] == "unknown"


@pytest.mark.parametrize("mutation", ["write", "replace", "delete"])
def test_concurrent_source_change_with_barriers(fixture, monkeypatch, mutation):
    import threading

    _, source, _, _ = fixture
    ready, done = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            assert ready.wait(10)
            if mutation == "delete":
                source.unlink()
            elif mutation == "replace":
                replacement = source.with_name("new-inode")
                replacement.write_bytes(source.read_bytes())
                replacement.replace(source)
            else:
                source.write_bytes(source.read_bytes() + b'\n')
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    original = api._capture
    calls = 0

    def capture(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
            assert done.wait(10)
        return original(path)

    monkeypatch.setattr(api, "_capture", capture)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        report = inspect(fixture)
    finally:
        ready.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert "source_changed_during_read" in codes(report)
    assert report["sources"][0]["hash"] == report["sources"][0]["graph"] == "unknown"


def test_stat_fstat_change_during_initial_capture(fixture, monkeypatch):
    _, source, _, _ = fixture
    original = os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(source.read_bytes() + b'\n')
        return original(fd)

    monkeypatch.setattr(os, "fstat", changed)
    report = inspect(fixture)
    assert "source_changed_during_read" in codes(report)
    assert report["sources"][0]["candidates"] is None


@pytest.mark.parametrize("error", [sqlite3.ProgrammingError("binding bug"), sqlite3.OperationalError("SQL typo")])
def test_sql_programming_errors_propagate(fixture, monkeypatch, error):
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(storage, "open_database", broken)
    with pytest.raises(type(error)):
        inspect(fixture)


def test_readonly_helper_sidecar_refusal_is_not_retried(fixture, monkeypatch):
    from siftd.errors import DriftError

    calls = []

    def refuse(*args, **kwargs):
        calls.append(kwargs)
        raise DriftError(SECRET)

    monkeypatch.setattr(storage, "open_database", refuse)
    report = inspect(fixture)
    assert report["status"] == "refused" and "database_unavailable" in codes(report)
    assert calls == [{"read_only": True, "auto_upgrade": False}]
    assert SECRET not in json.dumps(report)


def test_candidate_occurrences_do_not_inflate_record_findings(fixture):
    _, source, _, _ = fixture
    items = records()
    del items[2]["id"]
    items[2]["message"]["content"] = [{"type": "toolCall"}, {"type": "toolCall"}]
    write_source(source, items)
    report = inspect(fixture)
    candidate = report["sources"][0]["candidates"]
    assert candidate["kinds"]["tool_call"]["missing"] == 2
    assert candidate["kinds"]["assistant"]["missing"] == 1
    assert next(f for f in report["findings"] if f["code"] == "missing_id")["total"] == 1


def test_node_block_order_is_not_sibling_order():
    events = [dict(id="p", kind="prompt", parent_id=None)]
    blocks = [dict(event_id="p", block_index=i, block_type="text", content=json.dumps(value)) for i, value in enumerate(["a", "b"])]
    original = api._projection(events, blocks, [])[0]
    blocks[0]["block_index"], blocks[1]["block_index"] = 1, 0
    assert api._projection(events, blocks, [])[0] != original
