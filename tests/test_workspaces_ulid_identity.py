"""Workspaces are ULID-first in the read API.

Workspaces are first-class in storage (workspaces.id ULID PK, unique path,
git_remote, FK target for conversations.workspace_id) but the read API used to
expose them only by their slash-containing path. list_workspaces now carries the
stable ULID id + git_remote so the read API can address a workspace by id
(enabling /workspaces/{id} to mirror /conversations/{id}); path stays for display
and the existing --workspace filter UX.
"""

import sqlite3

from siftd.api.stats import list_workspaces
from siftd.serialization.serve_fmt import render_workspaces


def _seed(db) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,"
        " git_remote TEXT, discovered_at TEXT NOT NULL);"
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT,"
        " started_at TEXT, ended_at TEXT);"
        "INSERT INTO workspaces VALUES "
        "('01HWORKSPACEAAAAAAAAAAAAAA','/Users/kaygee/Code/siftd',"
        " 'git@github.com:k/siftd.git','2024-01-01T00:00:00Z');"
        "INSERT INTO conversations VALUES ('cA','01HWORKSPACEAAAAAAAAAAAAAA',"
        " '2024-01-01T00:00:00Z',NULL);"
        "INSERT INTO conversations VALUES ('cB','01HWORKSPACEAAAAAAAAAAAAAA',"
        " '2024-01-02T00:00:00Z',NULL);"
    )
    conn.commit()
    conn.close()


def test_list_workspaces_carries_ulid_and_git_remote(tmp_path):
    db = tmp_path / "ws.db"
    _seed(db)

    rows = list_workspaces(db_path=db, n=10)
    assert len(rows) == 1
    row = rows[0]
    # The stable identity is the ULID, not the slash-containing path.
    assert row["id"] == "01HWORKSPACEAAAAAAAAAAAAAA"
    assert row["path"] == "/Users/kaygee/Code/siftd"
    assert row["git_remote"] == "git@github.com:k/siftd.git"
    assert row["convs"] == 2


def test_render_workspaces_json_exposes_id_and_remote(tmp_path):
    db = tmp_path / "ws.db"
    _seed(db)

    from painted import Fidelity

    payload = render_workspaces(list_workspaces(db_path=db, n=10), Fidelity())
    ws = payload["workspaces"][0]
    assert ws["id"] == "01HWORKSPACEAAAAAAAAAAAAAA"
    assert ws["path"] == "/Users/kaygee/Code/siftd"
    assert ws["git_remote"] == "git@github.com:k/siftd.git"
    assert ws["conversations"] == 2
