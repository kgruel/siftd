"""The listing's per-row preamble check stays a per-conversation lookup.

``has_preamble`` is a correlated subquery, run once per listed row. Searched
through ``idx_events_parent`` (``parent_id IS NULL``), it walks every parentless
event in the database, because every prompt has a NULL parent too: the listing
then costs O(corpus) per row, ~20 ms a row on a 20k-session server.

Planner statistics are what choose that plan: on a corpus shaped like a real one
(tool calls chained by parent), ``sqlite_stat1`` reports ``parent_id`` as nearly
unique, since it averages rows per distinct value and NULL is one value. The
tests pin the plan with and without statistics.
"""

import pytest
from conftest import make_db

from siftd.api.conversations import _HAS_PREAMBLE_SQL
from siftd.storage.sqlite import open_database, refresh_planner_stats

_ROW_CHECK = f"SELECT {_HAS_PREAMBLE_SQL} FROM conversations c WHERE c.id = ?"


def _plan(conn, conversation_id: str) -> list[str]:
    return [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {_ROW_CHECK}", (conversation_id,))]


def _realistic_corpus(path, conversations: int = 5, tool_calls: int = 50):
    """Each conversation: a prompt, a response, then a chain of tool calls, each the next's parent."""
    make_db(path, conversations=[{"external_id": f"c{i}"} for i in range(conversations)])
    conn = open_database(path)
    for (cid,) in conn.execute("SELECT id FROM conversations").fetchall():
        (parent,) = conn.execute(
            "SELECT id FROM events WHERE conversation_id = ? AND kind = 'response'", (cid,)
        ).fetchone()
        for k in range(tool_calls):
            eid = f"{cid}-t{k}"
            conn.execute(
                "INSERT INTO events (id, kind, conversation_id, parent_id, external_id, timestamp)"
                " VALUES (?, 'tool_call', ?, ?, ?, '2026-01-01T00:00:00Z')",
                (eid, cid, parent, eid),
            )
            parent = eid
    conn.commit()
    return conn


@pytest.mark.parametrize("with_stats", [False, True], ids=["statless", "with-stats"])
def test_has_preamble_searches_one_conversations_events(tmp_path, with_stats):
    conn = _realistic_corpus(tmp_path / "t.db")
    if with_stats:
        refresh_planner_stats(conn, commit=True)
    conv = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]

    plan = " | ".join(_plan(conn, conv))

    assert "idx_events_parent" not in plan, f"walks every parentless event per row: {plan}"
    assert "idx_events_conversation_kind (conversation_id=? AND kind=?)" in plan, plan


def test_has_preamble_still_answers_the_rule(tmp_path):
    """The planner hint changes the plan, not the answer: a parentless response is a preamble."""
    make_db(tmp_path / "t.db", conversations=[{"external_id": "c0"}, {"external_id": "c1"}])
    conn = open_database(tmp_path / "t.db")
    plain, forked = (r[0] for r in conn.execute("SELECT id FROM conversations ORDER BY external_id"))
    conn.execute(
        "UPDATE events SET parent_id = NULL WHERE conversation_id = ? AND kind = 'response'", (forked,)
    )
    conn.commit()

    assert conn.execute(_ROW_CHECK, (plain,)).fetchone()[0] == 0
    assert conn.execute(_ROW_CHECK, (forked,)).fetchone()[0] == 1
