"""Planner statistics are a maintained state, not something a user has to ask for (#80).

With no ``sqlite_stat1`` SQLite picks join order from defaults, and for the
replacement snapshot's event-tag join that meant driving from every event-kind
tag assignment in the database instead of one conversation's events. These
tests pin that the write paths which change the corpus's size leave stats
behind, and that stats are what flip that plan.
"""

from conftest import make_db

from siftd.api.receive import receive_database
from siftd.storage.sqlite import open_database, refresh_planner_stats
from siftd.storage.tags import apply_tag, get_or_create_tag

# The snapshot query from storage/replacement.py, with its CROSS JOIN pin
# removed: what the planner chooses on its own.
_UNPINNED_SNAPSHOT = """
    SELECT ta.target_kind, e.kind, e.external_id, ta.tag_id, ta.applied_at
    FROM events e
    JOIN tag_assignments ta ON ta.target_id = e.id
    WHERE ta.target_kind IN ('prompt', 'response', 'tool_call') AND e.conversation_id = ?
"""


def _has_stats(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_stat1'"
    ).fetchone() is not None


def _first_table(conn, conversation_id: str) -> str:
    detail = conn.execute(f"EXPLAIN QUERY PLAN {_UNPINNED_SNAPSHOT}", (conversation_id,)).fetchone()[3]
    return detail.split()[1]


def _tagged_corpus(path, n: int = 120):
    make_db(path, conversations=[{"external_id": f"c{i}"} for i in range(n)])
    conn = open_database(path)
    tag_id = get_or_create_tag(conn, "t")
    for (event_id,) in conn.execute("SELECT id FROM events WHERE kind = 'prompt'").fetchall():
        apply_tag(conn, "prompt", event_id, tag_id)
    conn.commit()
    return conn


def test_stats_flip_the_unpinned_snapshot_join(tmp_path):
    conn = _tagged_corpus(tmp_path / "t.db")
    conv = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]

    assert not _has_stats(conn)
    assert _first_table(conn, conv) == "ta", "the statless plan this guards against"

    refresh_planner_stats(conn)

    assert _has_stats(conn)
    assert _first_table(conn, conv) == "e"
    conn.close()


def test_refresh_with_existing_stats_is_safe_to_repeat(tmp_path):
    conn = _tagged_corpus(tmp_path / "t.db", n=10)
    refresh_planner_stats(conn)
    before = conn.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0]
    refresh_planner_stats(conn)
    refresh_planner_stats(conn, full=True)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] >= before > 0
    assert not conn.in_transaction
    conn.close()


def test_merge_leaves_stats(tmp_path):
    """A receive-only server grows by merges alone."""
    target = make_db(tmp_path / "target.db", conversations=[{"external_id": "a"}])
    source = make_db(tmp_path / "source.db", conversations=[{"external_id": "b"}])
    conn = open_database(target, read_only=True)
    assert not _has_stats(conn)
    conn.close()

    assert receive_database(source, target)["status"] == "merged"

    conn = open_database(target, read_only=True)
    assert _has_stats(conn)
    conn.close()


def test_receive_create_leaves_stats(tmp_path):
    source = make_db(tmp_path / "source.db", conversations=[{"external_id": "b"}])
    target = tmp_path / "fresh.db"

    assert receive_database(source, target)["status"] == "created"

    conn = open_database(target, read_only=True)
    assert _has_stats(conn)
    conn.close()


def test_fresh_connection_gets_stats(tmp_path):
    """Before SQLite 3.46, ``PRAGMA optimize`` only considers tables the
    connection has already queried, so on a fresh one it stamps nothing. The
    statless case therefore has to ANALYZE outright."""
    _tagged_corpus(tmp_path / "t.db", n=10).close()

    conn = open_database(tmp_path / "t.db")
    refresh_planner_stats(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_stat1 WHERE tbl = 'tag_assignments'").fetchone()[0]
    conn.close()
