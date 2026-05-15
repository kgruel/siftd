"""Tests for converged ID resolver (AmbiguousPrefix detection) and short_id bump."""

import sqlite3

import pytest

from siftd.api.conversations import AmbiguousPrefix, resolve_entity_id
from siftd.output._id_format import short_id
from siftd.storage.sqlite import create_database, get_or_create_harness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_collision_db(tmp_path):
    """Create a DB with two conversations sharing a 10-char ULID prefix.

    Uses ULIDs that start with '01TESTCOLL' — guaranteed to collide at 10 chars.
    Returns (db_path, id_a, id_b).
    """
    db_path = tmp_path / "collision.db"
    conn = create_database(db_path)
    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")

    id_a = "01TESTCOLLA1B2C3D4E5F6G7H8"
    id_b = "01TESTCOLLB2C3D4E5F6G7H8I9"

    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (id_a, "ext-a", harness_id, "2024-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (id_b, "ext-b", harness_id, "2024-01-02T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db_path, id_a, id_b


def _make_unique_db(tmp_path):
    """Create a DB with two conversations with no collisions at 10 chars."""
    db_path = tmp_path / "unique.db"
    conn = create_database(db_path)
    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")

    id_a = "01AAAAAAAAA1B2C3D4E5F6G7H8"
    id_b = "01BBBBBBBBB2C3D4E5F6G7H8I9"

    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (id_a, "ext-a", harness_id, "2024-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (id_b, "ext-b", harness_id, "2024-01-02T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db_path, id_a, id_b


# ---------------------------------------------------------------------------
# Unit: short_id display width
# ---------------------------------------------------------------------------

class TestShortId:
    def test_length_is_12(self):
        ulid = "01ABCDEFGHIJ1234567890ABCD"
        assert len(short_id(ulid)) == 12

    def test_returns_prefix(self):
        ulid = "01ABCDEFGHIJ1234567890ABCD"
        assert short_id(ulid) == "01ABCDEFGHIJ"


# ---------------------------------------------------------------------------
# Unit: resolve_entity_id (converged resolver)
# ---------------------------------------------------------------------------

class TestResolveEntityId:
    def test_full_id_returns_id(self, tmp_path):
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = resolve_entity_id(conn, "conversation", id_a)
        conn.close()
        assert result == id_a

    def test_unique_prefix_returns_full_id(self, tmp_path):
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # id_a starts with "01AAAAAAAAA" — unique at 11 chars
        result = resolve_entity_id(conn, "conversation", "01AAAAAAAA")
        conn.close()
        assert result == id_a

    def test_not_found_returns_none(self, tmp_path):
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = resolve_entity_id(conn, "conversation", "01ZZZZZZZZ")
        conn.close()
        assert result is None

    def test_ambiguous_prefix_raises(self, tmp_path):
        db_path, id_a, id_b = _make_collision_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        with pytest.raises(AmbiguousPrefix) as exc_info:
            resolve_entity_id(conn, "conversation", "01TESTCOLL")
        conn.close()
        exc = exc_info.value
        assert exc.prefix == "01TESTCOLL"
        assert exc.total == 2
        assert id_a in exc.matched_ids
        assert id_b in exc.matched_ids

    def test_ambiguous_prefix_payload_capped_at_5(self, tmp_path):
        """When >5 conversations collide, matched_ids is capped at 5 but total is exact."""
        db_path = tmp_path / "many.db"
        conn = create_database(db_path)
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        ids = []
        for i in range(7):
            cid = f"01SHAREDPFX{i:015d}"
            conn.execute(
                "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
                (cid, f"ext-{i}", harness_id, "2024-01-01T00:00:00Z"),
            )
            ids.append(cid)
        conn.commit()
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        with pytest.raises(AmbiguousPrefix) as exc_info:
            resolve_entity_id(conn2, "conversation", "01SHAREDPFX")
        conn2.close()
        exc = exc_info.value
        assert exc.total == 7
        assert len(exc.matched_ids) == 5

    def test_ambiguous_prefix_matched_ids_ordered(self, tmp_path):
        """matched_ids is exactly the first 5 IDs in ORDER BY c.id order.

        IDs are inserted in non-alphabetical order so the test would fail if
        the resolver returned insertion order instead of sorted order.
        """
        db_path = tmp_path / "ordered.db"
        conn = create_database(db_path)
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")

        # Suffixes chosen so lexicographic order is: AA < CC < FF < MM < PP < SS < ZZ
        # Inserted in scrambled order to make the test non-trivial.
        suffixes_insertion_order = ["ZZ", "AA", "MM", "CC", "PP", "FF", "SS"]
        all_ids = []
        for sfx in suffixes_insertion_order:
            cid = f"01ORDERPFX0{sfx * 7}AAAA"[:26]
            conn.execute(
                "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at) VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
                (cid, f"ext-{sfx}", harness_id, "2024-01-01T00:00:00Z"),
            )
            all_ids.append(cid)
        conn.commit()
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        with pytest.raises(AmbiguousPrefix) as exc_info:
            resolve_entity_id(conn2, "conversation", "01ORDERPFX0")
        conn2.close()
        exc = exc_info.value
        assert exc.total == 7
        assert exc.matched_ids == sorted(all_ids)[:5]

    def test_workspace_entity_type_no_ambiguity_check(self, tmp_path):
        """Non-conversation entity types don't raise AmbiguousPrefix."""
        db_path = tmp_path / "ws.db"
        conn = create_database(db_path)
        conn.close()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = resolve_entity_id(conn, "workspace", "nonexistent")
        conn.close()
        assert result is None

    def test_unknown_entity_type_returns_none(self, tmp_path):
        db_path = tmp_path / "u.db"
        conn = create_database(db_path)
        conn.close()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = resolve_entity_id(conn, "bogus_type", "any")
        conn.close()
        assert result is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(argv):
    """Call siftd main() and return the integer exit code.

    main() returns an int directly; only argparse errors raise SystemExit.
    """
    from siftd.cli import main
    try:
        return main(argv)
    except SystemExit as e:
        return e.code


# ---------------------------------------------------------------------------
# Argparse-layer: siftd query <id>
# ---------------------------------------------------------------------------

class TestQueryAmbiguousPrefix:
    def test_ambiguous_prefix_exits_2(self, tmp_path, capsys):
        db_path, id_a, id_b = _make_collision_db(tmp_path)
        code = _invoke(["--db", str(db_path), "query", "01TESTCOLL"])
        assert code == 2
        err = capsys.readouterr().err
        assert "01TESTCOLL" in err
        assert "2 conversations" in err
        assert "Disambiguate" in err

    def test_ambiguous_stderr_lists_matched_ids(self, tmp_path, capsys):
        db_path, id_a, id_b = _make_collision_db(tmp_path)
        _invoke(["--db", str(db_path), "query", "01TESTCOLL"])
        err = capsys.readouterr().err
        assert id_a in err
        assert id_b in err

    def test_unique_prefix_resolves_successfully(self, tmp_path, capsys):
        """Unique 10-char prefix resolves to the matching conversation (exit 0)."""
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        # First 10 chars of id_a are unique (id_b starts with "01BBB...")
        prefix = id_a[:10]
        code = _invoke(["--db", str(db_path), "query", "--summary", prefix])
        assert code == 0
        out = capsys.readouterr().out
        assert short_id(id_a) in out

    def test_full_id_resolves_successfully(self, tmp_path, capsys):
        """Full 26-char ULID always resolves unambiguously (exit 0)."""
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        code = _invoke(["--db", str(db_path), "query", "--summary", id_a])
        assert code == 0
        out = capsys.readouterr().out
        assert short_id(id_a) in out


# ---------------------------------------------------------------------------
# Argparse-layer: siftd tag <id> <tag>
# ---------------------------------------------------------------------------

class TestTagAmbiguousPrefix:
    def test_ambiguous_prefix_exits_2(self, tmp_path, capsys):
        db_path, id_a, id_b = _make_collision_db(tmp_path)
        code = _invoke(["--db", str(db_path), "tag", "01TESTCOLL", "my-tag"])
        assert code == 2
        err = capsys.readouterr().err
        assert "Disambiguate" in err

    def test_full_id_applies_tag_successfully(self, tmp_path, capsys):
        """Full ID bypasses ambiguity check and applies the tag (exit 0)."""
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        code = _invoke(["--db", str(db_path), "tag", id_a, "my-tag"])
        assert code == 0


# ---------------------------------------------------------------------------
# Argparse-layer: siftd id <prefix>
# ---------------------------------------------------------------------------

class TestIdCmdAmbiguousPrefix:
    def test_ambiguous_prefix_exits_2(self, tmp_path, capsys):
        db_path, id_a, id_b = _make_collision_db(tmp_path)
        code = _invoke(["--db", str(db_path), "id", "01TESTCOLL"])
        assert code == 2
        err = capsys.readouterr().err
        assert "Disambiguate" in err

    def test_full_id_classifies_successfully(self, tmp_path, capsys):
        """Full conversation ID is classified and shown (exit 0)."""
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        code = _invoke(["--db", str(db_path), "id", id_a])
        assert code == 0
        out = capsys.readouterr().out
        assert short_id(id_a) in out


# ---------------------------------------------------------------------------
# Argparse-layer: siftd export <id>
# ---------------------------------------------------------------------------

class TestExportAmbiguousPrefix:
    def test_ambiguous_prefix_exits_2(self, tmp_path, capsys):
        """Ambiguous prefix to 'export' exits 2 with disambiguation hint."""
        db_path, id_a, id_b = _make_collision_db(tmp_path)
        code = _invoke(["--db", str(db_path), "export", "01TESTCOLL"])
        assert code == 2
        err = capsys.readouterr().err
        assert "Disambiguate" in err
        assert "01TESTCOLL" in err

    def test_full_id_exports_successfully(self, tmp_path, capsys):
        """Full ID exports the conversation without ambiguity error (exit 0)."""
        db_path, id_a, id_b = _make_unique_db(tmp_path)
        code = _invoke(["--db", str(db_path), "export", id_a])
        assert code == 0
