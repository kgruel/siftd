"""Tests for `siftd show` — the canonical conversation reader.

`show <id>` was extracted from `query <id>` (CLI UX audit, read-surface slice 2).
The detail handlers still live in query.py; `show` is a thin front-end over the
same _dispatch_detail. `query <id>` was later removed outright (clean break, no
alias — docs/dev/cli-verb-coherence-2026-07-07.md); these cover the surviving
`show` verb plus that removal's exit-2 contract.
"""

import sqlite3

import pytest

from siftd.cli import main


def _first_conv_id(test_db) -> str:
    return sqlite3.connect(str(test_db)).execute(
        "SELECT id FROM conversations LIMIT 1"
    ).fetchone()[0]


class TestShow:
    def test_show_summary(self, test_db, capsys):
        cid = _first_conv_id(test_db)
        rc = main(["--db", str(test_db), "show", cid, "--summary"])
        assert rc == 0
        assert "Conversation" in capsys.readouterr().out

    def test_show_full_conversation(self, test_db, capsys):
        cid = _first_conv_id(test_db)
        rc = main(["--db", str(test_db), "show", cid])
        assert rc == 0
        # Some rendered output is produced (turns or metadata).
        assert capsys.readouterr().out.strip()

    def test_show_by_prefix(self, test_db, capsys):
        """Any unambiguous prefix resolves, like query <id>."""
        cid = _first_conv_id(test_db)
        rc = main(["--db", str(test_db), "show", cid[:12], "--summary"])
        assert rc == 0
        assert "Conversation" in capsys.readouterr().out

    def test_show_not_found(self, test_db, capsys):
        cid = _first_conv_id(test_db)
        absent = cid[:-6] + "ZZZZZZ"  # same length/charset, guaranteed absent
        rc = main(["--db", str(test_db), "show", absent])
        assert rc == 1
        assert "not found" in capsys.readouterr().err.lower()

    def test_show_requires_id(self, test_db):
        """`show` with no ID is a usage error (unlike query, which lists)."""
        with pytest.raises(SystemExit):
            main(["--db", str(test_db), "show"])


def test_query_id_exits_2_with_show_hint(test_db, capsys):
    """`query <id>` is gone (clean break) — it exits 2 pointing at `show`."""
    cid = _first_conv_id(test_db)
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(test_db), "query", cid, "--summary"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "siftd show" in err
