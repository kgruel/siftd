"""Tests for workspace identity via git remote."""

import sqlite3

import pytest

from siftd.storage.migrate_workspaces import (
    backfill_git_remotes,
    count_workspaces_without_remote,
    find_duplicate_workspaces,
    merge_duplicate_workspaces,
    verify_workspace_identity,
)
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_workspace,
    insert_conversation,
)


@pytest.fixture
def db_with_workspaces(tmp_path):
    """Create database with multiple workspaces for testing."""
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness")

    # Manually insert workspaces to test migration scenarios
    # (bypass git lookup for controlled test data)
    conn.execute("""
        INSERT INTO workspaces (id, path, git_remote, discovered_at)
        VALUES ('ws1', '/path/to/project-a', NULL, '2024-01-01T00:00:00Z')
    """)
    conn.execute("""
        INSERT INTO workspaces (id, path, git_remote, discovered_at)
        VALUES ('ws2', '/different/path/project-a', NULL, '2024-01-02T00:00:00Z')
    """)
    conn.execute("""
        INSERT INTO workspaces (id, path, git_remote, discovered_at)
        VALUES ('ws3', '/path/to/project-b', 'github.com/user/project-b', '2024-01-03T00:00:00Z')
    """)
    conn.commit()

    return {"db_path": db_path, "conn": conn, "harness_id": harness_id}


class TestCountWorkspacesWithoutRemote:
    """Tests for count_workspaces_without_remote()."""

    def test_counts_workspaces(self, db_with_workspaces):
        """Correctly counts workspaces with and without git_remote."""
        conn = db_with_workspaces["conn"]
        counts = count_workspaces_without_remote(conn)

        assert counts["total"] == 3
        assert counts["without_remote"] == 2
        assert counts["with_remote"] == 1


class TestFindDuplicateWorkspaces:
    """Tests for find_duplicate_workspaces()."""

    def test_finds_no_duplicates(self, db_with_workspaces):
        """No duplicates when git_remotes are unique or NULL."""
        conn = db_with_workspaces["conn"]
        duplicates = find_duplicate_workspaces(conn)
        assert duplicates == []

    def test_finds_duplicates(self, db_with_workspaces):
        """Finds workspaces sharing the same git_remote."""
        conn = db_with_workspaces["conn"]

        # Add another workspace with same git_remote as ws3
        conn.execute("""
            INSERT INTO workspaces (id, path, git_remote, discovered_at)
            VALUES ('ws4', '/another/project-b', 'github.com/user/project-b', '2024-01-04T00:00:00Z')
        """)
        conn.commit()

        duplicates = find_duplicate_workspaces(conn)
        assert len(duplicates) == 1
        assert duplicates[0]["git_remote"] == "github.com/user/project-b"
        assert set(duplicates[0]["workspace_ids"]) == {"ws3", "ws4"}


class TestMergeDuplicateWorkspaces:
    """Tests for merge_duplicate_workspaces()."""

    def test_merges_duplicates(self, db_with_workspaces):
        """Merges duplicate workspaces and moves conversations."""
        conn = db_with_workspaces["conn"]
        harness_id = db_with_workspaces["harness_id"]

        # Add workspace with same git_remote
        conn.execute("""
            INSERT INTO workspaces (id, path, git_remote, discovered_at)
            VALUES ('ws4', '/another/project-b', 'github.com/user/project-b', '2024-01-04T00:00:00Z')
        """)
        conn.commit()

        # Add conversations to both workspaces
        # ws3 gets 2 conversations, ws4 gets 1
        insert_conversation(conn, "conv1", harness_id, "ws3", "2024-01-01T00:00:00Z")
        insert_conversation(conn, "conv2", harness_id, "ws3", "2024-01-02T00:00:00Z")
        insert_conversation(conn, "conv3", harness_id, "ws4", "2024-01-03T00:00:00Z")
        conn.commit()

        # Merge duplicates
        stats = merge_duplicate_workspaces(conn)

        assert stats["groups_found"] == 1
        assert stats["workspaces_merged"] == 1
        assert stats["conversations_moved"] == 1

        # Verify ws4 was merged into ws3 (more conversations)
        cur = conn.execute("SELECT COUNT(*) FROM workspaces WHERE id = 'ws4'")
        assert cur.fetchone()[0] == 0

        cur = conn.execute("SELECT COUNT(*) FROM conversations WHERE workspace_id = 'ws3'")
        assert cur.fetchone()[0] == 3

    def test_dry_run_does_not_modify(self, db_with_workspaces):
        """Dry run reports changes without making them."""
        conn = db_with_workspaces["conn"]
        harness_id = db_with_workspaces["harness_id"]

        conn.execute("""
            INSERT INTO workspaces (id, path, git_remote, discovered_at)
            VALUES ('ws4', '/another/project-b', 'github.com/user/project-b', '2024-01-04T00:00:00Z')
        """)
        insert_conversation(conn, "conv1", harness_id, "ws3", "2024-01-01T00:00:00Z")
        insert_conversation(conn, "conv2", harness_id, "ws4", "2024-01-02T00:00:00Z")
        conn.commit()

        stats = merge_duplicate_workspaces(conn, dry_run=True)

        assert stats["workspaces_merged"] == 1

        # Verify nothing changed
        cur = conn.execute("SELECT COUNT(*) FROM workspaces")
        assert cur.fetchone()[0] == 4


class TestVerifyWorkspaceIdentity:
    """Tests for verify_workspace_identity()."""

    def test_returns_correct_status(self, db_with_workspaces):
        """Returns complete workspace identity status."""
        conn = db_with_workspaces["conn"]
        status = verify_workspace_identity(conn)

        assert status["total"] == 3
        assert status["with_remote"] == 1
        assert status["without_remote"] == 2
        assert status["duplicate_groups"] == 0
        assert status["duplicate_workspaces"] == 0


class TestGetOrCreateWorkspaceWithGitRemote:
    """Tests for get_or_create_workspace() with git remote lookup."""

    def test_creates_workspace_without_git_remote(self, tmp_path):
        """Creates workspace when path has no git remote."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        # Create a directory without git
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        workspace_id = get_or_create_workspace(
            conn, str(project_dir), "2024-01-01T00:00:00Z"
        )

        # Verify workspace was created
        cur = conn.execute("SELECT path, git_remote FROM workspaces WHERE id = ?", (workspace_id,))
        row = cur.fetchone()
        assert row["path"] == str(project_dir)
        assert row["git_remote"] is None

    def test_creates_workspace_with_git_remote(self, tmp_path):
        """Creates workspace with git_remote when available."""
        import subprocess

        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        # Create a git repo with remote
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        subprocess.run(["git", "init", str(project_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(project_dir), "remote", "add", "origin", "git@github.com:user/project.git"],
            check=True, capture_output=True
        )

        workspace_id = get_or_create_workspace(
            conn, str(project_dir), "2024-01-01T00:00:00Z"
        )

        # Verify workspace was created with git_remote
        cur = conn.execute("SELECT path, git_remote FROM workspaces WHERE id = ?", (workspace_id,))
        row = cur.fetchone()
        assert row["path"] == str(project_dir)
        assert row["git_remote"] == "github.com/user/project"

    def test_returns_existing_workspace_by_git_remote(self, tmp_path):
        """Returns existing workspace when git_remote matches."""
        import subprocess

        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        # Create two directories for the "same" repo
        project_dir1 = tmp_path / "project1"
        project_dir1.mkdir()
        project_dir2 = tmp_path / "project2"
        project_dir2.mkdir()

        # Initialize both as git repos with same remote
        for proj in [project_dir1, project_dir2]:
            subprocess.run(["git", "init", str(proj)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(proj), "remote", "add", "origin", "git@github.com:user/same-repo.git"],
                check=True, capture_output=True
            )

        # Create workspace for first path
        ws_id1 = get_or_create_workspace(conn, str(project_dir1), "2024-01-01T00:00:00Z")

        # Second path should return same workspace
        ws_id2 = get_or_create_workspace(conn, str(project_dir2), "2024-01-02T00:00:00Z")

        assert ws_id1 == ws_id2

        # Only one workspace should exist
        cur = conn.execute("SELECT COUNT(*) FROM workspaces")
        assert cur.fetchone()[0] == 1

    def test_updates_existing_workspace_git_remote(self, tmp_path):
        """Updates git_remote for existing workspace when discovered."""
        import subprocess

        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # First call - no git repo yet
        ws_id1 = get_or_create_workspace(conn, str(project_dir), "2024-01-01T00:00:00Z")

        # Verify no git_remote
        cur = conn.execute("SELECT git_remote FROM workspaces WHERE id = ?", (ws_id1,))
        assert cur.fetchone()["git_remote"] is None

        # Now init git repo
        subprocess.run(["git", "init", str(project_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(project_dir), "remote", "add", "origin", "git@github.com:user/project.git"],
            check=True, capture_output=True
        )

        # Second call - should update git_remote
        ws_id2 = get_or_create_workspace(conn, str(project_dir), "2024-01-02T00:00:00Z")

        assert ws_id1 == ws_id2

        # Verify git_remote was updated
        cur = conn.execute("SELECT git_remote FROM workspaces WHERE id = ?", (ws_id1,))
        assert cur.fetchone()["git_remote"] == "github.com/user/project"


class TestWorkspaceFilter:
    """Tests for WhereBuilder.workspace() searching both columns."""

    def test_filters_by_path(self, tmp_path):
        """Can filter workspaces by path substring."""
        from siftd.storage.filters import WhereBuilder

        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        # Manually insert workspace
        conn.execute("""
            INSERT INTO workspaces (id, path, git_remote, discovered_at)
            VALUES ('ws1', '/path/to/myproject', 'github.com/user/myproject', '2024-01-01T00:00:00Z')
        """)
        conn.commit()

        wb = WhereBuilder()
        wb.workspace("myproject")

        sql = f"SELECT id FROM workspaces w {wb.where_sql()}"
        cur = conn.execute(sql, wb.params)
        rows = cur.fetchall()

        assert len(rows) == 1
        assert rows[0]["id"] == "ws1"

    def test_filters_by_git_remote(self, tmp_path):
        """Can filter workspaces by git_remote substring."""
        from siftd.storage.filters import WhereBuilder

        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        conn.execute("""
            INSERT INTO workspaces (id, path, git_remote, discovered_at)
            VALUES ('ws1', '/some/weird/path', 'github.com/user/target-repo', '2024-01-01T00:00:00Z')
        """)
        conn.commit()

        wb = WhereBuilder()
        wb.workspace("target-repo")

        sql = f"SELECT id FROM workspaces w {wb.where_sql()}"
        cur = conn.execute(sql, wb.params)
        rows = cur.fetchall()

        assert len(rows) == 1
        assert rows[0]["id"] == "ws1"
