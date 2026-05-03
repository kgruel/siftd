#!/usr/bin/env bash
# gen-schema-fixture.sh
# DESC: Dump current schema as fixture for tests/fixtures/schemas/v${SCHEMA_VERSION}.sql
# Usage: ./dev gen-schema-fixture
# Dependencies: uv
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

main() {
    cd "$DEV_ROOT"
    ensure_venv

    local version
    version=$(grep -m1 '^SCHEMA_VERSION = ' src/siftd/storage/sqlite.py | grep -oE '[0-9]+')
    local out="tests/fixtures/schemas/v${version}.sql"

    local tmpdb="/tmp/siftd-fixture-$$.db"
    # Expand $tmpdb eagerly (double-quoted) so the trap works after main() returns
    trap "rm -f '$tmpdb' '${tmpdb}-wal' '${tmpdb}-shm'" EXIT

    uv run python -c "
import sqlite3
from pathlib import Path
from siftd.storage.sqlite import open_database

db = Path('$tmpdb')
conn = open_database(db, read_only=False)
conn.close()

raw = sqlite3.connect(str(db))

# FTS5 virtual tables auto-create shadow tables (e.g. content_fts_config).
# Exclude them: the CREATE VIRTUAL TABLE statement recreates them on load.
FTS5_SHADOW = {'content_fts_config', 'content_fts_data', 'content_fts_idx',
               'content_fts_content', 'content_fts_docsize'}

# Tables/views/indexes first (rootpage order), triggers last (stable name order).
non_triggers = raw.execute(
    \"SELECT name, sql FROM sqlite_master\"
    \" WHERE sql IS NOT NULL AND type != 'trigger' ORDER BY rootpage\"
).fetchall()
triggers = raw.execute(
    \"SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name\"
).fetchall()

lines = []
for name, sql in non_triggers + triggers:
    if name in FTS5_SHADOW:
        continue
    lines.append(sql.strip() + ';')
lines.append('PRAGMA user_version = $version;')

raw.close()
Path('$out').write_text('\n'.join(lines) + '\n')
"

    log_success "Written: $out"
}

main "$@"
