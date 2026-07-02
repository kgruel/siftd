"""Tests for TargetRef — the unified parse → resolve → alias tag-target layer."""

import pytest

from siftd.api.conversations import AmbiguousPrefix
from siftd.api.target_ref import ResolvedTarget, TargetRef, alias, resolve
from siftd.storage.sqlite import create_database, get_or_create_harness


def _make_db(tmp_path):
    """A DB with one conversation and prompt/response/tool_call events."""
    db_path = tmp_path / "t.db"
    conn = create_database(db_path)
    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
    conv_id = "01CONVAAAAAAAAAAAAAAAAAAAA"
    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (conv_id, "ext", harness_id, "2024-01-01T00:00:00Z"),
    )
    events = [
        ("01EVTPROMPT0000000000000001", "prompt", "2024-01-01T00:00:01Z"),
        ("01EVTRESP000000000000000002", "response", "2024-01-01T00:00:02Z"),
        ("01EVTPROMPT0000000000000003", "prompt", "2024-01-01T00:00:03Z"),
        ("01EVTTOOL000000000000000004", "tool_call", "2024-01-01T00:00:04Z"),
    ]
    for eid, kind, ts in events:
        conn.execute(
            "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, ?, ?, ?)",
            (eid, kind, conv_id, ts),
        )
    conn.commit()
    return db_path, conn, conv_id


# --- parse round-trips -----------------------------------------------------

def test_from_colon_path_valid():
    ref = TargetRef.from_colon_path("01CONV:response:2")
    assert ref == TargetRef(conv_ref="01CONV", kind="response", position=2)


@pytest.mark.parametrize("bad", ["01CONV", "01CONV:response", "a:b:c", "01CONV:response:0", "01CONV::1"])
def test_from_colon_path_rejects(bad):
    assert TargetRef.from_colon_path(bad) is None


def test_from_positional_bare_id():
    ref, tags = TargetRef.from_positional(["01ABC", "foo", "bar"])
    assert ref == TargetRef(raw_id="01ABC")
    assert tags == ["foo", "bar"]


def test_from_positional_kind_word():
    ref, tags = TargetRef.from_positional(["response", "01ABC", "foo"])
    assert ref == TargetRef(raw_id="01ABC", kind="response")
    assert tags == ["foo"]


def test_from_positional_too_short():
    assert TargetRef.from_positional(["01ABC"]) is None
    assert TargetRef.from_positional(["response", "01ABC"]) is None


def test_from_wire_and_markers():
    assert TargetRef.from_wire({"entity_type": "prompt", "entity_id": "01X"}) == TargetRef(raw_id="01X", kind="prompt")
    # wire `last` means "N recent conversations" — a selection count, not a
    # target address — so from_wire ignores it (no exchange_index pun).
    assert TargetRef.from_wire({"last": 3}) == TargetRef(raw_id=None, kind=None)
    assert TargetRef.from_markers(last_marker="last_response").last_marker == "last_response"
    with pytest.raises(ValueError, match="Invalid last marker"):
        TargetRef.from_markers(last_marker="last_bogus")


# --- resolve ---------------------------------------------------------------

def test_resolve_colon_path(tmp_path):
    _, conn, conv_id = _make_db(tmp_path)
    got = resolve(conn, TargetRef.from_colon_path(f"{conv_id}:prompt:2"))
    assert got == ResolvedTarget("prompt", "01EVTPROMPT0000000000000003")


def test_resolve_colon_exchange_anchors_on_prompt(tmp_path):
    _, conn, conv_id = _make_db(tmp_path)
    got = resolve(conn, TargetRef.from_colon_path(f"{conv_id}:exchange:1"))
    assert got == ResolvedTarget("exchange", "01EVTPROMPT0000000000000001")


def test_resolve_colon_out_of_range(tmp_path):
    _, conn, conv_id = _make_db(tmp_path)
    with pytest.raises(IndexError):
        resolve(conn, TargetRef.from_colon_path(f"{conv_id}:tool_call:5"))


def test_resolve_bare_event_full_ulid(tmp_path):
    _, conn, _ = _make_db(tmp_path)
    got = resolve(conn, TargetRef(raw_id="01EVTRESP000000000000000002"))
    assert got == ResolvedTarget("response", "01EVTRESP000000000000000002")


def test_resolve_bare_conversation_full_ulid(tmp_path):
    _, conn, conv_id = _make_db(tmp_path)
    got = resolve(conn, TargetRef(raw_id=conv_id))
    assert got == ResolvedTarget("conversation", conv_id)


def test_resolve_kind_narrowed(tmp_path):
    _, conn, _ = _make_db(tmp_path)
    got = resolve(conn, TargetRef(raw_id="01EVTTOOL", kind="tool_call"))
    assert got == ResolvedTarget("tool_call", "01EVTTOOL000000000000000004")


def test_resolve_not_found(tmp_path):
    _, conn, _ = _make_db(tmp_path)
    with pytest.raises(LookupError):
        resolve(conn, TargetRef(raw_id="01NOSUCHTHING"))


def test_resolve_cross_kind_ambiguous(tmp_path):
    """A prefix shared by conversation + event raises AmbiguousPrefix with kinds."""
    db_path = tmp_path / "collide.db"
    conn = create_database(db_path)
    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
    shared = "01SHARED"
    conv_id = shared + "CONV0000000000000A"
    evt_id = shared + "EVT00000000000000B"
    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (conv_id, "ext", harness_id, "2024-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, 'prompt', ?, ?)",
        (evt_id, conv_id, "2024-01-01T00:00:01Z"),
    )
    conn.commit()
    with pytest.raises(AmbiguousPrefix) as exc:
        resolve(conn, TargetRef(raw_id=shared))
    assert exc.value.candidate_kinds is not None
    assert set(exc.value.candidate_kinds) == {"conversation", "prompt"}


def test_resolve_cross_kind_owner_scopes_events(tmp_path):
    """An owner-scoped caller cannot resolve another tenant's event by ULID."""
    db_path, conn, _ = _make_db(tmp_path)
    # Assign the conversation (and thus its events) to user 'alice'.
    conn.execute(
        "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at) "
        "VALUES (?, 'alice', NULL, '2024-01-01T00:00:00Z')",
        ("01CONVAAAAAAAAAAAAAAAAAAAA",),
    )
    conn.commit()

    # Alice resolves her own event by full ULID.
    got = resolve(conn, TargetRef(raw_id="01EVTRESP000000000000000002"), owner="alice")
    assert got == ResolvedTarget("response", "01EVTRESP000000000000000002")

    # Bob (no ownership) cannot — the event is invisible, so it's not found.
    with pytest.raises(LookupError):
        resolve(conn, TargetRef(raw_id="01EVTRESP000000000000000002"), owner="bob")


def test_resolve_deferred_marker_raises(tmp_path):
    _, conn, _ = _make_db(tmp_path)
    with pytest.raises(ValueError, match="resolve at ingest"):
        resolve(conn, TargetRef.from_markers(last_marker="last_prompt"))


# --- alias round-trips resolve --------------------------------------------

@pytest.mark.parametrize("colon", ["prompt:1", "prompt:2", "response:1", "tool_call:1", "exchange:2"])
def test_alias_roundtrips_resolve(tmp_path, colon):
    _, conn, conv_id = _make_db(tmp_path)
    kind, n = colon.split(":")
    resolved = resolve(conn, TargetRef.from_colon_path(f"{conv_id}:{colon}"))
    got = alias(conn, resolved.target_kind, resolved.target_id)
    assert got == f"{conv_id[:12]}:{kind}:{n}"


def test_alias_conversation(tmp_path):
    _, conn, conv_id = _make_db(tmp_path)
    assert alias(conn, "conversation", conv_id) == conv_id[:12]
