#!/usr/bin/env bash
# gen-schema-fixture.sh
# DESC: Dump current schema as fixture for tests/fixtures/schemas/v${SCHEMA_VERSION}.sql
# Usage: ./dev gen-schema-fixture
# Dependencies: uv, sqlite3
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
from siftd.storage.sqlite import open_database
from pathlib import Path
open_database(Path('$tmpdb'))
"

    {
        sqlite3 "$tmpdb" .schema
        echo "PRAGMA user_version = ${version};"
    } > "$out"

    log_success "Written: $out"
}

main "$@"
