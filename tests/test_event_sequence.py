"""events.sequence: the recorded order of a conversation, and every reader that follows it.

Order used to be ``(timestamp, id)``, and ULIDs are random within a
millisecond, so events that share a timestamp resolved arbitrarily: an adapter
that stamps every event with the session start (aider) made an exchange-index
lookup return the wrong prompt at 38 of 40 positions in a reproduction.
``store_conversation`` now assigns each event its tree-walk position; legacy
rows keep a NULL sequence and readers fall back to ``(timestamp, id)``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from siftd.api.target_ref import TargetRef, alias, resolve
from siftd.domain import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall
from siftd.storage.events import (
    get_event_position,
    get_event_tree,
    get_last_event_id,
    get_preceding_prompt_id,
    get_prompt_by_index,
    get_prompts_for_conv,
    get_responses_for_prompt,
    get_tool_calls_for_response,
)
from siftd.storage.fts import fts5_event_turn_index
from siftd.storage.queries import (
    fetch_event_turn_number,
    fetch_prompts_for_conversation,
    fetch_responses_for_conversation,
    fetch_tool_calls_for_conversation,
)
from siftd.storage.sqlite import SCHEMA_VERSION, create_database, open_database, store_conversation

SAME_INSTANT = "2024-03-01T10:00:00Z"
FIXTURES = Path(__file__).parent / "fixtures" / "schemas"


def _text(s: str) -> list[ContentBlock]:
    return [ContentBlock(block_type="text", content={"text": s})]


def _response(key: str, ts: str, *, tools: tuple[str, ...] = ()) -> Response:
    return Response(
        timestamp=ts,
        external_id=key,
        model="test-model",
        content=_text(f"answer {key}"),
        tool_calls=[ToolCall(tool_name="Read", input={"file_path": f"/{t}"}, external_id=t, timestamp=ts) for t in tools],
    )


def _conversation(external_id: str, prompts: list[Prompt], preamble: list[Response] | None = None) -> Conversation:
    return Conversation(
        external_id=external_id,
        harness=Harness(name="test_harness", source="test", log_format="jsonl"),
        started_at=SAME_INSTANT,
        prompts=prompts,
        preamble=preamble or [],
    )


def _aider_like(n: int = 40) -> Conversation:
    """n exchanges, every event stamped with the session start, as aider does."""
    return _conversation(
        "aider-1",
        [
            Prompt(timestamp=SAME_INSTANT, external_id=f"p{i}", content=_text(f"question {i}"),
                   responses=[_response(f"r{i}", SAME_INSTANT)])
            for i in range(n)
        ],
    )


@pytest.fixture
def conn(tmp_path):
    c = create_database(tmp_path / "t.db")
    yield c
    c.close()


def _external_ids(conn, ids: list[str]) -> list[str]:
    return [conn.execute("SELECT external_id FROM events WHERE id = ?", (i,)).fetchone()[0] for i in ids]


class TestSequenceAssignment:
    def test_sequence_follows_the_tree_walk_preamble_first(self, conn):
        conv = _conversation(
            "walk-1",
            [
                Prompt(timestamp="2024-03-01T10:00:01Z", external_id="p1", content=_text("one"),
                       responses=[_response("r1", "2024-03-01T10:00:02Z", tools=("t1", "t2")),
                                  _response("r2", "2024-03-01T10:00:03Z", tools=("t3",))]),
                Prompt(timestamp="2024-03-01T10:00:04Z", external_id="p2", content=_text("two"),
                       responses=[_response("r3", "2024-03-01T10:00:05Z")]),
            ],
            preamble=[_response("pre", "2024-03-01T09:59:00Z", tools=("tp",))],
        )
        cid = store_conversation(conn, conv, commit=True)

        rows = conn.execute(
            "SELECT external_id, sequence FROM events WHERE conversation_id = ? ORDER BY sequence", (cid,)
        ).fetchall()
        assert [r["external_id"] for r in rows] == ["pre", "tp", "p1", "r1", "t1", "t2", "r2", "t3", "p2", "r3"]
        assert [r["sequence"] for r in rows] == list(range(10))

    def test_each_conversation_counts_from_zero(self, conn):
        a = store_conversation(conn, _aider_like(3), commit=True)
        other = _aider_like(3)
        other.external_id = "aider-2"
        b = store_conversation(conn, other, commit=True)
        for cid in (a, b):
            seqs = [r[0] for r in conn.execute("SELECT sequence FROM events WHERE conversation_id = ? ORDER BY sequence", (cid,))]
            assert seqs == list(range(6))

    def test_a_fresh_database_has_the_column_and_its_index(self, conn):
        assert "sequence" in {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        indexes = {r[1]: [c[2] for c in conn.execute(f"PRAGMA index_info({r[1]})")] for r in conn.execute("PRAGMA index_list(events)")}
        assert indexes["idx_events_conversation_sequence"] == ["conversation_id", "sequence"]


@pytest.fixture(params=["as_stored", "scrambled"])
def flip(request) -> bool:
    """Whether to re-sequence a conversation's exchanges out of insertion order after storing.

    With equal timestamps and no tie-break, SQLite returns ties in scan order,
    which is insertion order, so a reader that ignores ``sequence`` can look
    correct by plan accident. Permuting the sequences makes recorded order differ
    from insertion order, so only a reader that actually orders by it can pass.
    The permutation (position = 7 * i mod n, a bijection when gcd(7, n) = 1) is
    neither the identity nor a mirror, so a window around one prompt is not
    symmetric under it.
    """
    return request.param == "scrambled"


def _scramble(i: int, n: int) -> int:
    return (7 * i) % n


def _expected(n: int, flip: bool) -> list[int]:
    """Which original index sits at each recorded position."""
    return sorted(range(n), key=lambda i: _scramble(i, n)) if flip else list(range(n))


def _apply(conn, cid: str, n: int, flip: bool) -> None:
    """Scramble an `_aider_like(n)` conversation's exchanges (a prompt and a response each)."""
    if flip:
        conn.execute(
            "UPDATE events SET sequence = 2 * ((7 * (sequence / 2)) % ?) + sequence % 2 WHERE conversation_id = ?",
            (n, cid),
        )
        conn.commit()


class TestEqualTimestamps:
    """The 38-of-40 case: every event shares one instant, so only sequence can say which is which."""

    def test_every_exchange_index_resolves_to_its_own_prompt(self, conn):
        cid = store_conversation(conn, _aider_like(40), commit=True)
        for i in range(1, 41):
            assert _external_ids(conn, [get_prompt_by_index(conn, cid, i)]) == [f"p{i - 1}"], i

    def test_the_prompt_list_and_tree_are_in_recorded_order(self, conn, flip):
        cid = store_conversation(conn, _aider_like(40), commit=True)
        _apply(conn, cid, 40, flip)
        want = [f"p{i}" for i in _expected(40, flip)]
        assert _external_ids(conn, [r["id"] for r in get_prompts_for_conv(conn, cid)]) == want
        tree = get_event_tree(conn, cid)
        assert [p["external_id"] for p in tree["prompts"]] == want
        assert _external_ids(conn, [r["id"] for r in fetch_prompts_for_conversation(conn, cid)]) == want

    def test_last_event_is_the_last_recorded_not_the_largest_ulid(self, conn):
        cid = store_conversation(conn, _aider_like(40), commit=True)
        assert _external_ids(conn, [get_last_event_id(conn, cid, "prompt")]) == ["p39"]
        assert _external_ids(conn, [get_last_event_id(conn, cid, "response")]) == ["r39"]

    def test_colon_paths_resolve_and_alias_back(self, conn):
        cid = store_conversation(conn, _aider_like(40), commit=True)
        for n in (1, 7, 23, 40):
            target = resolve(conn, TargetRef.from_colon_path(f"{cid}:prompt:{n}"))
            assert _external_ids(conn, [target.target_id]) == [f"p{n - 1}"]
            assert alias(conn, "prompt", target.target_id) == f"{cid[:12]}:prompt:{n}"

    def test_turn_indices_follow_the_prompt_a_response_answers(self, conn):
        cid = store_conversation(conn, _aider_like(40), commit=True)
        by_key = {r["external_id"]: r for r in conn.execute(
            "SELECT id, external_id, kind, sequence, timestamp, parent_id, conversation_id FROM events WHERE conversation_id = ?", (cid,))}
        for i in (0, 5, 39):
            assert fts5_event_turn_index(conn, by_key[f"p{i}"]["id"], cid) == i
            assert fts5_event_turn_index(conn, by_key[f"r{i}"]["id"], cid) == i
            # the parent-chain walk and the position walk must agree
            assert fetch_event_turn_number(conn, by_key[f"r{i}"]) == i + 1

    def test_neighbours_are_the_adjacent_prompts(self, conn):
        from siftd.api.events import get_event

        cid = store_conversation(conn, _aider_like(40), commit=True)
        mid = get_prompt_by_index(conn, cid, 20)
        detail = get_event(mid, conn=conn, include_neighbors=True)
        assert detail is not None and detail.neighbors is not None
        assert _external_ids(conn, [detail.neighbors["prev_event_id"], detail.neighbors["next_event_id"]]) == ["p18", "p20"]

    def test_responses_and_tool_calls_keep_recorded_order_under_one_parent(self, conn, flip):
        conv = _conversation("kids", [Prompt(
            timestamp=SAME_INSTANT, external_id="p", content=_text("q"),
            responses=[_response(f"r{i}", SAME_INSTANT, tools=(f"t{i}a", f"t{i}b")) for i in range(15)],
        )])
        cid = store_conversation(conn, conv, commit=True)
        if flip:  # re-sequence the 15 [response, call, call] blocks out of insertion order
            conn.execute(
                "UPDATE events SET sequence = 1 + 3 * ((7 * ((sequence - 1) / 3)) % 15) + (sequence - 1) % 3"
                " WHERE conversation_id = ? AND sequence >= 1", (cid,),
            )
            conn.commit()
        order = _expected(15, flip)
        pid = get_prompts_for_conv(conn, cid)[0]["id"]
        responses = get_responses_for_prompt(conn, pid)
        assert _external_ids(conn, [r["id"] for r in responses]) == [f"r{i}" for i in order]
        for i, r in zip(order, responses, strict=True):
            assert _external_ids(conn, [t["id"] for t in get_tool_calls_for_response(conn, r["id"])]) == [f"t{i}a", f"t{i}b"]
        assert _external_ids(conn, [r["id"] for r in fetch_responses_for_conversation(conn, cid)]) == [f"r{i}" for i in order]
        flat = [r["external_id"] for r in fetch_tool_calls_for_conversation(conn, cid)]
        assert flat == [f"t{i}{s}" for i in order for s in "ab"]


class TestExchangeAndSearchReaders:
    """The readers search and embedding sit on resolve a tie the same way the primitives do."""

    def test_exchanges_come_back_in_recorded_order(self, conn, flip):
        from siftd.storage.queries import fetch_conversation_exchanges, fetch_exchanges

        cid = store_conversation(conn, _aider_like(40), commit=True)
        _apply(conn, cid, 40, flip)
        want = [f"question {i}" for i in _expected(40, flip)]
        rows = fetch_exchanges(conn, conversation_id=cid)
        assert [r.prompt_text for r in rows] == want
        # the batched prompt_ids branch sorts in Python and must agree with SQL
        assert [r.prompt_text for r in fetch_exchanges(conn, prompt_ids=[r.prompt_id for r in reversed(rows)])] == want
        assert [r.prompt_text for r in fetch_exchanges(conn)] == want
        grouped = fetch_conversation_exchanges(conn, conversation_id=cid)[cid]
        assert [g["text"].split("\n")[0] for g in grouped] == want

    def test_prompt_ids_spanning_several_batches_are_merged_in_recorded_order(self, conn, flip):
        """fetch_exchanges looks prompt ids up 500 at a time and must restore the global order itself."""
        from siftd.storage.queries import fetch_exchanges

        cid = store_conversation(conn, _aider_like(520), commit=True)
        _apply(conn, cid, 520, flip)
        in_order = [r["id"] for r in get_prompts_for_conv(conn, cid)]
        assert [r.prompt_id for r in fetch_exchanges(conn, prompt_ids=list(reversed(in_order)))] == in_order

    def test_context_windows_and_turn_indices_use_the_same_order(self, conn, flip):
        from siftd.api.search import _events_to_turn_indices, enrich_context_window
        from siftd.domain import SearchChunk

        cid = store_conversation(conn, _aider_like(40), commit=True)
        _apply(conn, cid, 40, flip)
        order = _expected(40, flip)  # order[k] = which p/r pair sits at turn k
        ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM events WHERE conversation_id = ?", (cid,))}
        turn_of = {n: order.index(n) for n in (3, 30)}
        events = [ids["p3"], ids["r3"], ids["p30"], ids["r30"]]
        assert _events_to_turn_indices(conn, events, cid) == [turn_of[3], turn_of[3], turn_of[30], turn_of[30]]

        chunk = SearchChunk(conversation_id=cid, score=1.0, text="t", chunk_type="exchange", source_ids=[ids["p10"]])
        enrich_context_window(conn, [chunk], 2)
        assert chunk.context_window is not None
        k = order.index(10)
        assert [pt for _, pt, _, _ in chunk.context_window] == [f"question {order[i]}" for i in range(k - 2, k + 3)]
        assert [is_source for *_, is_source in chunk.context_window] == [False, False, True, False, False]

    def test_file_refs_keep_the_order_the_calls_were_made(self, tmp_path, flip):
        from siftd.api.file_refs import fetch_file_refs
        from siftd.storage.sqlite import ensure_tool_aliases, get_or_create_harness

        conn = open_database(tmp_path / "refs.db")  # the write open seeds the canonical tools
        ensure_tool_aliases(conn, get_or_create_harness(conn, "claude_code", source="anthropic"), {"Read": "file.read"})
        names = [f"f{i}.py" for i in range(12)]
        conv = _conversation("refs", [Prompt(
            timestamp=SAME_INSTANT, external_id="p", content=_text("q"),
            responses=[Response(timestamp=SAME_INSTANT, external_id="r", model="m", content=_text("a"),
                                tool_calls=[ToolCall(tool_name="Read", input={"file_path": f"/x/{n}"}, external_id=f"t{i}", timestamp=SAME_INSTANT)
                                            for i, n in enumerate(names)])],
        )])
        conv.harness = Harness(name="claude_code", source="anthropic", log_format="jsonl")
        cid = store_conversation(conn, conv, commit=True)
        if flip:  # the calls were recorded out of insertion order
            conn.execute(
                "UPDATE events SET sequence = 2 + (7 * (sequence - 2)) % 12 WHERE conversation_id = ? AND kind = 'tool_call'", (cid,)
            )
            conn.commit()
        pid = get_prompts_for_conv(conn, cid)[0]["id"]
        refs = fetch_file_refs(conn, [pid])[pid]
        conn.close()
        assert [r.basename for r in refs] == [names[i] for i in _expected(12, flip)]


class TestReaderOrder:
    """Sequence wins when present; (timestamp, id) is the fallback for NULL."""

    def _stored(self, conn):
        conv = _conversation("order-1", [
            Prompt(timestamp=f"2024-03-01T10:00:0{i}Z", external_id=f"p{i}", content=_text("q"),
                   responses=[_response(f"r{i}", f"2024-03-01T10:00:0{i}Z")])
            for i in range(5)
        ])
        return store_conversation(conn, conv, commit=True)

    def test_sequence_overrides_timestamp(self, conn):
        cid = self._stored(conn)
        # Reverse the recorded order: the readers must follow it, not the clock.
        conn.execute("UPDATE events SET sequence = 100 - sequence WHERE conversation_id = ?", (cid,))
        assert _external_ids(conn, [r["id"] for r in get_prompts_for_conv(conn, cid)]) == ["p4", "p3", "p2", "p1", "p0"]
        assert _external_ids(conn, [get_prompt_by_index(conn, cid, 1)]) == ["p4"]
        assert _external_ids(conn, [get_last_event_id(conn, cid, "prompt")]) == ["p0"]

    def test_null_sequence_falls_back_to_timestamp_then_id(self, conn):
        cid = self._stored(conn)
        conn.execute("UPDATE events SET sequence = NULL WHERE conversation_id = ?", (cid,))
        assert _external_ids(conn, [r["id"] for r in get_prompts_for_conv(conn, cid)]) == [f"p{i}" for i in range(5)]
        assert _external_ids(conn, [get_prompt_by_index(conn, cid, 5)]) == ["p4"]
        assert _external_ids(conn, [get_last_event_id(conn, cid, "prompt")]) == ["p4"]

    def test_null_sequence_ties_fall_back_to_id(self, conn):
        cid = store_conversation(conn, _aider_like(10), commit=True)
        conn.execute("UPDATE events SET sequence = NULL WHERE conversation_id = ?", (cid,))
        ids = [r["id"] for r in get_prompts_for_conv(conn, cid)]
        assert ids == sorted(ids)  # (timestamp, id) with one timestamp is id order

    def test_a_legacy_response_sharing_its_prompts_timestamp_still_follows_it(self, conn):
        """The NULL-sequence rule keeps its old tie behaviour: same instant counts as 'after the prompt'."""
        conv = _conversation("legacy-tie", [
            Prompt(timestamp="2024-03-01T10:00:01Z", external_id="p1", content=_text("q"),
                   responses=[_response("r1", "2024-03-01T10:00:01Z")]),
            Prompt(timestamp="2024-03-01T10:00:02Z", external_id="p2", content=_text("q"),
                   responses=[_response("r2", "2024-03-01T10:00:02Z")]),
        ])
        cid = store_conversation(conn, conv, commit=True)
        conn.execute("UPDATE events SET sequence = NULL WHERE conversation_id = ?", (cid,))
        r2 = conn.execute(
            "SELECT id, kind, sequence, timestamp FROM events WHERE external_id = 'r2' AND conversation_id = ?", (cid,)
        ).fetchone()
        assert _external_ids(conn, [get_preceding_prompt_id(conn, cid, r2)]) == ["p2"]

    def test_a_preamble_response_precedes_every_prompt(self, conn):
        conv = _conversation("pre", [Prompt(timestamp="2024-03-01T10:00:05Z", external_id="p", content=_text("q"),
                                            responses=[_response("r", "2024-03-01T10:00:06Z")])],
                             preamble=[_response("pre", "2024-03-01T09:00:00Z")])
        cid = store_conversation(conn, conv, commit=True)
        pre_id = conn.execute("SELECT id FROM events WHERE external_id = 'pre'").fetchone()[0]
        assert fts5_event_turn_index(conn, pre_id, cid) is None
        assert get_event_position(conn, pre_id) == 1  # first response in recorded order


class TestLegacyDatabase:
    """A v12 database migrates in place: columns appear, nothing is backfilled, everything still reads."""

    def _v12_database(self, tmp_path) -> Path:
        path = tmp_path / "v12.db"
        raw = sqlite3.connect(path)
        raw.executescript((FIXTURES / "v12.sql").read_text())
        raw.executescript(
            """
            INSERT INTO harnesses (id, name) VALUES ('h1', 'legacy');
            INSERT INTO conversations (id, external_id, harness_id, started_at)
                VALUES ('c1', 'legacy-1', 'h1', '2024-01-01T00:00:00Z');
            INSERT INTO events (id, kind, conversation_id, external_id, timestamp) VALUES
                ('01P1', 'prompt', 'c1', 'p1', '2024-01-01T00:00:01Z'),
                ('01P2', 'prompt', 'c1', 'p2', '2024-01-01T00:00:02Z'),
                ('01P3', 'prompt', 'c1', 'p3', '2024-01-01T00:00:03Z');
            """
        )
        raw.commit()
        raw.close()
        return path

    def test_migration_adds_the_columns_and_leaves_existing_rows_null(self, tmp_path):
        path = self._v12_database(tmp_path)
        raw = sqlite3.connect(path)
        assert "sequence" not in {r[1] for r in raw.execute("PRAGMA table_info(events)")}
        raw.close()

        conn = open_database(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 13
            assert conn.execute("SELECT COUNT(*), COUNT(sequence) FROM events").fetchone()[:] == (3, 0)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "conversation_links" in tables
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

    def test_legacy_rows_read_in_timestamp_order_and_new_ones_by_sequence(self, tmp_path):
        conn = open_database(self._v12_database(tmp_path))
        try:
            assert get_prompt_by_index(conn, "c1", 3) == "01P3"
            assert [r["id"] for r in get_prompts_for_conv(conn, "c1")] == ["01P1", "01P2", "01P3"]
            assert get_last_event_id(conn, "c1", "prompt") == "01P3"
            assert get_event_position(conn, "01P2") == 2

            # A conversation stored after the upgrade is sequenced; the legacy one is untouched.
            new = store_conversation(conn, _aider_like(4), commit=True)
            assert conn.execute("SELECT COUNT(sequence) FROM events WHERE conversation_id = ?", (new,)).fetchone()[0] == 8
            assert conn.execute("SELECT COUNT(sequence) FROM events WHERE conversation_id = 'c1'").fetchone()[0] == 0
        finally:
            conn.close()

    def test_migrating_twice_is_a_no_op(self, tmp_path):
        path = self._v12_database(tmp_path)
        open_database(path).close()
        conn = open_database(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        finally:
            conn.close()
