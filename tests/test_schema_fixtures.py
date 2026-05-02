"""Parametrized upgrade tests: load every checked-in schema fixture, run open_database(),
verify the result satisfies the full v3 contract.
"""

import re
import sqlite3
from pathlib import Path

import pytest

from siftd.storage.sqlite import (
    SCHEMA_VERSION,
    _CASCADE_CONTRACT,
    _table_needs_cascade,
    open_database,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "schemas"
FIXTURES = sorted(FIXTURE_DIR.glob("v*.sql"))


def _declared_version(fixture_path: Path) -> int:
    """Parse the PRAGMA user_version line from a fixture file."""
    m = re.search(r"PRAGMA user_version\s*=\s*(\d+)", fixture_path.read_text())
    assert m, f"No PRAGMA user_version found in {fixture_path.name}"
    return int(m.group(1))


class TestFixtureInventory:
    """Contract on the fixture set itself (not per-fixture migrations)."""

    def test_current_version_fixture_exists(self) -> None:
        declared = {_declared_version(p) for p in FIXTURES}
        assert any(v == SCHEMA_VERSION for v in declared), (
            f"No fixture declares user_version = {SCHEMA_VERSION}; "
            f"run ./dev gen-schema-fixture after bumping SCHEMA_VERSION"
        )

    def test_no_fixture_exceeds_schema_version(self) -> None:
        for p in FIXTURES:
            v = _declared_version(p)
            assert v <= SCHEMA_VERSION, (
                f"{p.name} declares user_version = {v} which exceeds "
                f"SCHEMA_VERSION = {SCHEMA_VERSION}"
            )


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=[f.stem for f in FIXTURES])
class TestFixtureMigration:
    """Each fixture must migrate cleanly to the current SCHEMA_VERSION."""

    def test_migrates_to_current_version(self, fixture_path: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        raw = sqlite3.connect(db_path)
        raw.row_factory = sqlite3.Row
        raw.executescript(fixture_path.read_text())
        raw.commit()
        raw.close()

        conn = open_database(db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION, (
                f"{fixture_path.name}: expected user_version = {SCHEMA_VERSION}, got {version}"
            )
        finally:
            conn.close()

    def test_cascade_contract_satisfied(self, fixture_path: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(fixture_path.read_text())
        raw.commit()
        raw.close()

        conn = open_database(db_path)
        try:
            for table, fks in _CASCADE_CONTRACT.items():
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                assert not _table_needs_cascade(conn, table, fks), (
                    f"{fixture_path.name}: {table} has unsatisfied FK contract after migration"
                )
        finally:
            conn.close()

    def test_blob_triggers_exist(self, fixture_path: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(fixture_path.read_text())
        raw.commit()
        raw.close()

        conn = open_database(db_path)
        try:
            triggers = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                    " AND name IN ("
                    "'tr_tool_calls_delete_release_blob',"
                    "'tr_tool_calls_update_release_blob'"
                    ")"
                ).fetchall()
            }
            assert "tr_tool_calls_delete_release_blob" in triggers, (
                f"{fixture_path.name}: tr_tool_calls_delete_release_blob missing after migration"
            )
            assert "tr_tool_calls_update_release_blob" in triggers, (
                f"{fixture_path.name}: tr_tool_calls_update_release_blob missing after migration"
            )
        finally:
            conn.close()

    def test_no_fk_violations(self, fixture_path: Path, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(fixture_path.read_text())
        raw.commit()
        raw.close()

        conn = open_database(db_path)
        try:
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            assert not violations, (
                f"{fixture_path.name}: FK violations after migration: {list(violations[:5])}"
            )
        finally:
            conn.close()
