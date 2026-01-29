"""CLI smoke tests — verify commands parse and run without import errors."""

import pytest

from siftd.cli import main


def test_help_exits_zero():
    """siftd --help exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_status_with_db(test_db):
    """siftd --db <path> status runs successfully."""
    rc = main(["--db", str(test_db), "status"])
    assert rc == 0


def test_query_with_db(test_db):
    """siftd --db <path> query lists conversations."""
    rc = main(["--db", str(test_db), "query"])
    assert rc == 0


def test_unknown_subcommand():
    """Unknown subcommand prints help and exits non-zero."""
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-command"])
    assert exc_info.value.code != 0


def test_tag_bulk_apply(test_db, capsys):
    """siftd tag <id> tag1 tag2 tag3 applies all tags in one call."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", conv_id, "alpha", "beta", "gamma"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Applied tag 'alpha'" in captured.out
    assert "Applied tag 'beta'" in captured.out
    assert "Applied tag 'gamma'" in captured.out

    # Verify all three tags are persisted
    conn = open_database(test_db)
    tags = conn.execute(
        """SELECT t.name FROM conversation_tags ct
           JOIN tags t ON t.id = ct.tag_id
           WHERE ct.conversation_id = ?
           ORDER BY t.name""",
        (conv_id,),
    ).fetchall()
    conn.close()
    assert [r["name"] for r in tags] == ["alpha", "beta", "gamma"]


def test_tag_bulk_remove(test_db, capsys):
    """siftd tag --remove <id> tag1 tag2 removes multiple tags."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    # Apply first
    main(["--db", str(test_db), "tag", conv_id, "alpha", "beta", "gamma"])
    # Remove two
    rc = main(["--db", str(test_db), "tag", "--remove", conv_id, "alpha", "gamma"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Removed tag 'alpha'" in captured.out
    assert "Removed tag 'gamma'" in captured.out

    # Only beta should remain
    conn = open_database(test_db)
    tags = conn.execute(
        """SELECT t.name FROM conversation_tags ct
           JOIN tags t ON t.id = ct.tag_id
           WHERE ct.conversation_id = ?""",
        (conv_id,),
    ).fetchall()
    conn.close()
    assert [r["name"] for r in tags] == ["beta"]
