"""SQLite storage adapter for siftd.

Core storage primitives: connection management, migrations, vocabulary entities,
insert operations, conversation store/lookup/delete, and file deduplication.

Tag operations: see storage/tags.py
FTS5 operations: see storage/fts.py
Backfill operations: see siftd/backfill.py
"""

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from siftd.domain import Conversation
from siftd.errors import DriftError
from siftd.ids import ulid as _ulid
from siftd.model_names import parse_model_name
from siftd.storage.attributes import set_attribute
from siftd.storage.events import (
    ensure_event_tool_call_triggers,
    ensure_polymorphic_cleanup_triggers,
    insert_event,
    insert_event_content,
    insert_event_response,
    insert_event_tool_call,
)
from siftd.storage.fts import ensure_fts_table, insert_fts_content
from siftd.storage.queries import ensure_workspace_pins_table
from siftd.storage.search_log import ensure_search_log_tables
from siftd.storage.sessions import ensure_session_tables
from siftd.storage.tags import (
    ensure_tag_pins_table,
    tag_derivative_conversation,
    tag_shell_command,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = 12

# Registry of versioned migrations: version -> migration function.
# Each function migrates the DB from version-1 to version.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

# Timeout for BEGIN IMMEDIATE during migration dispatch. Overridable via env
# for tests that need a short timeout to exercise the locked-DB path.
MIGRATION_BUSY_TIMEOUT_MS = int(os.environ.get("SIFTD_MIGRATION_BUSY_TIMEOUT_MS", "5000"))

_logger = logging.getLogger(__name__)


class SchemaUpgradeRequiredError(DriftError):
    """Raised on read-only open of a stale-schema DB that cannot be auto-upgraded.

    The fix is to make the database file (and parent directory) writable so a
    subsequent open can apply the schema migration in-place, or to migrate a
    writable copy first.
    """


def connect_read_only(
    db_path: Path,
    *,
    check_same_thread: bool = True,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """Open a read-only connection, deriving immutability instead of asserting it.

    `mode=ro&immutable=1` tells SQLite the file cannot change, so it omits all
    locking and change detection. That is not a promise this codebase can keep:
    `ingest`, a running `serve`, and any second CLI invocation write the same
    file from another process. SQLite calls the result undefined, and #38 measured it reaching
    users two ways, both silent:

      - An immutable reader ignores the `-wal` file entirely. Against a database
        with un-checkpointed commits — which is what a live `serve` leaves — a
        reader answers from the last checkpoint and says nothing.
      - When a writer checkpoints mid-read, main-file pages are rewritten under
        a reader that has no change detection. Scans then truncate early, counts
        disagree with the rows they counted, and `integrity_check` reports
        corruption in a database that is fine.

    So the plain `mode=ro` open is tried first: it takes WAL read marks and sees
    a consistent snapshot no matter who else is writing. It needs to create the
    `-shm` sidecar, which fails on genuinely read-only media — and that failure
    is the signal we want, because a medium no writer can reach is a medium
    where `immutable=1` is *true* rather than assumed. Immutability becomes a
    property discovered from the file, not a promise made about it.

    The probe is a statement, not the open: `sqlite3.connect` succeeds against
    read-only media, and the sidecar is only created when the first read
    transaction starts. Measured against a 19 MB WAL database, the probe adds
    ~10 µs per connection when the `-shm` already exists and ~100 µs when
    SQLite has to create it — the sidecar setup, not the pragma. It is not pure
    overhead: it pulls forward the read transaction the caller's own first
    query would open anyway.

    (An earlier note here claimed ~3 µs, carried over from #38 without being
    re-measured on this path. It was wrong in both regimes.)

    Only the sidecar's own failures fall back. A read-only directory raises
    SQLITE_READONLY_DIRECTORY, but a *locked* database raises SQLITE_BUSY — and
    that means a writer is active, which is exactly when `immutable=1` gives
    undefined results. Catching OperationalError wholesale would restore the
    defect precisely where it does the most damage, so anything outside the
    READONLY/CANTOPEN families propagates.

    Args:
        db_path: Path to the database file.
        check_same_thread: Passed through. False lets a connection be closed
            from a thread other than the one that opened it; it is not a
            concurrency claim, and callers still open one connection per thread.
        timeout: Passed through to `sqlite3.connect`; the default is SQLite's
            own. Lower it to reach the SQLITE_BUSY path without waiting out the
            full five seconds.
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread, timeout=timeout)
    try:
        conn.execute("PRAGMA schema_version")
    except sqlite3.OperationalError as e:
        if not (e.sqlite_errorname or "").startswith(("SQLITE_READONLY", "SQLITE_CANTOPEN")):
            raise
        conn.close()
        _refuse_immutable_over_unreplayed_sidecars(db_path, e)
        return sqlite3.connect(
            f"{uri}&immutable=1", uri=True, check_same_thread=check_same_thread, timeout=timeout
        )
    return conn


# A WAL file is a 32-byte header followed by frames. At exactly the header size
# (or below) it carries no committed pages, which is what a checkpointed-and-
# truncated `-wal` looks like.
_WAL_HEADER_BYTES = 32


def _refuse_immutable_over_unreplayed_sidecars(
    db_path: Path, cause: sqlite3.OperationalError
) -> None:
    """Reject the `immutable=1` fallback when sidecars still hold real state.

    An unwritable medium proves nothing can change the database *from now on*.
    It does not prove the main file is the whole database — and `immutable=1`
    reads only the main file. A `-wal` carrying committed frames, or a hot
    `-journal` recording a transaction that needs rolling back, is content the
    fallback would silently drop, which is exactly the stale-snapshot defect
    this helper exists to remove. Copying a database onto read-only media
    alongside its sidecars is the ordinary way to reach it.

    Neither can be replayed read-only: replay is a write. So there is no
    connection that answers correctly, and the honest outcome is an error
    naming the remedy rather than a plausible wrong answer.
    """
    for suffix, threshold, what in (
        ("-wal", _WAL_HEADER_BYTES, "committed transactions that were never checkpointed"),
        ("-journal", 0, "a transaction that needs rolling back"),
    ):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            carries_state = sidecar.stat().st_size > threshold
        except OSError:
            continue
        if carries_state:
            raise DriftError(
                f"{db_path} sits on read-only media next to a {sidecar.name} holding "
                f"{what}. Replaying it requires writing, so no read-only connection can "
                f"report the database's true contents. Copy the database and its "
                f"sidecars somewhere writable, or checkpoint it before making the "
                f"medium read-only."
            ) from cause


def _peek_user_version(db_path: Path) -> int:
    """Best-effort peek of PRAGMA user_version using a transient RO connection.

    Returns 0 on any sqlite error (garbage file, locked, etc.) — treated as
    'no info, don't auto-upgrade'. A locked database reaches that same 0 through
    connect_read_only, which propagates SQLITE_BUSY rather than degrading to an
    immutable read: `sqlite3.Error` covers it, so a busy file still means
    'no info' rather than a snapshot that predates the lock holder's commits.
    """
    try:
        peek = connect_read_only(db_path)
        try:
            return peek.execute("PRAGMA user_version").fetchone()[0]
        finally:
            peek.close()
    except sqlite3.Error:
        return 0


def _ensure_schema_for_readonly(db_path: Path) -> None:
    """Auto-upgrade a stale-schema DB so a read-only open will not crash.

    Called from open_database(read_only=True) before the RO connection is opened.
    If the DB is at a known stale version (0 < v < SCHEMA_VERSION):
      - file + parent dir writable: recurse into write-mode open_database to run
        the migration once, then return so the RO open can proceed against the
        upgraded file.
      - otherwise: raise SchemaUpgradeRequiredError with a clear message.

    Version == 0 is skipped — it means either an uninitialized/garbage file or
    a pre-versioning DB (extremely rare). Either way we let the main RO open
    fall through and surface its own error.

    Without this, RO commands (query, doctor, peek, search) crashed with a
    cryptic 'no such table: events' on a stale DB. See
    docs/dev/plans/2026-05-03-events-polymorphic-followup.md finding #1.
    """
    version = _peek_user_version(db_path)
    if version == 0 or version >= SCHEMA_VERSION:
        return
    file_writable = os.access(db_path, os.W_OK)
    dir_writable = os.access(db_path.parent, os.W_OK)
    if not (file_writable and dir_writable):
        raise SchemaUpgradeRequiredError(
            f"Database at {db_path} is at schema v{version}; this siftd install "
            f"requires v{SCHEMA_VERSION}. Cannot auto-upgrade — "
            f"{'file' if not file_writable else 'parent directory'} is not writable. "
            f"Make it writable and re-run, or migrate a writable copy."
        )
    _logger.info(
        "Auto-upgrading schema v%d → v%d for read-only open of %s",
        version, SCHEMA_VERSION, db_path,
    )
    # No checkpoint before closing: the RO open that follows reads the `-wal`,
    # so it sees the upgraded user_version wherever it landed. The explicit
    # `wal_checkpoint(TRUNCATE)` this used to run existed only because that open
    # was immutable and therefore blind to the WAL — a workaround for the
    # property #42 removed, not a requirement of the upgrade.
    open_database(db_path, read_only=False).close()


# =============================================================================
# Connection and migrations
# =============================================================================


def open_database(
    db_path: Path,
    *,
    read_only: bool = False,
    auto_upgrade: bool = True,
) -> sqlite3.Connection:
    """Open database connection, creating schema if needed.

    A connection belongs to the thread that opened it. There is deliberately no
    `check_same_thread` knob *on this function*: one connection read from
    several threads is not safe even at threadsafety==3 (the prepared-statement
    cache is shared state), and doctor — the one caller that used to ask for it
    — hit exactly that, as silently wrong query results rather than errors.
    Concurrent readers open one connection each.

    `connect_read_only`, which the read-only path below delegates to, does
    expose the flag, and that is not a reversal: there it only permits `close()`
    from a thread other than the opener, which is what doctor's per-thread
    connection pool needs at teardown. It is not permission to read one
    connection from two threads, which remains what went wrong.

    Args:
        db_path: Path to the database file.
        read_only: If True, open without running migrations/ensures that write.
            This enables read-only operations (status/query/search) against a DB that
            lives on read-only media or in restricted environments. If the DB is
            below SCHEMA_VERSION the migration is run in a transient write-mode
            open before the RO connection is established (or
            SchemaUpgradeRequiredError is raised if the file is not writable).
        auto_upgrade: When True (default) and read_only=True, a stale-schema DB
            is migrated up to SCHEMA_VERSION in a transient write-mode open.
            Set False for callers that need to *inspect* the on-disk schema
            version without mutating the file (e.g. `db schema-version`, slice
            source pre-check). Ignored when read_only=False (write-mode always
            migrates).
    """
    is_new = not db_path.exists()
    if is_new and read_only:
        raise FileNotFoundError(f"Database not found: {db_path}")

    if read_only and not is_new and auto_upgrade:
        _ensure_schema_for_readonly(db_path)

    if read_only:
        conn = connect_read_only(db_path)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        conn.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap
        conn.execute("PRAGMA temp_store = MEMORY")

    # Clear in-process vocabulary caches when opening a new connection
    # to prevent stale IDs from a previous connection
    clear_vocabulary_caches()

    try:
        if is_new:
            schema = SCHEMA_PATH.read_text()
            conn.executescript(schema)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")  # fresh DB IS at latest schema
            conn.commit()

        if not read_only:
            # Serialize concurrent migration startups: acquire write lock before
            # reading user_version or any migration metadata (R2 dispatch race).
            conn.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")

            # Peek user_version outside the write lock to determine whether a
            # pre-migration backup is needed.  backup_database() opens its own
            # source connection so it cannot run while BEGIN IMMEDIATE holds an
            # exclusive lock on the same file.
            _version_peek = conn.execute("PRAGMA user_version").fetchone()[0]
            if not is_new and _version_peek < SCHEMA_VERSION:
                from datetime import date as _date
                _bak_name = f"{db_path.stem}.bak.{_date.today():%Y%m%d}.db"
                # Surface the migration + backup so users see the side effect
                # of the auto-upgrade path; otherwise the backup file appears
                # silently next to their DB.
                _logger.info(
                    "Migrating schema v%d → v%d (%s)",
                    _version_peek, SCHEMA_VERSION, db_path,
                )
                _logger.info("Creating pre-migration backup: %s", _bak_name)
                backup_database(db_path, db_path.parent / _bak_name)

            # PRAGMA foreign_keys cannot be changed inside an active transaction
            # (SQLite silently ignores it). Migrations need FK OFF so that
            # DROP TABLE in _recreate_table_with_fks does not trigger ON DELETE
            # CASCADE on child tables. Set it before BEGIN IMMEDIATE so it takes
            # effect, then restore ON after commit.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")

            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is from a newer version of siftd "
                    f"(expected {SCHEMA_VERSION}). Please upgrade siftd."
                )

            _migrate_labels_to_tags(conn)
            _migrate_add_error_column(conn)
            _migrate_add_file_stat_columns(conn)
            _migrate_add_branch_column(conn)
            ensure_fts_table(conn)
            ensure_pricing_table(conn)
            ensure_canonical_tools(conn)
            ensure_content_blobs_table(conn)
            ensure_session_tables(conn)
            ensure_tag_pins_table(conn)
            ensure_workspace_pins_table(conn)
            _ensure_git_remote_index(conn)
            _ensure_ingested_files_conversation_index(conn)
            _ensure_usage_by_conv_model_table(conn)
            _ensure_conversation_stats_table(conn)
            ensure_conversation_owners_table(conn)
            ensure_sync_inbox_table(conn)
            ensure_search_log_tables(conn)

            # Versioned migration dispatch: run each un-applied version in order.
            # MIGRATIONS[v] runs only when the DB is below version v.
            for _v in range(version + 1, SCHEMA_VERSION + 1):
                if _v in MIGRATIONS:
                    MIGRATIONS[_v](conn)

            # Replace tr_tool_calls_* triggers with tr_event_tool_call_* on every
            # write open so old DBs upgraded in-place get the new trigger names.
            ensure_event_tool_call_triggers(conn)
            # Ensure polymorphic cascade-cleanup triggers exist (idempotent).
            ensure_polymorphic_cleanup_triggers(conn)

            # Stamp schema version after successful migrations
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
        else:
            # read_only: on future-version DB, warn and open (write-mode raises)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                _logger.warning(
                    "DB stamped v%d; current install supports only v%d, opening read-only. "
                    "Upgrade siftd.",
                    version,
                    SCHEMA_VERSION,
                )
    except Exception:
        if not read_only:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA foreign_keys = ON")
            except Exception:
                pass
        conn.close()
        raise

    return conn


def remove_database(db_path: Path) -> None:
    """Delete a database file and its SQLite sidecars.

    A SQLite database is three files, not one, so unlinking just the ``.db``
    orphans any ``-wal``/``-shm`` beside it. That matters most for the
    ephemeral payloads sync and receive stage into temp directories: their
    cleanup runs while the sidecars exist, and whatever it misses outlives the
    payload.

    Only for databases the caller *owns and is destroying*. Removing sidecars
    from a database that stays in use is never safe — a live ``-shm`` carries
    the locking state shared between processes, and replacing it out from under
    an open connection costs coherence, not just tidiness.
    """
    for artifact in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if artifact.exists():
            artifact.unlink()


def create_empty_database(db_path: Path) -> None:
    """Create a new database with the base schema only (no migrations).

    Used for slice targets that need a clean schema without
    the session/prompt_tags/etc. ensure tables.
    """
    # Slice/export paths may be reused within a single workflow; remove any
    # existing DB and SQLite sidecars so schema creation always starts clean.
    remove_database(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def backup_database(source_path: Path, target_path: Path) -> None:
    """Create a consistent online backup using sqlite3.Connection.backup().

    Args:
        source_path: Path to the source database.
        target_path: Path to write the backup. Parent directory is created if needed.

    Raises:
        FileNotFoundError: If source database does not exist.
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Database not found: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source_path.as_posix()}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True)
    try:
        dest_conn = sqlite3.connect(str(target_path))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def _migrate_labels_to_tags(conn: sqlite3.Connection) -> None:
    """Migrate old label tables to tag tables if they exist.

    Renames: labels -> tags, conversation_labels -> conversation_tags,
    workspace_labels -> workspace_tags, and updates column names.
    """
    # Check if old 'labels' table exists
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='labels'"
    )
    if not cur.fetchone():
        return  # No migration needed

    # Check if new 'tags' table already exists (migration already done)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
    )
    if cur.fetchone():
        return  # Already migrated

    # Perform migration
    conn.execute("ALTER TABLE labels RENAME TO tags")
    conn.execute("ALTER TABLE conversation_labels RENAME TO conversation_tags")
    conn.execute("ALTER TABLE workspace_labels RENAME TO workspace_tags")

    # Rename label_id columns to tag_id
    # SQLite requires recreating tables to rename columns in older versions,
    # but ALTER TABLE ... RENAME COLUMN works in SQLite 3.25.0+ (2018-09-15)
    conn.execute("ALTER TABLE conversation_tags RENAME COLUMN label_id TO tag_id")
    conn.execute("ALTER TABLE workspace_tags RENAME COLUMN label_id TO tag_id")


def _migrate_add_error_column(conn: sqlite3.Connection) -> None:
    """Add error column to ingested_files if it doesn't exist."""
    cur = conn.execute("PRAGMA table_info(ingested_files)")
    columns = {row[1] for row in cur.fetchall()}
    if "error" not in columns:
        try:
            conn.execute("ALTER TABLE ingested_files ADD COLUMN error TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            # Race: another process added the column; verify it's actually there
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ingested_files)").fetchall()}
            if "error" not in columns:
                raise


def _migrate_add_file_stat_columns(conn: sqlite3.Connection) -> None:
    """Add file_mtime and file_size columns to ingested_files if they don't exist."""
    cur = conn.execute("PRAGMA table_info(ingested_files)")
    columns = {row[1] for row in cur.fetchall()}
    if "file_mtime" not in columns:
        for col_ddl, col_name in [
            ("ALTER TABLE ingested_files ADD COLUMN file_mtime REAL", "file_mtime"),
            ("ALTER TABLE ingested_files ADD COLUMN file_size INTEGER", "file_size"),
        ]:
            try:
                conn.execute(col_ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
                cols = {r[1] for r in conn.execute("PRAGMA table_info(ingested_files)").fetchall()}
                if col_name not in cols:
                    raise


def _migrate_add_branch_column(conn: sqlite3.Connection) -> None:
    """Add branch column to conversations if it doesn't exist."""
    cur = conn.execute("PRAGMA table_info(conversations)")
    columns = {row[1] for row in cur.fetchall()}
    if "branch" not in columns:
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN branch TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
            if "branch" not in cols:
                raise


def _migrate_add_cascade_deletes(conn: sqlite3.Connection) -> None:
    """Add ON DELETE CASCADE to foreign key constraints.

    SQLite doesn't support ALTER TABLE to modify FK constraints, so we must
    recreate each table. We check if migration is needed by inspecting the
    table DDL in sqlite_master.
    """
    # Check if migration is needed by looking at prompts table DDL
    cur = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='prompts'"
    )
    row = cur.fetchone()
    if not row:
        return  # Table doesn't exist yet
    if "ON DELETE CASCADE" in row[0]:
        return  # Already migrated

    # Disable FK enforcement during migration (required for table recreation)
    conn.execute("PRAGMA foreign_keys = OFF")

    tool_call_columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    ingested_file_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ingested_files)")
    }
    tool_calls_result_hash_ddl = ""
    tool_calls_result_hash_columns = ""
    if "result_hash" in tool_call_columns:
        tool_calls_result_hash_ddl = (
            "\n                result_hash     TEXT REFERENCES content_blobs(hash),"
        )
        tool_calls_result_hash_columns = ", result_hash"

    ingested_files_file_stat_ddl = ""
    ingested_files_file_stat_columns = ""
    if "file_mtime" in ingested_file_columns:
        ingested_files_file_stat_ddl = (
            ",\n                file_mtime      REAL,"
            "\n                file_size       INTEGER"
        )
        ingested_files_file_stat_columns = ", file_mtime, file_size"

    # Tables that need migration, in order that respects dependencies
    # (parent tables first for drops, child tables first for creates)
    tables_to_migrate = [
        # (table_name, new_ddl, columns_to_copy)
        ("prompts", """
            CREATE TABLE prompts_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                external_id     TEXT,
                timestamp       TEXT NOT NULL,
                UNIQUE (conversation_id, external_id)
            )
        """, "id, conversation_id, external_id, timestamp"),
        ("responses", """
            CREATE TABLE responses_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                prompt_id       TEXT REFERENCES prompts(id) ON DELETE CASCADE,
                model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
                provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
                external_id     TEXT,
                timestamp       TEXT NOT NULL,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                UNIQUE (conversation_id, external_id)
            )
        """, "id, conversation_id, prompt_id, model_id, provider_id, external_id, timestamp, input_tokens, output_tokens"),
        ("tool_calls", f"""
            CREATE TABLE tool_calls_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
                external_id     TEXT,
                input           TEXT,
                result          TEXT,{tool_calls_result_hash_ddl}
                status          TEXT,
                timestamp       TEXT
            )
        """, f"id, response_id, conversation_id, tool_id, external_id, input, result{tool_calls_result_hash_columns}, status, timestamp"),
        ("prompt_content", """
            CREATE TABLE prompt_content_new (
                id              TEXT PRIMARY KEY,
                prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                block_index     INTEGER NOT NULL,
                block_type      TEXT NOT NULL,
                content         TEXT NOT NULL,
                UNIQUE (prompt_id, block_index)
            )
        """, "id, prompt_id, block_index, block_type, content"),
        ("response_content", """
            CREATE TABLE response_content_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                block_index     INTEGER NOT NULL,
                block_type      TEXT NOT NULL,
                content         TEXT NOT NULL,
                UNIQUE (response_id, block_index)
            )
        """, "id, response_id, block_index, block_type, content"),
        ("conversation_attributes", """
            CREATE TABLE conversation_attributes_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (conversation_id, key, scope)
            )
        """, "id, conversation_id, key, value, scope"),
        ("prompt_attributes", """
            CREATE TABLE prompt_attributes_new (
                id              TEXT PRIMARY KEY,
                prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (prompt_id, key, scope)
            )
        """, "id, prompt_id, key, value, scope"),
        ("response_attributes", """
            CREATE TABLE response_attributes_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (response_id, key, scope)
            )
        """, "id, response_id, key, value, scope"),
        ("tool_call_attributes", """
            CREATE TABLE tool_call_attributes_new (
                id              TEXT PRIMARY KEY,
                tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (tool_call_id, key, scope)
            )
        """, "id, tool_call_id, key, value, scope"),
        ("conversation_tags", """
            CREATE TABLE conversation_tags_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (conversation_id, tag_id)
            )
        """, "id, conversation_id, tag_id, applied_at"),
        ("tool_call_tags", """
            CREATE TABLE tool_call_tags_new (
                id              TEXT PRIMARY KEY,
                tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (tool_call_id, tag_id)
            )
        """, "id, tool_call_id, tag_id, applied_at"),
        ("ingested_files", f"""
            CREATE TABLE ingested_files_new (
                id              TEXT PRIMARY KEY,
                path            TEXT NOT NULL UNIQUE,
                file_hash       TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                ingested_at     TEXT NOT NULL,
                error           TEXT{ingested_files_file_stat_ddl}
            )
        """, f"id, path, file_hash, harness_id, conversation_id, ingested_at, error{ingested_files_file_stat_columns}"),
    ]

    for table_name, new_ddl, columns in tables_to_migrate:
        # Check if table exists
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        if not cur.fetchone():
            continue  # Table doesn't exist, skip

        # Create new table
        conn.execute(new_ddl)
        # Copy data
        conn.execute(f"INSERT INTO {table_name}_new ({columns}) SELECT {columns} FROM {table_name}")
        # Drop old table
        conn.execute(f"DROP TABLE {table_name}")
        # Rename new table
        conn.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")

    # Recreate indexes that were dropped with the tables
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_conversation ON prompts(conversation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_timestamp ON prompts(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_conversation ON responses(conversation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_prompt ON responses(prompt_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_model ON responses(model_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_timestamp ON responses(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_response ON tool_calls(response_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation ON tool_calls(conversation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_status ON tool_calls(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompt_content_prompt ON prompt_content(prompt_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_response_content_response ON response_content(response_id)")

    conn.commit()
    # Re-enable FK enforcement
    conn.execute("PRAGMA foreign_keys = ON")


# =============================================================================
# Version-2 migration: cascade delete contract enforcement
# =============================================================================

# FK contract: (from_col, to_table, on_delete) triples that each table must satisfy.
_CASCADE_CONTRACT: dict[str, list[tuple[str, str, str]]] = {
    "tool_aliases": [
        ("harness_id", "harnesses", "CASCADE"),
        ("tool_id", "tools", "CASCADE"),
    ],
    "pricing": [
        ("model_id", "models", "CASCADE"),
        ("provider_id", "providers", "CASCADE"),
    ],
    "conversations": [
        ("harness_id", "harnesses", "CASCADE"),
        ("workspace_id", "workspaces", "SET NULL"),
    ],
    "prompts": [("conversation_id", "conversations", "CASCADE")],
    "responses": [
        ("conversation_id", "conversations", "CASCADE"),
        ("prompt_id", "prompts", "CASCADE"),
        ("model_id", "models", "SET NULL"),
        ("provider_id", "providers", "SET NULL"),
    ],
    "tool_calls": [
        ("response_id", "responses", "CASCADE"),
        ("conversation_id", "conversations", "CASCADE"),
        ("tool_id", "tools", "SET NULL"),
    ],
    "prompt_content": [("prompt_id", "prompts", "CASCADE")],
    "response_content": [("response_id", "responses", "CASCADE")],
    "conversation_attributes": [("conversation_id", "conversations", "CASCADE")],
    "prompt_attributes": [("prompt_id", "prompts", "CASCADE")],
    "response_attributes": [("response_id", "responses", "CASCADE")],
    "tool_call_attributes": [("tool_call_id", "tool_calls", "CASCADE")],
    "workspace_tags": [
        ("workspace_id", "workspaces", "CASCADE"),
        ("tag_id", "tags", "CASCADE"),
    ],
    "conversation_tags": [
        ("conversation_id", "conversations", "CASCADE"),
        ("tag_id", "tags", "CASCADE"),
    ],
    "tool_call_tags": [
        ("tool_call_id", "tool_calls", "CASCADE"),
        ("tag_id", "tags", "CASCADE"),
    ],
    "ingested_files": [
        ("harness_id", "harnesses", "CASCADE"),
        ("conversation_id", "conversations", "CASCADE"),
    ],
    "prompt_tags": [
        ("prompt_id", "prompts", "CASCADE"),
        ("tag_id", "tags", "CASCADE"),
    ],
    "conversation_owners": [("conversation_id", "conversations", "CASCADE")],
    "conversation_stats": [("conversation_id", "conversations", "CASCADE")],
    "tool_search": [
        ("tool_call_id", "tool_calls", "CASCADE"),
        ("conversation_id", "conversations", "CASCADE"),
        ("response_id", "responses", "CASCADE"),
    ],
}


def _table_needs_cascade(
    conn: sqlite3.Connection,
    table: str,
    required_fks: list[tuple[str, str, str]],
) -> bool:
    """Return True if any required FK is absent or has the wrong on_delete action."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
    actual: dict[tuple[str, str], str] = {(r[3], r[2]): r[6] for r in rows}
    return any(actual.get((col, tbl)) != action for col, tbl, action in required_fks)


def _recreate_table_with_fks(
    conn: sqlite3.Connection,
    table_name: str,
    new_ddl: str,
    columns: str,
    indexes: list[str],
) -> None:
    """Replace a table's DDL to update FK constraints, preserving all rows.

    Caller must hold PRAGMA foreign_keys = OFF and an active SAVEPOINT.
    Exposed as a named function so tests can monkeypatch it to exercise the
    ROLLBACK TO path without touching real data.
    """
    conn.execute(new_ddl)
    conn.execute(f"INSERT INTO {table_name}_new ({columns}) SELECT {columns} FROM {table_name}")
    conn.execute(f"DROP TABLE {table_name}")
    conn.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")
    for idx_sql in indexes:
        conn.execute(idx_sql)


def _migrate_cascade_v2(conn: sqlite3.Connection) -> None:
    """Version-2 migration: enforce ON DELETE CASCADE/SET NULL on all FK tables.

    Replaces the legacy prompts-only early-exit with per-table FK validation so
    partial-cascade states are always repaired. Uses SAVEPOINT for transactional
    rollback and try/finally to restore FK enforcement even on failure.
    """
    # Detect optional columns added by the legacy chain before this migration runs.
    tc_cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)").fetchall()}
    inf_cols = {r[1] for r in conn.execute("PRAGMA table_info(ingested_files)").fetchall()}
    conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}

    result_hash_ddl = (
        "\n                result_hash     TEXT REFERENCES content_blobs(hash),"
        if "result_hash" in tc_cols else ""
    )
    result_hash_col = ", result_hash" if "result_hash" in tc_cols else ""

    file_stat_ddl = (
        ",\n                file_mtime      REAL,\n                file_size       INTEGER"
        if "file_mtime" in inf_cols else ""
    )
    file_stat_col = ", file_mtime, file_size" if "file_mtime" in inf_cols else ""

    branch_ddl = "\n                branch          TEXT," if "branch" in conv_cols else ""
    branch_col = ", branch" if "branch" in conv_cols else ""

    # (table_name, new_ddl, columns_csv, indexes)
    _TABLE_SPECS: list[tuple[str, str, str, list[str]]] = [
        (
            "tool_aliases",
            """
            CREATE TABLE tool_aliases_new (
                id              TEXT PRIMARY KEY,
                raw_name        TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                tool_id         TEXT NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
                UNIQUE (raw_name, harness_id)
            )
            """,
            "id, raw_name, harness_id, tool_id",
            [
                "CREATE INDEX IF NOT EXISTS idx_tool_aliases_tool ON tool_aliases(tool_id)",
                "CREATE INDEX IF NOT EXISTS idx_tool_aliases_harness ON tool_aliases(harness_id)",
            ],
        ),
        (
            "pricing",
            """
            CREATE TABLE pricing_new (
                id              TEXT PRIMARY KEY,
                model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
                provider_id     TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
                input_per_mtok  REAL,
                output_per_mtok REAL,
                UNIQUE (model_id, provider_id)
            )
            """,
            "id, model_id, provider_id, input_per_mtok, output_per_mtok",
            [],
        ),
        (
            "conversations",
            f"""
            CREATE TABLE conversations_new (
                id              TEXT PRIMARY KEY,
                external_id     TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                workspace_id    TEXT REFERENCES workspaces(id) ON DELETE SET NULL,{branch_ddl}
                started_at      TEXT NOT NULL,
                ended_at        TEXT,
                UNIQUE (harness_id, external_id)
            )
            """,
            f"id, external_id, harness_id, workspace_id{branch_col}, started_at, ended_at",
            [
                "CREATE INDEX IF NOT EXISTS idx_conversations_harness ON conversations(harness_id)",
                "CREATE INDEX IF NOT EXISTS idx_conversations_workspace ON conversations(workspace_id)",
                "CREATE INDEX IF NOT EXISTS idx_conversations_started ON conversations(started_at)",
                "CREATE INDEX IF NOT EXISTS idx_conversations_ended ON conversations(ended_at)",
            ],
        ),
        (
            "prompts",
            """
            CREATE TABLE prompts_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                external_id     TEXT,
                timestamp       TEXT NOT NULL,
                UNIQUE (conversation_id, external_id)
            )
            """,
            "id, conversation_id, external_id, timestamp",
            [
                "CREATE INDEX IF NOT EXISTS idx_prompts_conversation ON prompts(conversation_id)",
                "CREATE INDEX IF NOT EXISTS idx_prompts_timestamp ON prompts(timestamp)",
            ],
        ),
        (
            "responses",
            """
            CREATE TABLE responses_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                prompt_id       TEXT REFERENCES prompts(id) ON DELETE CASCADE,
                model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
                provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
                external_id     TEXT,
                timestamp       TEXT NOT NULL,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                UNIQUE (conversation_id, external_id)
            )
            """,
            "id, conversation_id, prompt_id, model_id, provider_id, external_id, timestamp, input_tokens, output_tokens",
            [
                "CREATE INDEX IF NOT EXISTS idx_responses_conversation ON responses(conversation_id)",
                "CREATE INDEX IF NOT EXISTS idx_responses_prompt ON responses(prompt_id)",
                "CREATE INDEX IF NOT EXISTS idx_responses_model ON responses(model_id)",
                "CREATE INDEX IF NOT EXISTS idx_responses_timestamp ON responses(timestamp)",
            ],
        ),
        (
            "tool_calls",
            f"""
            CREATE TABLE tool_calls_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
                external_id     TEXT,
                input           TEXT,
                result          TEXT,{result_hash_ddl}
                status          TEXT,
                timestamp       TEXT
            )
            """,
            f"id, response_id, conversation_id, tool_id, external_id, input, result{result_hash_col}, status, timestamp",
            [
                "CREATE INDEX IF NOT EXISTS idx_tool_calls_response ON tool_calls(response_id)",
                "CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation ON tool_calls(conversation_id)",
                "CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_id)",
                "CREATE INDEX IF NOT EXISTS idx_tool_calls_status ON tool_calls(status)",
            ],
        ),
        (
            "prompt_content",
            """
            CREATE TABLE prompt_content_new (
                id              TEXT PRIMARY KEY,
                prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                block_index     INTEGER NOT NULL,
                block_type      TEXT NOT NULL,
                content         TEXT NOT NULL,
                UNIQUE (prompt_id, block_index)
            )
            """,
            "id, prompt_id, block_index, block_type, content",
            ["CREATE INDEX IF NOT EXISTS idx_prompt_content_prompt ON prompt_content(prompt_id)"],
        ),
        (
            "response_content",
            """
            CREATE TABLE response_content_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                block_index     INTEGER NOT NULL,
                block_type      TEXT NOT NULL,
                content         TEXT NOT NULL,
                UNIQUE (response_id, block_index)
            )
            """,
            "id, response_id, block_index, block_type, content",
            ["CREATE INDEX IF NOT EXISTS idx_response_content_response ON response_content(response_id)"],
        ),
        (
            "conversation_attributes",
            """
            CREATE TABLE conversation_attributes_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (conversation_id, key, scope)
            )
            """,
            "id, conversation_id, key, value, scope",
            [],
        ),
        (
            "prompt_attributes",
            """
            CREATE TABLE prompt_attributes_new (
                id              TEXT PRIMARY KEY,
                prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (prompt_id, key, scope)
            )
            """,
            "id, prompt_id, key, value, scope",
            [],
        ),
        (
            "response_attributes",
            """
            CREATE TABLE response_attributes_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (response_id, key, scope)
            )
            """,
            "id, response_id, key, value, scope",
            [
                "CREATE INDEX IF NOT EXISTS idx_response_attributes_key"
                " ON response_attributes(key, response_id, value)"
            ],
        ),
        (
            "tool_call_attributes",
            """
            CREATE TABLE tool_call_attributes_new (
                id              TEXT PRIMARY KEY,
                tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (tool_call_id, key, scope)
            )
            """,
            "id, tool_call_id, key, value, scope",
            [],
        ),
        (
            "workspace_tags",
            """
            CREATE TABLE workspace_tags_new (
                id              TEXT PRIMARY KEY,
                workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (workspace_id, tag_id)
            )
            """,
            "id, workspace_id, tag_id, applied_at",
            ["CREATE INDEX IF NOT EXISTS idx_workspace_tags_tag ON workspace_tags(tag_id)"],
        ),
        (
            "conversation_tags",
            """
            CREATE TABLE conversation_tags_new (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (conversation_id, tag_id)
            )
            """,
            "id, conversation_id, tag_id, applied_at",
            ["CREATE INDEX IF NOT EXISTS idx_conversation_tags_tag ON conversation_tags(tag_id)"],
        ),
        (
            "tool_call_tags",
            """
            CREATE TABLE tool_call_tags_new (
                id              TEXT PRIMARY KEY,
                tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (tool_call_id, tag_id)
            )
            """,
            "id, tool_call_id, tag_id, applied_at",
            ["CREATE INDEX IF NOT EXISTS idx_tool_call_tags_tag ON tool_call_tags(tag_id)"],
        ),
        (
            "ingested_files",
            f"""
            CREATE TABLE ingested_files_new (
                id              TEXT PRIMARY KEY,
                path            TEXT NOT NULL UNIQUE,
                file_hash       TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                ingested_at     TEXT NOT NULL,
                error           TEXT{file_stat_ddl}
            )
            """,
            f"id, path, file_hash, harness_id, conversation_id, ingested_at, error{file_stat_col}",
            [],
        ),
        (
            "prompt_tags",
            """
            CREATE TABLE prompt_tags_new (
                id TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at TEXT NOT NULL,
                UNIQUE (prompt_id, tag_id)
            )
            """,
            "id, prompt_id, tag_id, applied_at",
            [],
        ),
        (
            "conversation_owners",
            """
            CREATE TABLE conversation_owners_new (
                conversation_id TEXT NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                user_id         TEXT NOT NULL,
                push_id         TEXT,
                assigned_at     TEXT NOT NULL,
                PRIMARY KEY (conversation_id)
            )
            """,
            "conversation_id, user_id, push_id, assigned_at",
            [
                "CREATE INDEX IF NOT EXISTS idx_conversation_owners_user"
                " ON conversation_owners(user_id)"
            ],
        ),
        (
            "conversation_stats",
            """
            CREATE TABLE conversation_stats_new (
                conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                prompt_count    INTEGER NOT NULL DEFAULT 0,
                response_count  INTEGER NOT NULL DEFAULT 0,
                total_tokens    INTEGER NOT NULL DEFAULT 0,
                model_name      TEXT,
                cost            REAL
            )
            """,
            "conversation_id, prompt_count, response_count, total_tokens, model_name, cost",
            [],
        ),
        (
            "tool_search",
            """
            CREATE TABLE tool_search_new (
                tool_call_id TEXT PRIMARY KEY REFERENCES tool_calls(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                response_id TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                timestamp TEXT,
                tool_name TEXT,
                tool_family TEXT,
                tool_description TEXT,
                status TEXT,
                path TEXT,
                basename TEXT,
                ext TEXT,
                command TEXT,
                command_verb TEXT,
                pattern TEXT,
                arg TEXT,
                result_snippet TEXT,
                workspace_path TEXT,
                search_text TEXT NOT NULL
            )
            """,
            (
                "tool_call_id, conversation_id, response_id, timestamp, tool_name, tool_family,"
                " tool_description, status, path, basename, ext, command, command_verb, pattern,"
                " arg, result_snippet, workspace_path, search_text"
            ),
            [
                "CREATE INDEX IF NOT EXISTS idx_tool_search_conversation ON tool_search(conversation_id)"
            ],
        ),
    ]

    # Identify tables that exist and need their FK spec repaired.
    tables_to_fix = []
    for table_name, new_ddl, columns, indexes in _TABLE_SPECS:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        if not exists:
            continue
        contract = _CASCADE_CONTRACT.get(table_name, [])
        if contract and _table_needs_cascade(conn, table_name, contract):
            tables_to_fix.append((table_name, new_ddl, columns, indexes))

    if not tables_to_fix:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("SAVEPOINT cascade_delete_migration")
        try:
            for table_name, new_ddl, columns, indexes in tables_to_fix:
                _recreate_table_with_fks(conn, table_name, new_ddl, columns, indexes)
            conn.execute("RELEASE cascade_delete_migration")
        except Exception:
            conn.execute("ROLLBACK TO cascade_delete_migration")
            conn.execute("RELEASE cascade_delete_migration")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"FK violations after cascade migration: {list(violations[:5])}"
        )


MIGRATIONS[2] = _migrate_cascade_v2


def _migrate_blob_integrity_v3(conn: sqlite3.Connection) -> None:
    """Version-3 migration: add ref_count NOT NULL CHECK(ref_count >= 0) to content_blobs.

    Garbage-collects rows with ref_count <= 0 before recreating the table, since
    SQLite requires table recreation to add CHECK constraints and existing violating
    rows would prevent insertion under the new schema.

    Also fixes the delete trigger from `ref_count = 0` to `ref_count <= 0` for
    existing databases that have the old trigger from before this migration.
    """
    # Add result_hash column to tool_calls if it exists and lacks the column.
    # Moved here from ensure_content_blobs_table (slice 8 cleanup).
    cur = conn.execute("PRAGMA table_info(tool_calls)")
    tc_cols = {row[1] for row in cur.fetchall()}
    if tc_cols and "result_hash" not in tc_cols:
        conn.execute(
            "ALTER TABLE tool_calls ADD COLUMN result_hash TEXT REFERENCES content_blobs(hash)"
        )

    # Check whether content_blobs exists and already has the CHECK constraint
    # (fresh DBs created from the updated schema.sql have it already).
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='content_blobs'"
    ).fetchone()
    if ddl_row is None:
        return

    needs_recreate = "CHECK" not in (ddl_row[0] or "")

    if needs_recreate:
        # PRAGMA foreign_keys must be set before any DML to avoid Python's
        # sqlite3 implicit-BEGIN making it a no-op.  All cleanup (UPDATE/DELETE)
        # and table recreation happen inside one SAVEPOINT with FK off.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("SAVEPOINT blob_integrity_v3")
            try:
                # Drop blob ref_count triggers up-front: modern SQLite parses
                # all trigger bodies during ALTER TABLE RENAME to rewrite name
                # references, and stale references to the dropped content_blobs
                # would abort the rename. They are recreated below in their
                # current clamped form.
                conn.execute("DROP TRIGGER IF EXISTS tr_tool_calls_delete_release_blob")
                conn.execute("DROP TRIGGER IF EXISTS tr_tool_calls_update_release_blob")
                # Null out result_hash refs to blobs we're about to delete,
                # then remove the garbage rows before adding the CHECK constraint.
                conn.execute("""
                    UPDATE tool_calls SET result_hash = NULL
                    WHERE result_hash IN (SELECT hash FROM content_blobs WHERE ref_count <= 0)
                """)
                conn.execute("DELETE FROM content_blobs WHERE ref_count <= 0")
                conn.execute("""
                    CREATE TABLE content_blobs_new (
                        hash TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        ref_count INTEGER NOT NULL DEFAULT 1 CHECK (ref_count >= 0),
                        created_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO content_blobs_new (hash, content, ref_count, created_at)
                    SELECT hash, content, ref_count, created_at FROM content_blobs
                """)
                conn.execute("DROP TABLE content_blobs")
                conn.execute("ALTER TABLE content_blobs_new RENAME TO content_blobs")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_content_blobs_ref_count ON content_blobs(ref_count)"
                )
                conn.execute("RELEASE SAVEPOINT blob_integrity_v3")
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT blob_integrity_v3")
                conn.execute("RELEASE SAVEPOINT blob_integrity_v3")
                raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"FK violations after blob integrity migration: {list(violations[:5])}"
            )

    # Drop+recreate blob refcount triggers on tool_calls when the table exists.
    # This covers three failure modes:
    # - cascade migration (v2) drops triggers via DROP TABLE tool_calls, so they
    #   may not exist at all when this migration runs
    # - pre-v3 delete trigger using `ref_count = 0` instead of `ref_count <= 0`
    # - pre-fix update trigger using unclamped `ref_count - 1` instead of MAX(...)
    # On v6+ DBs where tool_calls has been dropped, this block is skipped entirely.
    _tc_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchone()
    if _tc_exists:
        conn.execute("DROP TRIGGER IF EXISTS tr_tool_calls_delete_release_blob")
        conn.execute("DROP TRIGGER IF EXISTS tr_tool_calls_update_release_blob")
        conn.execute("""
            CREATE TRIGGER tr_tool_calls_delete_release_blob
            AFTER DELETE ON tool_calls
            FOR EACH ROW
            WHEN OLD.result_hash IS NOT NULL
            BEGIN
                UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0) WHERE hash = OLD.result_hash;
                DELETE FROM content_blobs WHERE hash = OLD.result_hash AND ref_count <= 0;
            END
        """)
        conn.execute("""
            CREATE TRIGGER tr_tool_calls_update_release_blob
            AFTER UPDATE OF result_hash ON tool_calls
            FOR EACH ROW
            WHEN OLD.result_hash IS NOT NEW.result_hash
            BEGIN
                UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0)
                    WHERE OLD.result_hash IS NOT NULL AND hash = OLD.result_hash;
                DELETE FROM content_blobs
                    WHERE OLD.result_hash IS NOT NULL AND hash = OLD.result_hash AND ref_count <= 0;
                UPDATE content_blobs SET ref_count = ref_count + 1
                    WHERE NEW.result_hash IS NOT NULL AND hash = NEW.result_hash;
            END
        """)


MIGRATIONS[3] = _migrate_blob_integrity_v3


# =============================================================================
# Version-4 migration: polymorphic events schema
# =============================================================================


class MigrationAssertionError(RuntimeError):
    """Raised when a post-backfill row-count assertion fails.

    Propagates out of the migration function, through the runner, into
    open_database()'s outer except block, which issues ROLLBACK.
    """


def _migrate_v4_polymorphic_schema(conn: sqlite3.Connection) -> None:
    """Version-4 migration: create polymorphic events tables and backfill from old forks.

    Creates events, event_response, event_tool_call, event_content, attributes,
    and tag_assignments tables, then backfills data from the existing
    prompts/responses/tool_calls/prompt_content/response_content/*_attributes/*_tags tables.

    Old tables are NOT dropped here (slice 8 cleanup). Storage writers continue
    writing to old tables until slices 2-5 move them.

    Caller (open_database runner) owns the transaction — this function must not
    call conn.commit(), conn.rollback(), or conn.execute("BEGIN").
    """
    # 1. Create new tables (IF NOT EXISTS — fresh DBs created from the updated
    #    schema.sql already have them; existing DBs don't yet).
    #    Use individual execute() calls — executescript() issues an implicit COMMIT
    #    which would break the runner's outer BEGIN IMMEDIATE transaction.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            parent_id       TEXT REFERENCES events(id) ON DELETE CASCADE,
            external_id     TEXT,
            timestamp       TEXT NOT NULL,
            UNIQUE (conversation_id, kind, external_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_conversation_kind ON events(conversation_id, kind)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_response (
            event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
            provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
            input_tokens    INTEGER,
            output_tokens   INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_tool_call (
            event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
            input           TEXT,
            result_hash     TEXT REFERENCES content_blobs(hash),
            status          TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_content (
            id              TEXT PRIMARY KEY,
            event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            block_index     INTEGER NOT NULL,
            block_type      TEXT NOT NULL,
            content         TEXT NOT NULL,
            UNIQUE (event_id, block_index)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_content_event ON event_content(event_id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attributes (
            id              TEXT PRIMARY KEY,
            target_kind     TEXT NOT NULL,
            target_id       TEXT NOT NULL,
            key             TEXT NOT NULL,
            value           TEXT NOT NULL,
            scope           TEXT,
            UNIQUE (target_kind, target_id, key, scope)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attributes_target ON attributes(target_kind, target_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attributes_key"
        " ON attributes(key, target_kind, target_id, value)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tag_assignments (
            id              TEXT PRIMARY KEY,
            target_kind     TEXT NOT NULL,
            target_id       TEXT NOT NULL,
            tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at      TEXT NOT NULL,
            UNIQUE (target_kind, target_id, tag_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_assignments_target"
        " ON tag_assignments(target_kind, target_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_assignments_tag ON tag_assignments(tag_id)"
    )

    # Progress: report what's about to be copied. The polymorphic backfill is the
    # I/O-heavy phase (writes the bulk of the migration WAL) and previously ran
    # in silence for tens of minutes on real data.
    _n_prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    _n_responses = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    _n_tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    _logger.info(
        "Migration v4: copying %d prompts, %d responses, %d tool_calls into events",
        _n_prompts, _n_responses, _n_tool_calls,
    )

    # 2. Backfill events from prompts (kind='prompt', parent_id=NULL)
    conn.execute("""
        INSERT OR IGNORE INTO events (id, kind, conversation_id, parent_id, external_id, timestamp)
        SELECT id, 'prompt', conversation_id, NULL, external_id, timestamp
        FROM prompts
    """)

    # 3. Backfill events from responses (kind='response', parent_id=prompt_id)
    conn.execute("""
        INSERT OR IGNORE INTO events (id, kind, conversation_id, parent_id, external_id, timestamp)
        SELECT id, 'response', conversation_id, prompt_id, external_id, timestamp
        FROM responses
    """)

    # 3.5 Pre-pass: suffix duplicate tool_call external_ids within the same conversation.
    #
    # events has UNIQUE (conversation_id, kind, external_id). If two tool_calls rows in the
    # same conversation share a non-NULL external_id, the backfill INSERT OR IGNORE would
    # silently drop one of them — causing the post-backfill assertion to fail (count mismatch)
    # and rolling back the whole migration.
    #
    # Fix: rewrite external_id in tool_calls to external_id || ':n' (rn=row number within
    # the duplicate group) for every row beyond the first. This mutates the soon-to-be-dropped
    # legacy table and is bounded by row count. The suffixed IDs are permanent post-migration.
    conn.execute("""
        UPDATE tool_calls
        SET external_id = tool_calls.external_id || ':' || ranked.rn
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY conversation_id, external_id
                       ORDER BY COALESCE(timestamp, ''), id
                   ) AS rn
            FROM tool_calls
            WHERE external_id IS NOT NULL
        ) AS ranked
        WHERE tool_calls.id = ranked.id
          AND ranked.rn > 1
    """)

    # 4. Backfill events from tool_calls (kind='tool_call', parent_id=response_id)
    #    tool_calls.timestamp is nullable; use empty string as sentinel for NOT NULL.
    conn.execute("""
        INSERT OR IGNORE INTO events (id, kind, conversation_id, parent_id, external_id, timestamp)
        SELECT id, 'tool_call', conversation_id, response_id, external_id,
               COALESCE(timestamp, '')
        FROM tool_calls
    """)

    # 5. Backfill sparse extensions
    conn.execute("""
        INSERT OR IGNORE INTO event_response (event_id, model_id, provider_id, input_tokens, output_tokens)
        SELECT id, model_id, provider_id, input_tokens, output_tokens
        FROM responses
    """)
    conn.execute("""
        INSERT OR IGNORE INTO event_tool_call (event_id, tool_id, input, result_hash, status)
        SELECT id, tool_id, input, result_hash, status
        FROM tool_calls
    """)

    # 6. Backfill content (IDs preserved from prompt_content / response_content)
    conn.execute("""
        INSERT OR IGNORE INTO event_content (id, event_id, block_index, block_type, content)
        SELECT id, prompt_id, block_index, block_type, content
        FROM prompt_content
    """)
    conn.execute("""
        INSERT OR IGNORE INTO event_content (id, event_id, block_index, block_type, content)
        SELECT id, response_id, block_index, block_type, content
        FROM response_content
    """)

    # 7. Backfill attributes from all four old tables
    conn.execute("""
        INSERT OR IGNORE INTO attributes (id, target_kind, target_id, key, value, scope)
        SELECT id, 'conversation', conversation_id, key, value, scope
        FROM conversation_attributes
    """)
    conn.execute("""
        INSERT OR IGNORE INTO attributes (id, target_kind, target_id, key, value, scope)
        SELECT id, 'prompt', prompt_id, key, value, scope
        FROM prompt_attributes
    """)
    conn.execute("""
        INSERT OR IGNORE INTO attributes (id, target_kind, target_id, key, value, scope)
        SELECT id, 'response', response_id, key, value, scope
        FROM response_attributes
    """)
    conn.execute("""
        INSERT OR IGNORE INTO attributes (id, target_kind, target_id, key, value, scope)
        SELECT id, 'tool_call', tool_call_id, key, value, scope
        FROM tool_call_attributes
    """)

    # 8. Backfill tag_assignments from old junction tables
    conn.execute("""
        INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at)
        SELECT id, 'workspace', workspace_id, tag_id, applied_at
        FROM workspace_tags
    """)
    conn.execute("""
        INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at)
        SELECT id, 'conversation', conversation_id, tag_id, applied_at
        FROM conversation_tags
    """)
    conn.execute("""
        INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at)
        SELECT id, 'tool_call', tool_call_id, tag_id, applied_at
        FROM tool_call_tags
    """)

    # prompt_tags is half-wired: present on any DB opened with current code, but
    # the API never writes to it.  Guard against DBs that predate ensure_prompt_tags_table().
    has_prompt_tags = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_tags'"
    ).fetchone() is not None
    if has_prompt_tags:
        conn.execute("""
            INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at)
            SELECT id, 'prompt', prompt_id, tag_id, applied_at
            FROM prompt_tags
        """)

    # 9. Row-count assertions: mismatch raises MigrationAssertionError → caller rolls back.
    def _count(q: str) -> int:
        return conn.execute(q).fetchone()[0]

    def _assert_eq(label: str, actual_q: str, expected: int) -> None:
        actual = _count(actual_q)
        if actual != expected:
            raise MigrationAssertionError(
                f"Migration v4 assertion failed — {label}: expected {expected}, got {actual}"
            )

    _assert_eq(
        "events[prompt]",
        "SELECT COUNT(*) FROM events WHERE kind='prompt'",
        _count("SELECT COUNT(*) FROM prompts"),
    )
    _assert_eq(
        "events[response]",
        "SELECT COUNT(*) FROM events WHERE kind='response'",
        _count("SELECT COUNT(*) FROM responses"),
    )
    _assert_eq(
        "events[tool_call]",
        "SELECT COUNT(*) FROM events WHERE kind='tool_call'",
        _count("SELECT COUNT(*) FROM tool_calls"),
    )
    _assert_eq(
        "event_response",
        "SELECT COUNT(*) FROM event_response",
        _count("SELECT COUNT(*) FROM responses"),
    )
    _assert_eq(
        "event_tool_call",
        "SELECT COUNT(*) FROM event_tool_call",
        _count("SELECT COUNT(*) FROM tool_calls"),
    )
    _assert_eq(
        "event_content",
        "SELECT COUNT(*) FROM event_content",
        _count("SELECT COUNT(*) FROM prompt_content") +
        _count("SELECT COUNT(*) FROM response_content"),
    )
    _assert_eq(
        "attributes",
        "SELECT COUNT(*) FROM attributes",
        _count("SELECT COUNT(*) FROM conversation_attributes") +
        _count("SELECT COUNT(*) FROM prompt_attributes") +
        _count("SELECT COUNT(*) FROM response_attributes") +
        _count("SELECT COUNT(*) FROM tool_call_attributes"),
    )
    expected_tag_assignments = (
        _count("SELECT COUNT(*) FROM workspace_tags") +
        _count("SELECT COUNT(*) FROM conversation_tags") +
        _count("SELECT COUNT(*) FROM tool_call_tags") +
        (_count("SELECT COUNT(*) FROM prompt_tags") if has_prompt_tags else 0)
    )
    _assert_eq(
        "tag_assignments",
        "SELECT COUNT(*) FROM tag_assignments",
        expected_tag_assignments,
    )


MIGRATIONS[4] = _migrate_v4_polymorphic_schema


def _migrate_v5_fts_simplification(conn: sqlite3.Connection) -> None:
    """Drop content_fts with old side column, recreate from event_content."""
    _n_blocks = conn.execute("""
        SELECT COUNT(*) FROM event_content
        WHERE json_valid(content) AND json_extract(content, '$.text') IS NOT NULL
    """).fetchone()[0]
    _logger.info("Migration v5: rebuilding FTS5 index from %d text blocks", _n_blocks)
    conn.execute("DROP TABLE IF EXISTS content_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE content_fts USING fts5(
            text_content,
            event_content_id UNINDEXED,
            event_id         UNINDEXED,
            conversation_id  UNINDEXED,
            tokenize='porter unicode61 remove_diacritics 1'
        )
    """)
    conn.execute("""
        INSERT INTO content_fts (text_content, event_content_id, event_id, conversation_id)
        SELECT
            json_extract(ec.content, '$.text'),
            ec.id,
            ec.event_id,
            e.conversation_id
        FROM event_content ec
        JOIN events e ON e.id = ec.event_id
        WHERE json_valid(ec.content)
          AND json_extract(ec.content, '$.text') IS NOT NULL
    """)
    fts_count = conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0]
    ec_count = conn.execute("""
        SELECT COUNT(*) FROM event_content
        WHERE json_valid(content)
          AND json_extract(content, '$.text') IS NOT NULL
    """).fetchone()[0]
    if fts_count != ec_count:
        raise RuntimeError(
            f"FTS5 repopulation mismatch: {fts_count} FTS rows vs {ec_count} event_content rows"
        )


MIGRATIONS[5] = _migrate_v5_fts_simplification


def _migrate_v6_drop_legacy_tables(conn: sqlite3.Connection) -> None:
    """v6: Drop 13 legacy tables and heal content_blob ref_count."""
    # Step 0: Inline blob migration for any tool_calls rows whose result TEXT was never
    # migrated to content_blobs (migrate_blobs.py was a separate optional step pre-slice-8).
    # Must run BEFORE the ref_count heal so the heal sees the newly-created blob rows.
    has_tool_calls = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchone()
    if has_tool_calls:
        unmigrated = conn.execute(
            "SELECT id, result FROM tool_calls WHERE result IS NOT NULL AND result_hash IS NULL"
        ).fetchall()
        from datetime import UTC
        from datetime import datetime as _datetime
        now = _datetime.now(UTC).isoformat()
        for row in unmigrated:
            tool_call_id = row[0]
            result_text = row[1]
            blob_hash = hashlib.sha256(result_text.encode()).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO content_blobs (hash, content, ref_count, created_at)"
                " VALUES (?, ?, 0, ?)",
                (blob_hash, result_text, now),
            )
            conn.execute(
                "UPDATE tool_calls SET result_hash = ? WHERE id = ?",
                (blob_hash, tool_call_id),
            )
            conn.execute(
                "UPDATE event_tool_call SET result_hash = ? WHERE event_id = ?",
                (blob_hash, tool_call_id),
            )

    # Step 1: Heal ref_count before any drops.
    # The naive correlated-subquery form is O(M·N) and pinned a CPU for 44+ min
    # on a 2.9G real-world db. Index event_tool_call(result_hash) and rewrite as
    # a single set-based UPDATE so event_tool_call is scanned once.
    _n_blobs = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
    _n_etc = conn.execute("SELECT COUNT(*) FROM event_tool_call").fetchone()[0]
    _logger.info(
        "Migration v6: healing content_blob ref counts (%d blobs, %d tool_call refs)",
        _n_blobs, _n_etc,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_tool_call_result_hash"
        " ON event_tool_call(result_hash) WHERE result_hash IS NOT NULL"
    )
    conn.execute("""
        WITH counts AS (
            SELECT result_hash, COUNT(*) AS c
            FROM event_tool_call
            WHERE result_hash IS NOT NULL
            GROUP BY result_hash
        )
        UPDATE content_blobs
        SET ref_count = COALESCE(
            (SELECT c FROM counts WHERE counts.result_hash = content_blobs.hash), 0
        )
    """)
    conn.execute("DELETE FROM content_blobs WHERE ref_count = 0")

    # Step 2: Drop old tables in child→parent order
    _legacy_tables = (
        "tool_call_attributes",
        "tool_call_tags",
        "response_content",
        "response_attributes",
        "prompt_content",
        "prompt_attributes",
        "prompt_tags",
        "conversation_attributes",
        "conversation_tags",
        "workspace_tags",
        "tool_calls",
        "responses",
        "prompts",
    )
    _logger.info("Migration v6: dropping %d legacy tables", len(_legacy_tables))
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in _legacy_tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys = ON")


MIGRATIONS[6] = _migrate_v6_drop_legacy_tables


def _migrate_v7_pending_tags_exchange_index(conn: sqlite3.Connection) -> None:
    """v7: Increment pending_tags.exchange_index by 1 (0-based → 1-based).

    Slice 5 changed the exchange tagging API from 0-based to 1-based indices.
    Any pending_tags rows written before that change carry 0-based indices and
    must be incremented before the strict >= 1 validation in _get_prompt_by_index
    can be relied upon.
    """
    # pending_tags may not exist on very old DBs that skipped ensure_session_tables;
    # ensure it exists before touching it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_tags (
            id                 TEXT PRIMARY KEY,
            harness_session_id TEXT NOT NULL,
            tag_name           TEXT NOT NULL,
            entity_type        TEXT NOT NULL DEFAULT 'conversation',
            exchange_index     INTEGER,
            created_at         TEXT NOT NULL
        )
    """)
    before_count = conn.execute(
        "SELECT COUNT(*) FROM pending_tags WHERE exchange_index IS NOT NULL"
    ).fetchone()[0]
    _logger.info(
        "Migration v7: rewriting %d pending_tags exchange_index entries", before_count
    )

    conn.execute(
        "UPDATE pending_tags SET exchange_index = exchange_index + 1 WHERE exchange_index IS NOT NULL"
    )

    after_count = conn.execute(
        "SELECT COUNT(*) FROM pending_tags WHERE exchange_index IS NOT NULL"
    ).fetchone()[0]
    if after_count != before_count:
        raise RuntimeError(
            f"Migration v7 assertion failed — pending_tags count changed: "
            f"before={before_count}, after={after_count}"
        )
    invalid = conn.execute(
        "SELECT COUNT(*) FROM pending_tags WHERE exchange_index IS NOT NULL AND exchange_index < 1"
    ).fetchone()[0]
    if invalid:
        raise RuntimeError(
            f"Migration v7 assertion failed — {invalid} rows still have exchange_index < 1 after increment"
        )


MIGRATIONS[7] = _migrate_v7_pending_tags_exchange_index


def _migrate_v8_drop_tool_search(conn: sqlite3.Connection) -> None:
    """v8: Drop tool_search projection table and its FTS5 virtual table.

    tool_search was a denormalized projection introduced pre-v0.8.0 to compensate
    for the four-fork storage. Post-v0.8.0 the events substrate renders it redundant.
    Production data had 400k+ FK orphans against tables dropped in v6.
    Rollback: restore from the auto-generated pre-migration backup file
    (<db>.bak.YYYYMMDD.db next to the database).
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_search'"
    ).fetchone()
    if has_table:
        row_count = conn.execute("SELECT COUNT(*) FROM tool_search").fetchone()[0]
        _logger.info("Migration v8: dropping tool_search projection (~%d rows reclaimable)", row_count)
    else:
        _logger.info("Migration v8: tool_search table absent, nothing to drop")
    # FTS virtual table must be dropped first; it binds to tool_search rowids.
    conn.execute("DROP TABLE IF EXISTS tool_search_fts")
    conn.execute("DROP TABLE IF EXISTS tool_search")
    # SQLite drops triggers tied to the parent table automatically; no explicit
    # DROP TRIGGER needed.


MIGRATIONS[8] = _migrate_v8_drop_tool_search


def _migrate_v9_usage_rollup(conn: sqlite3.Connection) -> None:
    """v9: add the usage_by_conv_model rollup; re-derive conversation_stats from it.

    Creates the keystone derived-tier fact table at grain
    (conversation_id, model_id, provider_id), backfills it from the event tables,
    and rebuilds conversation_stats as a cache over it.  The single per-response
    cost definition now lives in usage_rollup.rebuild_usage_by_conv_model.

    Caller (open_database runner) owns the transaction — this function must not
    call conn.commit(), conn.rollback(), or conn.execute("BEGIN").
    Rollback on assertion failure: restore from the auto-generated pre-migration
    backup file (<db>.bak.YYYYMMDD.db next to the database).
    """
    from siftd.storage.usage_rollup import (
        ensure_usage_by_conv_model_table,
        rebuild_rollups,
    )

    # Canonical pre-migration cost total from the existing (v8, raw-derived)
    # conversation_stats — used to assert the rollup reproduces it.
    pre_cost = None
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_stats'"
    ).fetchone():
        pre_cost = conn.execute("SELECT SUM(cost) FROM conversation_stats").fetchone()[0]

    ensure_usage_by_conv_model_table(conn)
    rebuild_rollups(conn)  # rollup from raw, then conversation_stats from rollup; no commit

    # The rollup is built from the flattened response rows (events JOIN
    # event_response).  A bare response event with no event_response extension
    # contributes nothing — a legitimate (if degenerate) state on synthetic /
    # legacy DBs — so integrity keys off the flatten count, not the response-event
    # count.  Surface (but do not abort on) any such orphans: the old
    # conversation_stats counted them and the rollup-derived one will not.
    flatten_n = conn.execute(
        "SELECT COUNT(*) FROM events e JOIN event_response er ON er.event_id = e.id "
        "WHERE e.kind = 'response'"
    ).fetchone()[0]
    n_resp_events = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'response'").fetchone()[0]
    if n_resp_events != flatten_n:
        _logger.warning(
            "Migration v9: %d response event(s) lack an event_response row and are "
            "excluded from the usage rollup (previously counted by conversation_stats).",
            n_resp_events - flatten_n,
        )

    n_rollup = conn.execute("SELECT COUNT(*) FROM usage_by_conv_model").fetchone()[0]
    if flatten_n > 0 and n_rollup == 0:
        raise MigrationAssertionError(
            f"Migration v9: usage_by_conv_model empty despite {flatten_n} response rows"
        )

    # Integrity: every flattened response is counted exactly once in the rollup
    # (catches a GROUP BY / join that silently drops or duplicates rows).
    post_resp = conn.execute(
        "SELECT COALESCE(SUM(response_count), 0) FROM usage_by_conv_model"
    ).fetchone()[0]
    if post_resp != flatten_n:
        raise MigrationAssertionError(
            f"Migration v9 response_count integrity: rollup SUM={post_resp} "
            f"vs {flatten_n} flattened response rows"
        )

    # Cost parity catches the 290x fan-out class (orders of magnitude).  The
    # tolerance absorbs accumulated per-conversation rounding (pre is rounded
    # per-conv; the rollup total is unrounded) — it is NOT a bit-parity check.
    if pre_cost is not None:
        post_cost = conn.execute("SELECT SUM(cost) FROM usage_by_conv_model").fetchone()[0] or 0.0
        tol = max(1.0, abs(pre_cost) * 0.01)
        if abs(post_cost - pre_cost) > tol:
            raise MigrationAssertionError(
                f"Migration v9 cost parity: rollup SUM(cost)={post_cost:.4f} "
                f"vs pre conversation_stats SUM={pre_cost:.4f} (tol {tol:.4f})"
            )


MIGRATIONS[9] = _migrate_v9_usage_rollup


def _migrate_v10_cache_tokens(conn: sqlite3.Connection) -> None:
    """v10: fold Anthropic cache tokens into the usage fact + bill them.

    Pre-v10 the rollup summed only ``event_response.input_tokens`` — for Anthropic
    the *uncached* sliver — so tokens read ~1% of reality and cost collapsed to
    output-only (the cache-aware cost_expr subtracted cache_read from an
    already-uncached input → clamped to 0). The cache_read / cache_creation tokens
    were captured in the polymorphic ``attributes`` table but never reached the
    fact.

    This migration adds the cache token columns to ``usage_by_conv_model`` and the
    override-only cache-rate columns to ``pricing``, then rebuilds: ``input_tokens``
    becomes the TRUE total (uncached + cache_read + cache_creation, normalized per
    provider convention), the cache components are stored disjointly, and cost
    bills all four components (see usage_rollup.rebuild_usage_by_conv_model /
    sql_helpers.cost_expr_sql).

    Cost CHANGES BY DESIGN here (≈13× up on a cache-heavy corpus), so — unlike v9 —
    there is no cost-parity assertion; the guardrails run in the new direction
    (cost/tokens grow, uncached stays non-negative). Caller owns the transaction.
    """
    from siftd.storage.usage_rollup import rebuild_rollups

    def _has_col(table: str, col: str) -> bool:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))

    # Pre-migration totals (from the v9 rollup) to assert monotonic growth.
    pre_cost = conn.execute("SELECT SUM(cost) FROM usage_by_conv_model").fetchone()[0]
    pre_input = conn.execute("SELECT COALESCE(SUM(input_tokens), 0) FROM usage_by_conv_model").fetchone()[0]

    # Additive ALTERs (idempotent — a fresh v10 DB already has them via the DDL).
    for col in ("cache_read_per_mtok", "cache_creation_per_mtok"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pricing'").fetchone() \
                and not _has_col("pricing", col):
            conn.execute(f"ALTER TABLE pricing ADD COLUMN {col} REAL")
    for col in ("cache_read_tokens", "cache_creation_tokens"):
        if not _has_col("usage_by_conv_model", col):
            conn.execute(f"ALTER TABLE usage_by_conv_model ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

    rebuild_rollups(conn)  # repopulate with cache-aware tokens + 4-component cost

    # Integrity: response_count still equals the flattened response rows (the
    # GROUP BY didn't drop or duplicate) — counts don't move with this migration.
    flatten_n = conn.execute(
        "SELECT COUNT(*) FROM events e JOIN event_response er ON er.event_id = e.id "
        "WHERE e.kind = 'response'"
    ).fetchone()[0]
    post_resp = conn.execute(
        "SELECT COALESCE(SUM(response_count), 0) FROM usage_by_conv_model"
    ).fetchone()[0]
    if post_resp != flatten_n:
        raise MigrationAssertionError(
            f"Migration v10 response_count integrity: rollup SUM={post_resp} "
            f"vs {flatten_n} flattened response rows"
        )

    # uncached is non-negative: input_tokens (the true total) must be >= the cache
    # components in every row, or the convention normalization produced nonsense.
    bad = conn.execute(
        "SELECT COUNT(*) FROM usage_by_conv_model "
        "WHERE cache_read_tokens + cache_creation_tokens > input_tokens"
    ).fetchone()[0]
    if bad:
        raise MigrationAssertionError(
            f"Migration v10: {bad} rollup row(s) have cache tokens exceeding the "
            f"total input_tokens (uncached < 0) — convention normalization is wrong"
        )

    # New-direction guardrails (the inverse of v9's parity): folding cache in only
    # adds tokens and cost, so post >= pre. A corpus with no Anthropic cache is
    # unchanged (>=, not strictly >).
    post_input = conn.execute("SELECT COALESCE(SUM(input_tokens), 0) FROM usage_by_conv_model").fetchone()[0]
    if post_input < pre_input:
        raise MigrationAssertionError(
            f"Migration v10: total input_tokens shrank ({post_input} < {pre_input}) "
            f"— folding cache in must not reduce the token total"
        )
    if pre_cost is not None:
        post_cost = conn.execute("SELECT SUM(cost) FROM usage_by_conv_model").fetchone()[0]
        if post_cost is not None and post_cost < pre_cost - max(1.0, abs(pre_cost) * 0.01):
            raise MigrationAssertionError(
                f"Migration v10: total cost shrank (post={post_cost:.4f} < "
                f"pre={pre_cost:.4f}) — billing cache components must not reduce cost"
            )


MIGRATIONS[10] = _migrate_v10_cache_tokens


def _project_pricing_reference(conn: sqlite3.Connection) -> None:
    """UPSERT the pricing reference (siftd/data/pricing.toml + user override) into
    the pricing table, keyed by canonical (model name, provider name).

    This is the projection that makes the table reference-derived rather than
    born-frozen: ON CONFLICT corrects an existing (model_id, provider_id) row to the
    current reference value (e.g. a stale sync-imported price), while leaving
    out-of-band rows for models the reference doesn't cover untouched. A reference
    name may resolve to several model_ids (spelling variants that backfill_models
    canonicalized to one name) — each is priced.
    """
    from siftd.pricing import load_pricing_reference

    for e in load_pricing_reference():
        # One reference name can resolve to several model_ids (spelling variants
        # canonicalized to one name). Insert per (model_id, provider_id) with a FRESH
        # id each — a single shared id across N fresh rows would collide on the PK
        # (which ON CONFLICT(model_id, provider_id) does not catch).
        targets = conn.execute(
            "SELECT m.id AS model_id, p.id AS provider_id FROM models m "
            "JOIN providers p ON p.name = ? WHERE m.name = ?",
            (e.provider, e.model),
        ).fetchall()
        for t in targets:
            conn.execute(
                """
                INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok,
                                     cache_read_per_mtok, cache_creation_per_mtok, source, as_of)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id, provider_id) DO UPDATE SET
                    input_per_mtok          = excluded.input_per_mtok,
                    output_per_mtok         = excluded.output_per_mtok,
                    cache_read_per_mtok     = excluded.cache_read_per_mtok,
                    cache_creation_per_mtok = excluded.cache_creation_per_mtok,
                    source                  = excluded.source,
                    as_of                   = excluded.as_of
                """,
                (_ulid(), t["model_id"], t["provider_id"], e.input_per_mtok, e.output_per_mtok,
                 e.cache_read_per_mtok, e.cache_creation_per_mtok, e.source, e.as_of),
            )


def ensure_pricing_table(conn: sqlite3.Connection) -> None:
    """Create the pricing table and project the pricing reference onto it. Idempotent.

    The table is a *projection* of the version-controlled reference (see
    ``siftd.pricing``): prices are UPSERT-applied on every open, never frozen
    (``INSERT OR IGNORE`` would have let a stale value persist forever) and never
    synced between machines. Runs at open BEFORE the migration loop, so it is
    self-healing across schema versions.

    The cache-rate columns are deliberately NOT added here — they are added by the
    v10 migration, and the rollup's cache-awareness gate keys on their presence;
    adding them early would flip v9's frozen-cost rebuild path and break v9's cost
    parity on a pre-v10 DB climbing through v9. So the projection runs only once the
    table is at v10+ shape (cache columns present); on a pre-v10 table it is deferred
    to _migrate_v11's ensure call (after v10 adds the cache columns).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing (
            id              TEXT PRIMARY KEY,
            model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
            provider_id     TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            input_per_mtok  REAL,
            output_per_mtok REAL,
            cache_read_per_mtok     REAL,
            cache_creation_per_mtok REAL,
            source          TEXT,
            as_of           TEXT,
            UNIQUE (model_id, provider_id)
        )
    """)
    # Self-heal + project only once the table is at v10+ shape (cache columns present).
    # On a pre-v10 table mid-migration we add NOTHING here: adding the cache columns
    # would flip v9's frozen-cost rebuild gate, and adding the v11 provenance columns
    # *before* v10's cache columns would make migrated column order diverge from the
    # fresh DDL (slice.py copies pricing with SELECT pr.*). _migrate_v11 calls ensure
    # again after v10 has added the cache columns — this branch then appends provenance
    # AFTER them, so migrated order == fresh order.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pricing)")}
    if "cache_read_per_mtok" in existing:
        for col in ("source", "as_of"):
            if col not in existing:
                conn.execute(f"ALTER TABLE pricing ADD COLUMN {col} TEXT")
                existing.add(col)
        _project_pricing_reference(conn)


def recanonicalize_model_names(conn: sqlite3.Connection, *, commit: bool = False) -> int:
    """Re-parse ``raw_name`` → canonical ``name`` (and parsed fields) for model rows
    the parser previously fell back on (``creator``/``family`` NULL).

    An improved :func:`parse_model_name` (e.g. the dot/dash normalization) only reaches
    *new* rows via ``get_or_create_model``; this updates existing rows so spelling
    variants like ``claude-haiku-4.5`` collapse onto the canonical name the pricing
    reference is keyed by. ``model_id`` is unchanged (FKs intact). Returns rows updated.
    """
    rows = conn.execute(
        "SELECT id, raw_name FROM models WHERE creator IS NULL OR family IS NULL"
    ).fetchall()
    updated = 0
    for row in rows:
        parsed = parse_model_name(row["raw_name"])
        if parsed["creator"] is None:  # still a fallback parse — nothing useful to set
            continue
        conn.execute(
            "UPDATE models SET name = ?, creator = ?, family = ?, version = ?, "
            "variant = ?, released = ? WHERE id = ?",
            (parsed["name"], parsed["creator"], parsed["family"], parsed["version"],
             parsed["variant"], parsed["released"], row["id"]),
        )
        updated += 1
    if commit:
        conn.commit()
    return updated


def _migrate_v11_pricing_reference(conn: sqlite3.Connection) -> None:
    """v11: the pricing table becomes a projection of the version-controlled reference.

    Pre-v11 prices were seeded with INSERT OR IGNORE (so a stale value, once present,
    was never corrected — "born-frozen") and copied between machines by sync (so the
    first-arriving value won permanently). The live corpus showed the two
    highest-volume models mispriced this way (claude-opus-4-5 frozen at 5/25 vs the
    reference's 15/75; claude-haiku-4-5 at 1/5 vs 0.80/4.0).

    This migration: (1) canonicalizes model names so the reference (keyed by canonical
    name) reaches spelling variants (e.g. claude-haiku-4.5 → claude-haiku-4-5);
    (2) reprojects the reference via UPSERT, correcting the frozen rows and adding
    provenance; (3) rebuilds the rollup so materialized cost reflects the corrected
    prices. Cost CHANGES BY DESIGN (opus-4-5 reprices ~3× up, haiku down) so there is
    no cost-parity assertion. Caller owns the transaction.
    """
    from siftd.storage.usage_rollup import rebuild_rollups

    recanonicalize_model_names(conn)      # canonicalize names → reference matches
    ensure_pricing_table(conn)            # reproject reference (UPSERT) onto pricing
    rebuild_rollups(conn)                 # re-materialize cost from corrected pricing

    # Integrity: the rebuild didn't drop or duplicate response rows.
    flatten_n = conn.execute(
        "SELECT COUNT(*) FROM events e JOIN event_response er ON er.event_id = e.id "
        "WHERE e.kind = 'response'"
    ).fetchone()[0]
    post_resp = conn.execute(
        "SELECT COALESCE(SUM(response_count), 0) FROM usage_by_conv_model"
    ).fetchone()[0]
    if post_resp != flatten_n:
        raise MigrationAssertionError(
            f"Migration v11 response_count integrity: rollup SUM={post_resp} "
            f"vs {flatten_n} flattened response rows"
        )

    # The reprojection corrected the frozen rows: every reference-covered (model,
    # provider) now matches the reference value (no stale price survived the UPSERT).
    from siftd.pricing import load_pricing_reference
    for e in load_pricing_reference():
        if e.input_per_mtok is None:
            continue
        bad = conn.execute(
            """
            SELECT pr.input_per_mtok, pr.output_per_mtok
            FROM pricing pr JOIN models m ON m.id = pr.model_id
            JOIN providers p ON p.id = pr.provider_id
            WHERE m.name = ? AND p.name = ?
              AND (pr.input_per_mtok IS NOT ? OR pr.output_per_mtok IS NOT ?)
            """,
            (e.model, e.provider, e.input_per_mtok, e.output_per_mtok),
        ).fetchone()
        if bad is not None:
            raise MigrationAssertionError(
                f"Migration v11: {e.model}/{e.provider} priced {bad[0]}/{bad[1]} "
                f"after reprojection, expected reference {e.input_per_mtok}/{e.output_per_mtok}"
            )


MIGRATIONS[11] = _migrate_v11_pricing_reference


def _migrate_v12_block_cleanup_trigger(conn: sqlite3.Connection) -> None:
    """v12: Add the event_content cascade-cleanup trigger for block tags (WS8).

    Block-level tagging introduces target_kind = 'block' referencing
    event_content.id. event_content rows cascade-delete via their events FK, but
    that FK fires no trigger touching tag_assignments/attributes — so a block tag
    would orphan. This trigger mirrors the other polymorphic cleanup triggers.
    Idempotent: ensure_polymorphic_cleanup_triggers also creates it on every write
    open, so this migration only stamps the version and guarantees old in-place
    DBs pick it up.
    """
    from siftd.storage.events import ensure_polymorphic_cleanup_triggers

    ensure_polymorphic_cleanup_triggers(conn)


MIGRATIONS[12] = _migrate_v12_block_cleanup_trigger


def ensure_content_blobs_table(conn: sqlite3.Connection) -> None:
    """Create content_blobs table and result_hash column if they don't exist. Idempotent."""
    # Create content_blobs table (with NOT NULL + CHECK added in schema v3).
    # CREATE TABLE IF NOT EXISTS won't apply new constraints to existing tables;
    # the v3 migration (_migrate_blob_integrity_v3) handles that for existing DBs.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_blobs (
            hash TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            ref_count INTEGER NOT NULL DEFAULT 1 CHECK (ref_count >= 0),
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_blobs_ref_count ON content_blobs(ref_count)"
    )


def _ensure_git_remote_index(conn: sqlite3.Connection) -> None:
    """Create index on workspaces.git_remote if it doesn't exist. Idempotent."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspaces_git_remote ON workspaces(git_remote)"
    )


def _ensure_ingested_files_conversation_index(conn: sqlite3.Connection) -> None:
    """Create index on ingested_files.conversation_id if missing. Idempotent.

    The column is read by conversation, not by path: ingest asks "does another
    path point at this conversation?" before every replace, and the
    ``ON DELETE CASCADE`` from ``conversations`` asks the same question on
    every conversation delete. Both were full table scans.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ingested_files_conversation "
        "ON ingested_files(conversation_id)"
    )


def _ensure_conversation_stats_table(conn: sqlite3.Connection) -> None:
    """Create the conversation_stats materialized table. Idempotent."""
    from siftd.storage.conversation_stats import ensure_conversation_stats_table

    ensure_conversation_stats_table(conn)


def _ensure_usage_by_conv_model_table(conn: sqlite3.Connection) -> None:
    """Create the usage_by_conv_model rollup table. Idempotent."""
    from siftd.storage.usage_rollup import ensure_usage_by_conv_model_table

    ensure_usage_by_conv_model_table(conn)


def ensure_push_log_table(conn: sqlite3.Connection) -> None:
    """Create push_log table for server attribution. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_log (
            push_id TEXT PRIMARY KEY,
            user_identity TEXT NOT NULL,
            pushed_at TEXT NOT NULL,
            conversations INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            source_ip TEXT
        )
    """)


def ensure_audit_log_table(conn: sqlite3.Connection) -> None:
    """Create audit_log table for state-changing operation provenance. Idempotent.

    Records who did what to which entity, so destructive mutations (tag
    delete/rename, etc.) on the shared multi-tenant DB are attributable rather
    than repudiable. Server-side only; mirrors push_log. See finding F6.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id          TEXT PRIMARY KEY,
            actor       TEXT NOT NULL,
            action      TEXT NOT NULL,
            target_type TEXT,
            target      TEXT,
            detail      TEXT,
            source_ip   TEXT,
            occurred_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_log_occurred
        ON audit_log(occurred_at)
    """)


def ensure_conversation_owners_table(conn: sqlite3.Connection) -> None:
    """Create conversation_owners table for multi-tenancy. Idempotent.

    Server-side only — tracks which user owns each conversation.
    Called from the open_database() migration chain and idempotently
    from the receive path.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_owners (
            conversation_id TEXT NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            user_id         TEXT NOT NULL,
            push_id         TEXT,
            assigned_at     TEXT NOT NULL,
            PRIMARY KEY (conversation_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversation_owners_user
        ON conversation_owners(user_id)
    """)


def ensure_sync_inbox_table(conn: sqlite3.Connection) -> None:
    """Create sync_inbox table for staged push payloads. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_inbox (
            id                  TEXT PRIMARY KEY,
            received_at         TEXT NOT NULL,
            processed_at        TEXT,
            processing_started_at TEXT,
            status              TEXT NOT NULL DEFAULT 'staged',
            error               TEXT,
            source_host         TEXT,
            size_bytes          INTEGER,
            conversations       INTEGER,
            user_id             TEXT,
            push_id             TEXT
        )
    """)
    # Migration: add processing_started_at if missing (pre-existing DBs)
    try:
        conn.execute("SELECT processing_started_at FROM sync_inbox LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE sync_inbox ADD COLUMN processing_started_at TEXT"
        )
    # Migration: add user_id/push_id so a deferred (staged) merge carries the
    # pushing identity into the owner-partitioned merge — without it a staged
    # push would merge unscoped and reopen the write-path IDOR the synchronous
    # path closes. Pre-existing staged rows have NULL user_id (merged unscoped,
    # as they were before this column existed).
    try:
        conn.execute("SELECT user_id FROM sync_inbox LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sync_inbox ADD COLUMN user_id TEXT")
        conn.execute("ALTER TABLE sync_inbox ADD COLUMN push_id TEXT")


# Alias for backwards compatibility
create_database = open_database


# =============================================================================
# Vocabulary entities (get-or-create)
# =============================================================================


# In-process caches for vocabulary lookups (valid within single connection lifetime)
_harness_cache: dict[str, str] = {}
_provider_cache: dict[str, str] = {}
_model_cache: dict[str, str] = {}


def clear_vocabulary_caches() -> None:
    """Clear all in-process vocabulary caches.

    Called when opening a new database connection to prevent stale IDs.
    """
    _harness_cache.clear()
    _provider_cache.clear()
    _model_cache.clear()
    # These are defined later in the module but accessible as globals
    if "_tool_alias_cache" in globals():
        _tool_alias_cache.clear()
    if "_tool_name_cache" in globals():
        _tool_name_cache.clear()
    # Clear tag cache from tags module
    try:
        from siftd.storage import tags
        tags._tag_cache.clear()
    except (ImportError, AttributeError):  # pragma: no cover
        pass
    # Reset blob batch timestamp so new connections get fresh timestamps
    try:
        from siftd.storage import blobs
        blobs._batch_timestamp = None
    except (ImportError, AttributeError):  # pragma: no cover
        pass


_HARNESS_COLS = frozenset({"version", "display_name", "source", "log_format"})
_PROVIDER_COLS = frozenset({"display_name", "billing_model"})
_TOOL_COLS = frozenset({"category", "description"})
_MODEL_COLS = frozenset({"name", "creator", "family", "version", "variant", "released"})


def get_or_create_harness(conn: sqlite3.Connection, name: str, **kwargs) -> str:
    """Get or create harness, return id (ULID)."""
    unknown = set(kwargs) - _HARNESS_COLS
    if unknown:
        raise ValueError(
            f"unknown column(s) for harnesses: {sorted(unknown)}; allowed: {sorted(_HARNESS_COLS)}"
        )
    if name in _harness_cache:
        return _harness_cache[name]

    cur = conn.execute("SELECT id FROM harnesses WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        _harness_cache[name] = row["id"]
        return row["id"]

    ulid = _ulid()
    cols = ["id", "name"] + list(kwargs.keys())
    vals = [ulid, name] + list(kwargs.values())
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO harnesses ({col_names}) VALUES ({placeholders})", vals)
    _harness_cache[name] = ulid
    return ulid


def harness_id_for_conversation(conn: sqlite3.Connection, conversation) -> str:
    """Resolve (creating if needed) the harness row a parsed conversation names.

    One home for the optional-field dance that every caller needing a
    conversation's ``harness_id`` — to store it, or to look one up by
    ``(harness_id, external_id)`` — was otherwise repeating.
    """
    harness_kwargs = {}
    if conversation.harness.source:
        harness_kwargs["source"] = conversation.harness.source
    if conversation.harness.log_format:
        harness_kwargs["log_format"] = conversation.harness.log_format
    if conversation.harness.display_name:
        harness_kwargs["display_name"] = conversation.harness.display_name
    return get_or_create_harness(conn, conversation.harness.name, **harness_kwargs)


def get_or_create_workspace(conn: sqlite3.Connection, path: str, discovered_at: str) -> str:
    """Get or create workspace, return id (ULID).

    Uses git remote URL as the primary identity when available, falling back
    to normalized filesystem path. This allows the same repository to be
    recognized across different machines or path locations.

    The path is normalized (resolved to absolute) before lookup/storage to
    ensure consistent matching regardless of how the path was specified.
    """
    from siftd.git import get_canonical_workspace_identity

    git_remote, normalized_path = get_canonical_workspace_identity(path)

    # If git remote exists, check if we already have this repo by remote
    if git_remote:
        cur = conn.execute(
            "SELECT id, git_remote FROM workspaces WHERE git_remote = ?",
            (git_remote,)
        )
        row = cur.fetchone()
        if row:
            return row["id"]

    # Fallback: check by normalized path
    cur = conn.execute("SELECT id, git_remote FROM workspaces WHERE path = ?", (normalized_path,))
    row = cur.fetchone()
    if row:
        # Update git_remote if we now know it and it wasn't set before
        if git_remote and not row["git_remote"]:
            conn.execute(
                "UPDATE workspaces SET git_remote = ? WHERE id = ?",
                (git_remote, row["id"])
            )
        return row["id"]

    # Create new workspace with normalized path
    ulid = _ulid()
    conn.execute(
        "INSERT INTO workspaces (id, path, git_remote, discovered_at) VALUES (?, ?, ?, ?)",
        (ulid, normalized_path, git_remote, discovered_at)
    )
    return ulid


def get_or_create_model(conn: sqlite3.Connection, raw_name: str, **kwargs) -> str:
    """Get or create model, return id (ULID).

    On creation, parses raw_name into structured fields (name, creator,
    family, version, variant, released) using parse_model_name().
    Explicit kwargs override parsed values.
    """
    unknown = set(kwargs) - _MODEL_COLS
    if unknown:
        raise ValueError(
            f"unknown column(s) for models: {sorted(unknown)}; allowed: {sorted(_MODEL_COLS)}"
        )
    if raw_name in _model_cache:
        return _model_cache[raw_name]

    cur = conn.execute("SELECT id FROM models WHERE raw_name = ?", (raw_name,))
    row = cur.fetchone()
    if row:
        _model_cache[raw_name] = row["id"]
        return row["id"]

    parsed = parse_model_name(raw_name)
    # Explicit kwargs override parsed values
    parsed.update(kwargs)

    ulid = _ulid()
    cols = ["id", "raw_name", "name", "creator", "family", "version", "variant", "released"]
    vals = [ulid, raw_name, parsed["name"], parsed["creator"], parsed["family"],
            parsed["version"], parsed["variant"], parsed["released"]]
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO models ({col_names}) VALUES ({placeholders})", vals)
    _model_cache[raw_name] = ulid
    return ulid


def get_or_create_provider(conn: sqlite3.Connection, name: str, **kwargs) -> str:
    """Get or create provider, return id (ULID)."""
    unknown = set(kwargs) - _PROVIDER_COLS
    if unknown:
        raise ValueError(
            f"unknown column(s) for providers: {sorted(unknown)}; allowed: {sorted(_PROVIDER_COLS)}"
        )
    if name in _provider_cache:
        return _provider_cache[name]

    cur = conn.execute("SELECT id FROM providers WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        _provider_cache[name] = row["id"]
        return row["id"]

    ulid = _ulid()
    cols = ["id", "name"] + list(kwargs.keys())
    vals = [ulid, name] + list(kwargs.values())
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO providers ({col_names}) VALUES ({placeholders})", vals)
    _provider_cache[name] = ulid
    return ulid


def get_or_create_tool(conn: sqlite3.Connection, name: str, **kwargs) -> str:
    """Get or create tool, return id (ULID)."""
    unknown = set(kwargs) - _TOOL_COLS
    if unknown:
        raise ValueError(
            f"unknown column(s) for tools: {sorted(unknown)}; allowed: {sorted(_TOOL_COLS)}"
        )
    cur = conn.execute("SELECT id FROM tools WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]

    ulid = _ulid()
    cols = ["id", "name"] + list(kwargs.keys())
    vals = [ulid, name] + list(kwargs.values())
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO tools ({col_names}) VALUES ({placeholders})", vals)
    return ulid


# Cache: (raw_name, harness_id) -> tool_id
_tool_alias_cache: dict[tuple[str, str], str] = {}
# Cache: tool_id -> canonical_name
_tool_name_cache: dict[str, str] = {}


def get_or_create_tool_by_alias(conn: sqlite3.Connection, raw_name: str, harness_id: str) -> str:
    """Look up tool by alias for this harness, or create with raw name as canonical."""
    cache_key = (raw_name, harness_id)
    if cache_key in _tool_alias_cache:
        return _tool_alias_cache[cache_key]

    # Check alias first (harness-specific)
    cur = conn.execute(
        "SELECT tool_id FROM tool_aliases WHERE raw_name = ? AND harness_id = ?",
        (raw_name, harness_id)
    )
    row = cur.fetchone()
    if row:
        _tool_alias_cache[cache_key] = row["tool_id"]
        return row["tool_id"]

    # Check if tool exists with this name
    cur = conn.execute("SELECT id FROM tools WHERE name = ?", (raw_name,))
    row = cur.fetchone()
    if row:
        tool_id = row["id"]
    else:
        # Create new tool with raw name as canonical (for now)
        tool_id = _ulid()
        conn.execute("INSERT INTO tools (id, name) VALUES (?, ?)", (tool_id, raw_name))

    # Create alias for this harness
    alias_id = _ulid()
    conn.execute(
        "INSERT OR IGNORE INTO tool_aliases (id, raw_name, harness_id, tool_id) VALUES (?, ?, ?, ?)",
        (alias_id, raw_name, harness_id, tool_id)
    )
    _tool_alias_cache[cache_key] = tool_id
    return tool_id


# =============================================================================
# Canonical tools taxonomy
# =============================================================================

CANONICAL_TOOLS: list[dict[str, str]] = [
    {"name": "file.read", "category": "file", "description": "Read file contents"},
    {"name": "file.write", "category": "file", "description": "Write/create a file"},
    {"name": "file.edit", "category": "file", "description": "Edit/modify existing file"},
    {"name": "file.glob", "category": "file", "description": "Find files by pattern"},
    {"name": "shell.execute", "category": "shell", "description": "Execute shell commands"},
    {"name": "shell.stdin", "category": "shell", "description": "Send input to running shell"},
    {"name": "search.grep", "category": "search", "description": "Search file contents"},
    {"name": "search.web", "category": "search", "description": "Web search"},
    {"name": "web.fetch", "category": "web", "description": "Fetch URL content"},
    {"name": "task.spawn", "category": "task", "description": "Launch subtask/agent"},
    {"name": "task.output", "category": "task", "description": "Get task output"},
    {"name": "task.kill", "category": "task", "description": "Kill running task"},
    {"name": "ui.ask", "category": "ui", "description": "Ask user a question"},
    {"name": "ui.todo", "category": "ui", "description": "Write todo items"},
    {"name": "notebook.edit", "category": "notebook", "description": "Edit notebook cell"},
    {"name": "skill.invoke", "category": "skill", "description": "Invoke a skill"},
]


def ensure_canonical_tools(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Insert all canonical tools if not already present. Idempotent."""
    for tool in CANONICAL_TOOLS:
        conn.execute(
            "INSERT OR IGNORE INTO tools (id, name, category, description) VALUES (?, ?, ?, ?)",
            (_ulid(), tool["name"], tool["category"], tool["description"]),
        )
    if commit:
        conn.commit()


def ensure_tool_aliases(conn: sqlite3.Connection, harness_id: str, aliases: dict[str, str]) -> None:
    """Register tool alias mappings for a harness. Idempotent.

    aliases: dict of raw_name -> canonical_name
    """
    for raw_name, canonical_name in aliases.items():
        # Look up the canonical tool id
        cur = conn.execute("SELECT id FROM tools WHERE name = ?", (canonical_name,))
        row = cur.fetchone()
        if not row:
            continue  # canonical tool not found, skip
        tool_id = row["id"]
        conn.execute(
            "INSERT OR IGNORE INTO tool_aliases (id, raw_name, harness_id, tool_id) VALUES (?, ?, ?, ?)",
            (_ulid(), raw_name, harness_id, tool_id),
        )


# =============================================================================
# Insert operations
# =============================================================================


def insert_conversation(
    conn: sqlite3.Connection,
    external_id: str,
    harness_id: str,
    workspace_id: str | None,
    started_at: str,
    branch: str | None = None,
    ended_at: str | None = None,
) -> str:
    """Insert conversation, return id (ULID)."""
    ulid = _ulid()
    conn.execute(
        """INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ulid, external_id, harness_id, workspace_id, branch, started_at, ended_at)
    )
    return ulid



# =============================================================================
# Compatibility shims (slice 2–3 transition; remove in slice 8)
# These preserve old call-sites while data is now stored in events tables.
# =============================================================================

def insert_prompt(
    conn: sqlite3.Connection,
    conversation_id: str,
    external_id: str | None,
    timestamp: str,
) -> str:
    uid = _ulid()
    insert_event(conn, uid, "prompt", conversation_id, timestamp, external_id=external_id)
    return uid


def insert_response(
    conn: sqlite3.Connection,
    conversation_id: str,
    prompt_id: str | None,
    model_id: str | None,
    provider_id: str | None,
    external_id: str | None,
    timestamp: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> str:
    uid = _ulid()
    insert_event(conn, uid, "response", conversation_id, timestamp, parent_id=prompt_id, external_id=external_id)
    insert_event_response(conn, uid, model_id=model_id, provider_id=provider_id, input_tokens=input_tokens, output_tokens=output_tokens)
    return uid


def insert_prompt_content(
    conn: sqlite3.Connection,
    prompt_id: str,
    block_index: int,
    block_type: str,
    content: str,
) -> str:
    uid = _ulid()
    insert_event_content(conn, content_id=uid, event_id=prompt_id, block_index=block_index, block_type=block_type, content=content)
    return uid


def insert_response_content(
    conn: sqlite3.Connection,
    response_id: str,
    block_index: int,
    block_type: str,
    content: str,
) -> str:
    uid = _ulid()
    insert_event_content(conn, content_id=uid, event_id=response_id, block_index=block_index, block_type=block_type, content=content)
    return uid


def insert_tool_call(
    conn: sqlite3.Connection,
    response_id: str,
    conversation_id: str,
    tool_id: str | None,
    external_id: str | None,
    input_json: str | None,
    result_json: str | None,
    status: str | None,
    timestamp: str | None,
    *,
    dedupe_result: bool = True,
    filter_binary: bool = True,
) -> str:
    uid = _ulid()
    insert_event(conn, uid, "tool_call", conversation_id, timestamp or "", parent_id=response_id, external_id=external_id)
    insert_event_tool_call(conn, uid, tool_id=tool_id, input_json=input_json, result_json=result_json, status=status, dedupe_result=dedupe_result, filter_binary=filter_binary)
    return uid


# =============================================================================
# High-level storage functions
# =============================================================================


def store_conversation(
    conn: sqlite3.Connection,
    conversation: Conversation,
    *,
    commit: bool = False,
    filter_binary: bool = True,
    _workspace_cache: dict | None = None,
) -> str:
    """Store a complete Conversation domain object.

    Walks the nested tree and calls insert_* functions.
    Caller controls commit (default: no commit).

    Args:
        conn: Database connection
        conversation: Conversation domain object to store
        commit: Whether to commit the transaction (default: False)
        filter_binary: If True (default), filter binary content (images, base64)
            from tool results before storage.
        _workspace_cache: Optional dict for caching workspace identity lookups
            across multiple calls. Pass the same dict to batch store_conversation
            calls to avoid repeated git subprocess calls.
    """
    harness_id = harness_id_for_conversation(conn, conversation)

    # Get or create provider (derived from harness source)
    provider_id = None
    if conversation.harness.source:
        provider_id = get_or_create_provider(conn, conversation.harness.source)

    # Get or create workspace
    workspace_id = None
    branch = conversation.branch
    if branch is None and conversation.workspace_path:
        from siftd.git import get_worktree_branch

        branch = get_worktree_branch(conversation.workspace_path)

    if conversation.workspace_path:
        ws_path = conversation.workspace_path
        if _workspace_cache is not None and ws_path in _workspace_cache:
            workspace_id = _workspace_cache[ws_path]
        else:
            workspace_id = get_or_create_workspace(
                conn, ws_path, conversation.started_at
            )
            if _workspace_cache is not None:
                _workspace_cache[ws_path] = workspace_id

    # Create conversation
    conversation_id = insert_conversation(
        conn,
        external_id=conversation.external_id,
        harness_id=harness_id,
        workspace_id=workspace_id,
        branch=branch,
        started_at=conversation.started_at,
        ended_at=conversation.ended_at,
    )

    # Conversation-level derived attributes (e.g. sub-agent type/description
    # from the Claude Code sidecar). scope='analyzer' marks them as adapter-
    # derived; set_attribute upserts so re-ingest is idempotent.
    for attr_key, attr_value in conversation.attributes.items():
        set_attribute(conn, "conversation", conversation_id, attr_key, attr_value, scope="analyzer")

    # Process prompts
    for prompt in conversation.prompts:
        prompt_id = insert_prompt(conn, conversation_id, prompt.external_id, prompt.timestamp)

        # Insert prompt content blocks
        for idx, block in enumerate(prompt.content):
            content_id = insert_prompt_content(conn, prompt_id, idx, block.block_type, json.dumps(block.content))
            if text := block.content.get("text"):
                insert_fts_content(conn, content_id, prompt_id, conversation_id, text)

        # Process responses for this prompt
        for response in prompt.responses:
            model_id = None
            if response.model:
                model_id = get_or_create_model(conn, response.model)

            input_tokens = None
            output_tokens = None
            if response.usage:
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens

            response_id = insert_response(
                conn,
                conversation_id=conversation_id,
                prompt_id=prompt_id,
                model_id=model_id,
                provider_id=provider_id,
                external_id=response.external_id,
                timestamp=response.timestamp,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            # Insert response content blocks
            for idx, block in enumerate(response.content):
                content_id = insert_response_content(conn, response_id, idx, block.block_type, json.dumps(block.content))
                if text := block.content.get("text"):
                    insert_fts_content(conn, content_id, response_id, conversation_id, text)

            for attr_key, attr_value in response.attributes.items():
                set_attribute(conn, "response", response_id, attr_key, attr_value, scope="provider")

            # Insert tool calls
            for tool_call in response.tool_calls:
                tool_id = get_or_create_tool_by_alias(
                    conn, tool_call.tool_name, harness_id
                )
                if tool_id not in _tool_name_cache:
                    _tool_name_cache[tool_id] = conn.execute(
                        "SELECT name FROM tools WHERE id = ?", (tool_id,)
                    ).fetchone()["name"]
                canonical_name = _tool_name_cache[tool_id]

                tool_call_id = insert_tool_call(
                    conn,
                    response_id=response_id,
                    conversation_id=conversation_id,
                    tool_id=tool_id,
                    external_id=tool_call.external_id,
                    input_json=json.dumps(tool_call.input),
                    result_json=json.dumps(tool_call.result) if tool_call.result else None,
                    status=tool_call.status,
                    timestamp=tool_call.timestamp or response.timestamp,
                    filter_binary=filter_binary,
                )

                for attr_key, attr_value in tool_call.attributes.items():
                    set_attribute(conn, "tool_call", tool_call_id, attr_key, attr_value)

                tag_shell_command(conn, tool_call_id, canonical_name, tool_call.input)
                tag_derivative_conversation(
                    conn, conversation_id, canonical_name, tool_call.input
                )

    if commit:
        conn.commit()
    return conversation_id


# =============================================================================
# Conversation lookup and deletion
# =============================================================================


def find_conversation_by_external_id(
    conn: sqlite3.Connection,
    harness_id: str,
    external_id: str,
) -> dict | None:
    """Find a conversation by harness + external_id.

    Returns dict with {id, ended_at} or None if not found.
    """
    cur = conn.execute(
        "SELECT id, ended_at FROM conversations WHERE harness_id = ? AND external_id = ?",
        (harness_id, external_id)
    )
    row = cur.fetchone()
    if row:
        return {"id": row["id"], "ended_at": row["ended_at"]}
    return None


def get_harness_id_by_name(conn: sqlite3.Connection, name: str) -> str | None:
    """Get harness ID by name."""
    cur = conn.execute("SELECT id FROM harnesses WHERE name = ?", (name,))
    row = cur.fetchone()
    return row["id"] if row else None


def delete_conversation(conn: sqlite3.Connection, conversation_id: str) -> None:
    """Delete a conversation and all related data.

    Explicitly deletes:
    - content_fts (virtual table, no FK/trigger support)

    Conversation FK CASCADE handles: events, event_content, event_response,
    event_tool_call, ingested_files, content_blobs (via tr_event_tool_call_* trigger).

    tr_polymorphic_*_cleanup triggers handle tag_assignments and attributes for
    conversations, events, and workspaces — no explicit cleanup needed here.
    """
    # Clean up FTS index entries (virtual tables don't support CASCADE or triggers)
    conn.execute(
        "DELETE FROM content_fts WHERE conversation_id = ?", (conversation_id,)
    )

    # Delete conversation - CASCADE handles events and all event_* children;
    # tr_polymorphic_conversations_cleanup cleans tag_assignments/attributes for the
    # conversation row, and tr_polymorphic_events_cleanup fires for each cascaded event row.
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# =============================================================================
# File deduplication functions
# =============================================================================


def compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def check_file_ingested(conn: sqlite3.Connection, path: str) -> bool:
    """Check if a file has already been ingested."""
    cur = conn.execute("SELECT 1 FROM ingested_files WHERE path = ?", (path,))
    return cur.fetchone() is not None


def get_ingested_file_info(conn: sqlite3.Connection, path: str) -> dict | None:
    """Get stored info for an ingested file.

    Returns dict with {file_hash, conversation_id, error, file_mtime, file_size}
    or None if not found.
    """
    cur = conn.execute(
        "SELECT file_hash, conversation_id, error, file_mtime, file_size FROM ingested_files WHERE path = ?",
        (path,)
    )
    row = cur.fetchone()
    if row:
        return {
            "file_hash": row["file_hash"],
            "conversation_id": row["conversation_id"],
            "error": row["error"],
            "file_mtime": row["file_mtime"],
            "file_size": row["file_size"],
        }
    return None


def record_ingested_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    conversation_id: str,
    *,
    file_mtime: float | None = None,
    file_size: int | None = None,
    commit: bool = False,
) -> str:
    """Record that a file has been ingested. Returns the record id.

    Derives harness_id from the conversation record.
    Caller controls commit (default: no commit).
    """
    from datetime import UTC, datetime

    harness_id = _harness_id_of(conn, conversation_id)

    ulid = _ulid()
    ingested_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at, file_mtime, file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ulid, path, file_hash, harness_id, conversation_id, ingested_at, file_mtime, file_size)
    )
    if commit:
        conn.commit()
    return ulid


def link_ingested_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    conversation_id: str,
    *,
    file_mtime: float | None = None,
    file_size: int | None = None,
    commit: bool = False,
) -> None:
    """Point a path's bookkeeping row at an existing conversation.

    Same shape as :func:`record_ingested_file` but idempotent on ``path``: it
    upserts instead of inserting, so it can repair a row that already exists
    (stale hash, NULL conversation_id, recorded error) as well as create one.
    Kept separate because the plain INSERT in ``record_ingested_file`` is what
    the normal ingest paths rely on to catch a double-record bug; this one is
    for the recovery path, where a row may or may not be there and either way
    must end up pointing at ``conversation_id`` with no error.

    Returns nothing: an upsert's row id is not news to a caller that already
    knows the path, and re-reading it took a query neither caller wanted.
    """
    from datetime import UTC, datetime

    harness_id = _harness_id_of(conn, conversation_id)

    ingested_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO ingested_files
               (id, path, file_hash, harness_id, conversation_id, ingested_at, error, file_mtime, file_size)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               file_hash=excluded.file_hash,
               harness_id=excluded.harness_id,
               conversation_id=excluded.conversation_id,
               ingested_at=excluded.ingested_at,
               error=NULL,
               file_mtime=excluded.file_mtime,
               file_size=excluded.file_size""",
        (_ulid(), path, file_hash, harness_id, conversation_id, ingested_at, file_mtime, file_size),
    )
    if commit:
        conn.commit()


def _harness_id_of(conn: sqlite3.Connection, conversation_id: str) -> str:
    """The harness a stored conversation belongs to, for its bookkeeping row."""
    row = conn.execute(
        "SELECT harness_id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Conversation not found: {conversation_id}")
    return row[0]


def record_empty_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    harness_id: str,
    *,
    file_mtime: float | None = None,
    file_size: int | None = None,
    commit: bool = False,
) -> str:
    """Record an empty file (no conversation). Returns the record id.

    Used for files that parse to zero conversations (e.g., empty JSONL files).
    Stores with conversation_id=NULL so they're tracked but can be re-ingested
    if content appears later.
    """
    from datetime import UTC, datetime

    ulid = _ulid()
    ingested_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at, file_mtime, file_size)
           VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
        (ulid, path, file_hash, harness_id, ingested_at, file_mtime, file_size)
    )
    if commit:
        conn.commit()
    return ulid


def record_session_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    harness_id: str,
    *,
    file_mtime: float | None = None,
    file_size: int | None = None,
    commit: bool = False,
) -> str:
    """Record/refresh a per-file marker for a session-strategy source.

    A session source (e.g. an OpenCode or Gemini SQLite DB) yields many
    conversations from one file; each is stored and deduped independently by
    external_id. This marker tracks the file's hash/mtime for the fast-path
    skip with conversation_id=NULL, so replacing any single session's
    conversation does not cascade-delete the marker (the conversation_id FK is
    ON DELETE CASCADE). Upserts on the unique path. Returns the record id.
    """
    from datetime import UTC, datetime

    ingested_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO ingested_files
               (id, path, file_hash, harness_id, conversation_id, ingested_at, error, file_mtime, file_size)
           VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               file_hash=excluded.file_hash,
               harness_id=excluded.harness_id,
               conversation_id=NULL,
               ingested_at=excluded.ingested_at,
               error=NULL,
               file_mtime=excluded.file_mtime,
               file_size=excluded.file_size""",
        (_ulid(), path, file_hash, harness_id, ingested_at, file_mtime, file_size),
    )
    if commit:
        conn.commit()
    row = conn.execute("SELECT id FROM ingested_files WHERE path = ?", (path,)).fetchone()
    return row[0]


def record_failed_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    harness_id: str,
    error: str,
    *,
    file_mtime: float | None = None,
    file_size: int | None = None,
    commit: bool = False,
) -> str:
    """Record a file that failed ingestion. Returns the record id.

    Stores with conversation_id=NULL and error message so the file is tracked
    and won't retry unless its hash changes.
    """
    from datetime import UTC, datetime

    ulid = _ulid()
    ingested_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at, error, file_mtime, file_size)
           VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
        (ulid, path, file_hash, harness_id, ingested_at, error, file_mtime, file_size)
    )
    if commit:
        conn.commit()
    return ulid


def clear_ingested_file_error(
    conn: sqlite3.Connection,
    path: str,
) -> None:
    """Clear error and delete the ingested_files record so the file can be re-ingested."""
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (path,))


def get_ingest_errors(conn: sqlite3.Connection) -> list[dict]:
    """Get files that failed ingestion, grouped by harness.

    Returns list of dicts with keys: path, error, harness_name.
    Returns empty list if the error column doesn't exist yet.
    """
    cur = conn.execute("PRAGMA table_info(ingested_files)")
    columns = {row[1] for row in cur.fetchall()}
    if "error" not in columns:
        return []

    cur = conn.execute("""
        SELECT f.path, f.error, COALESCE(h.name, f.harness_id) AS harness_name
        FROM ingested_files f
        LEFT JOIN harnesses h ON h.id = f.harness_id
        WHERE f.error IS NOT NULL
    """)
    return [{"path": row["path"], "error": row["error"], "harness_name": row["harness_name"]} for row in cur.fetchall()]


def get_models_without_pricing(conn: sqlite3.Connection) -> list[dict]:
    """Get models used in responses that have no pricing data.

    Returns list of dicts with keys: model_name, provider_name.
    Returns empty list if the pricing table doesn't exist yet.
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pricing'"
    )
    if not cur.fetchone():
        return []

    cur = conn.execute("""
        SELECT DISTINCT m.name as model_name, COALESCE(p.name, 'unknown') as provider_name
        FROM event_response er
        JOIN models m ON er.model_id = m.id
        LEFT JOIN providers p ON er.provider_id = p.id
        WHERE er.model_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM pricing pr
            WHERE pr.model_id = er.model_id
              AND (er.provider_id IS NULL OR pr.provider_id = er.provider_id)
          )
        ORDER BY provider_name, m.name
    """)
    return [{"model_name": row[0], "provider_name": row[1]} for row in cur.fetchall()]


def get_priced_models_without_provenance(conn: sqlite3.Connection) -> list[dict]:
    """Get models that are priced but whose pricing row has no reference provenance.

    These are "out-of-band" rows: a price reached the table by some path other than
    the version-controlled reference (historically, sync from another machine), so
    ``pricing.source IS NULL``. The projection (UPSERT from the reference) preserves
    rather than corrects them — they could be wrong and a fresh machine wouldn't have
    them. Surfacing them tells the user which models to verify and add to the
    reference. Only models WITH usage are reported. Empty list if no pricing table or
    no ``source`` column (pre-v11).
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pricing'"
    )
    if not cur.fetchone():
        return []
    if not any(r[1] == "source" for r in conn.execute("PRAGMA table_info(pricing)")):
        return []

    cur = conn.execute("""
        SELECT DISTINCT m.name AS model_name, COALESCE(p.name, 'unknown') AS provider_name
        FROM pricing pr
        JOIN models m ON m.id = pr.model_id
        LEFT JOIN providers p ON p.id = pr.provider_id
        WHERE pr.source IS NULL
          AND EXISTS (SELECT 1 FROM event_response er WHERE er.model_id = pr.model_id)
        ORDER BY provider_name, m.name
    """)
    return [{"model_name": row[0], "provider_name": row[1]} for row in cur.fetchall()]


def get_freelist_info(conn: sqlite3.Connection) -> dict:
    """Get SQLite freelist page statistics.

    Returns dict with keys: freelist_count, page_count, page_size.
    """
    return {
        "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0],
        "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
        "page_size": conn.execute("PRAGMA page_size").fetchone()[0],
    }


def get_pending_schema_migrations(conn: sqlite3.Connection) -> list[str]:
    """Detect schema migrations that haven't been applied yet.

    Returns list of human-readable migration descriptions.
    Uses the same detection logic as the _migrate_* and ensure_* functions.
    """
    pending = []

    # error column on ingested_files (from _migrate_add_error_column)
    cur = conn.execute("PRAGMA table_info(ingested_files)")
    columns = {row[1] for row in cur.fetchall()}
    if "error" not in columns:
        pending.append("add error column to ingested_files")

    # pricing table (from ensure_pricing_table)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pricing'"
    )
    if not cur.fetchone():
        pending.append("create pricing table")

    # content_blobs table (from ensure_content_blobs_table)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='content_blobs'"
    )
    if not cur.fetchone():
        pending.append("create content_blobs table")

    # FTS5 content_fts table (from ensure_fts_table)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
    )
    if not cur.fetchone():
        pending.append("create FTS5 search index")

    # file_mtime/file_size columns on ingested_files (from _migrate_add_file_stat_columns)
    cur = conn.execute("PRAGMA table_info(ingested_files)")
    columns = {row[1] for row in cur.fetchall()}
    if "file_mtime" not in columns:
        pending.append("add file_mtime/file_size columns to ingested_files")

    # branch column on conversations (from _migrate_add_branch_column)
    cur = conn.execute("PRAGMA table_info(conversations)")
    columns = {row[1] for row in cur.fetchall()}
    if "branch" not in columns:
        pending.append("add branch column to conversations")

    # active_sessions table (from ensure_session_tables)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='active_sessions'"
    )
    if not cur.fetchone():
        pending.append("create session tables")

    # git_remote index (from _ensure_git_remote_index)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_workspaces_git_remote'"
    )
    if not cur.fetchone():
        pending.append("create git_remote index on workspaces")

    # conversation_stats table (from _ensure_conversation_stats_table)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_stats'"
    )
    if not cur.fetchone():
        pending.append("create conversation_stats table")

    # conversation_owners table (from ensure_conversation_owners_table)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_owners'"
    )
    if not cur.fetchone():
        pending.append("create conversation_owners table")

    # sync_inbox table (from ensure_sync_inbox_table)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_inbox'"
    )
    if not cur.fetchone():
        pending.append("create sync_inbox table")

    return pending


def update_file_stat(
    conn: sqlite3.Connection,
    path: str,
    file_mtime: float,
    file_size: int,
) -> None:
    """Update only mtime+size for an existing ingested_files record.

    Used when file content hasn't changed (hash matches) but mtime drifted
    (e.g., backup restore, copy). Updates the stored stat so future runs
    can skip via the fast path.
    """
    conn.execute(
        "UPDATE ingested_files SET file_mtime = ?, file_size = ? WHERE path = ?",
        (file_mtime, file_size, path),
    )
