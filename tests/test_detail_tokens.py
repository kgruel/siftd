"""The conversation detail reports tokens the way the listing (the usage rollup) does.

Since 0.9 every aggregate's ``input_tokens`` is the true total, cache included,
normalized across provider conventions. The detail summed raw
``event_response.input_tokens`` instead: uncached-only for Anthropic, so a cached
Claude session's ``siftd show`` reported a fraction of the ``siftd query`` row's
tokens (161 of 458 conversations over two weeks on a real server, up to 180x).
The listing's own live fallbacks (a row the rollup hasn't reached, a DB with no
derived tier) summed raw input too, so a row changed at the next rebuild.
"""

import sqlite3

import pytest
from painted import Fidelity

import siftd.storage.sqlite as sq
from siftd.api.conversations import get_conversation, list_conversations
from siftd.storage.attributes import set_attribute
from siftd.storage.usage_rollup import rebuild_rollups

# (source, raw input, cache_read, cache_creation) -> true total input
CASES = [
    ("anthropic", 1000, 5000, 2000, 8000),  # exclusive: input + read + creation
    ("openai", 8000, 5000, 0, 8000),  # inclusive: input already holds the read
    ("multi", 100, 5000, 0, 5100),  # per-row signature: input < read => exclusive
    ("anthropic", 1000, 0, 0, 1000),  # no cache: unchanged
]


def _conv(db, source, inp, cache_read, cache_creation, out=100):
    conn = sq.create_database(db)
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    h = sq.get_or_create_harness(conn, "tool", source=source)
    m = sq.get_or_create_model(conn, "the-model")
    p = sq.get_or_create_provider(conn, source)
    cid = sq.insert_conversation(conn, "c1", h, ws, "2024-01-01T00:00:00Z")
    for i in range(2):  # two turns, so per-turn and per-conversation sums both count
        pid = sq.insert_prompt(conn, cid, f"p{i}", f"2024-01-01T00:0{i}:00Z")
        eid = sq.insert_response(conn, cid, pid, m, p, f"r{i}", f"2024-01-01T00:0{i}:01Z", inp, out)
        if cache_read:
            set_attribute(conn, "response", eid, "cache_read_input_tokens", str(cache_read), scope="provider")
        if cache_creation:
            set_attribute(conn, "response", eid, "cache_creation_input_tokens", str(cache_creation), scope="provider")
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    return cid


@pytest.mark.parametrize(("source", "inp", "cache_read", "cache_creation", "total"), CASES)
def test_detail_tokens_match_the_listing(tmp_path, source, inp, cache_read, cache_creation, total):
    db = tmp_path / "t.db"
    cid = _conv(db, source, inp, cache_read, cache_creation)

    [row] = list_conversations(fidelity=Fidelity(), db_path=db, n=0)
    detail = get_conversation(cid, fidelity=Fidelity(), db_path=db)

    assert row.total_tokens == 2 * (total + 100)
    assert detail.total_input_tokens == 2 * total
    assert detail.total_input_tokens + detail.total_output_tokens == row.total_tokens
    assert [t.total_input_tokens for t in detail.turns] == [total, total]


@pytest.mark.parametrize("tier", ["row-not-rolled-up", "no-derived-tier"])
@pytest.mark.parametrize(("source", "inp", "cache_read", "cache_creation", "total"), CASES)
def test_the_listing_reads_the_same_before_the_rollup(tmp_path, tier, source, inp, cache_read, cache_creation, total):
    db = tmp_path / "t.db"
    _conv(db, source, inp, cache_read, cache_creation)
    conn = sqlite3.connect(db)
    if tier == "row-not-rolled-up":
        conn.execute("DELETE FROM conversation_stats")  # the COALESCE's live subquery answers
    else:
        conn.execute("DROP TABLE conversation_stats")  # the no-stats fallback answers
    conn.commit()
    conn.close()

    [row] = list_conversations(fidelity=Fidelity(), db_path=db, n=0)

    assert row.total_tokens == 2 * (total + 100)


def test_live_tokens_are_each_conversations_own(tmp_path):
    """The live subquery is correlated: a flatten alias that captured ``c`` summed every conversation into every row."""
    db = tmp_path / "t.db"
    conn = sq.create_database(db)
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    h = sq.get_or_create_harness(conn, "tool", source="anthropic")
    m = sq.get_or_create_model(conn, "the-model")
    p = sq.get_or_create_provider(conn, "anthropic")
    expected = {}
    for i, (inp, cached) in enumerate([(10, 1000), (20, 0), (30, 300)]):
        cid = sq.insert_conversation(conn, f"c{i}", h, ws, f"2024-01-0{i + 1}T00:00:00Z")
        pid = sq.insert_prompt(conn, cid, f"p{i}", f"2024-01-0{i + 1}T00:00:00Z")
        eid = sq.insert_response(conn, cid, pid, m, p, f"r{i}", f"2024-01-0{i + 1}T00:00:01Z", inp, 5)
        if cached:
            set_attribute(conn, "response", eid, "cache_read_input_tokens", str(cached), scope="provider")
        expected[cid] = inp + cached + 5
    rebuild_rollups(conn)
    conn.execute("DELETE FROM conversation_stats")
    conn.commit()
    conn.close()

    rows = list_conversations(fidelity=Fidelity(), db_path=db, n=0)

    assert {r.id: r.total_tokens for r in rows} == expected
