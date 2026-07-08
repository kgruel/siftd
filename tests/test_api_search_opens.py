"""Opened-signal linkage at api/conversations.py::get_conversation (S3)."""

from painted import Fidelity

from siftd.api.conversations import get_conversation
from siftd.storage.search_log import SearchEventFingerprint, record_search
from siftd.storage.sqlite import open_database


def _seed_search(test_db, *, result_ids, session_id=None):
    conn = open_database(test_db, read_only=False)
    try:
        sid = record_search(
            conn, query="python", issuer="cli", fingerprint=SearchEventFingerprint(),
            executed_mode="fts", result_ids=result_ids, result_count=len(result_ids),
            session_id=session_id, commit=True,
        )
        return sid
    finally:
        conn.close()


def _opens(test_db):
    conn = open_database(test_db, read_only=True)
    try:
        return conn.execute("SELECT * FROM search_opens ORDER BY id").fetchall()
    finally:
        conn.close()


def _conv_id(test_db, external_id):
    conn = open_database(test_db, read_only=True)
    try:
        return conn.execute(
            "SELECT id FROM conversations WHERE external_id = ?", (external_id,)
        ).fetchone()["id"]
    finally:
        conn.close()


class TestWebClickLinkage:
    def test_precise_search_event_id_records_open(self, test_db):
        conv2 = _conv_id(test_db, "conv2")
        sid = _seed_search(test_db, result_ids=[conv2])

        get_conversation(conv2, fidelity=Fidelity(depth=1), db_path=test_db, search_event_id=sid)

        opens = _opens(test_db)
        assert len(opens) == 1
        assert opens[0]["search_event_id"] == sid
        assert opens[0]["conversation_id"] == conv2
        assert opens[0]["rank"] == 1
        assert opens[0]["surface"] == "web-click"


class TestCliHeuristicLinkage:
    def test_binds_to_recent_search_containing_the_id(self, test_db):
        conv2 = _conv_id(test_db, "conv2")
        sid = _seed_search(test_db, result_ids=[conv2])

        get_conversation(conv2, fidelity=Fidelity(depth=1), db_path=test_db)

        opens = _opens(test_db)
        assert len(opens) == 1
        assert opens[0]["search_event_id"] == sid
        assert opens[0]["surface"] == "cli-heuristic"

    def test_unrelated_open_records_nothing(self, test_db):
        conv1 = _conv_id(test_db, "conv1")
        conv2 = _conv_id(test_db, "conv2")
        _seed_search(test_db, result_ids=[conv2])  # conv1 not in this search's results

        get_conversation(conv1, fidelity=Fidelity(depth=1), db_path=test_db)

        assert _opens(test_db) == []

    def test_no_prior_search_records_nothing(self, test_db):
        conv1 = _conv_id(test_db, "conv1")
        get_conversation(conv1, fidelity=Fidelity(depth=1), db_path=test_db)
        assert _opens(test_db) == []
