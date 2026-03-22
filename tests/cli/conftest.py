from types import SimpleNamespace

import pytest
from conftest import make_db
from siftd.storage.sqlite import open_database


@pytest.fixture
def cli_db(tmp_path):
    """Test database with extracted IDs for CLI testing."""
    db = make_db(tmp_path / "test.db", conversations=[{"external_id": "test-conv-1"}])
    conn = open_database(db, read_only=True)
    row = conn.execute(
        "SELECT id, external_id FROM conversations ORDER BY started_at LIMIT 1"
    ).fetchone()
    conn.close()
    return SimpleNamespace(
        path=db,
        conv_id=row["id"],
        external_id=row["external_id"],
        args=["--db", str(db)],
    )
