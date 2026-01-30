"""SQLite storage adapter for siftd.

Core storage primitives: connection management, migrations, vocabulary entities,
insert operations, conversation store/lookup/delete, and file deduplication.

Tag operations: see storage/tags.py
FTS5 operations: see storage/fts.py
Backfill operations: see siftd/backfill.py
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from siftd.domain import Conversation
from siftd.ids import ulid as _ulid
from siftd.model_names import parse_model_name
from siftd.storage.fts import ensure_fts_table, insert_fts_content
from siftd.storage.tags import tag_derivative_conversation, tag_shell_command

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# =============================================================================
# Connection and migrations
# =============================================================================


def open_database(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open database connection, creating schema if needed.

    Args:
        db_path: Path to the database file.
        read_only: If True, open without running migrations/ensures that write.
            This enables read-only operations (status/query/ask) against a DB that
            lives on read-only media or in restricted environments.
    """
    is_new = not db_path.exists()
    if is_new and read_only:
        raise FileNotFoundError(f"Database not found: {db_path}")

    if read_only:
        # Use URI mode with mode=ro&immutable=1 to avoid creating WAL/SHM sidecars
        # and to work on read-only filesystems. Mirrors embeddings.py approach.
        uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if is_new:
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)
        conn.commit()

    if not read_only:
        _migrate_labels_to_tags(conn)
        _migrate_add_error_column(conn)
        _migrate_add_cascade_deletes(conn)
        ensure_fts_table(conn)
        ensure_pricing_table(conn)
        ensure_canonical_tools(conn)
        ensure_tool_call_tags_table(conn)
        ensure_content_blobs_table(conn)
    return conn


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

    conn.commit()


def _migrate_add_error_column(conn: sqlite3.Connection) -> None:
    """Add error column to ingested_files if it doesn't exist."""
    cur = conn.execute("PRAGMA table_info(ingested_files)")
    columns = {row[1] for row in cur.fetchall()}
    if "error" not in columns:
        conn.execute("ALTER TABLE ingested_files ADD COLUMN error TEXT")
        conn.commit()


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
        ("tool_calls", """
            CREATE TABLE tool_calls_new (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
                external_id     TEXT,
                input           TEXT,
                result          TEXT,
                status          TEXT,
                timestamp       TEXT
            )
        """, "id, response_id, conversation_id, tool_id, external_id, input, result, status, timestamp"),
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
        ("ingested_files", """
            CREATE TABLE ingested_files_new (
                id              TEXT PRIMARY KEY,
                path            TEXT NOT NULL UNIQUE,
                file_hash       TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                ingested_at     TEXT NOT NULL,
                error           TEXT
            )
        """, "id, path, file_hash, harness_id, conversation_id, ingested_at, error"),
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


def ensure_pricing_table(conn: sqlite3.Connection) -> None:
    """Create the pricing table if it doesn't exist. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing (
            id              TEXT PRIMARY KEY,
            model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
            provider_id     TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
            input_per_mtok  REAL,
            output_per_mtok REAL,
            UNIQUE (model_id, provider_id)
        )
    """)


def ensure_tool_call_tags_table(conn: sqlite3.Connection) -> None:
    """Create the tool_call_tags table if it doesn't exist. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_call_tags (
            id              TEXT PRIMARY KEY,
            tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
            tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at      TEXT NOT NULL,
            UNIQUE (tool_call_id, tag_id)
        )
    """)


def ensure_content_blobs_table(conn: sqlite3.Connection) -> None:
    """Create content_blobs table and result_hash column if they don't exist. Idempotent."""
    # Create content_blobs table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_blobs (
            hash TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            ref_count INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_blobs_ref_count ON content_blobs(ref_count)"
    )

    # Add result_hash column to tool_calls if it doesn't exist
    cur = conn.execute("PRAGMA table_info(tool_calls)")
    columns = {row[1] for row in cur.fetchall()}
    if "result_hash" not in columns:
        conn.execute(
            "ALTER TABLE tool_calls ADD COLUMN result_hash TEXT REFERENCES content_blobs(hash)"
        )

    # Create trigger for ref_count cleanup on delete
    # Check if trigger already exists
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='tr_tool_calls_delete_release_blob'"
    )
    if not cur.fetchone():
        conn.execute("""
            CREATE TRIGGER tr_tool_calls_delete_release_blob
            AFTER DELETE ON tool_calls
            FOR EACH ROW
            WHEN OLD.result_hash IS NOT NULL
            BEGIN
                UPDATE content_blobs SET ref_count = ref_count - 1 WHERE hash = OLD.result_hash;
                DELETE FROM content_blobs WHERE hash = OLD.result_hash AND ref_count = 0;
            END
        """)

    conn.commit()


# Alias for backwards compatibility
create_database = open_database


# =============================================================================
# Vocabulary entities (get-or-create)
# =============================================================================


def get_or_create_harness(conn: sqlite3.Connection, name: str, **kwargs) -> str:
    """Get or create harness, return id (ULID)."""
    cur = conn.execute("SELECT id FROM harnesses WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]

    ulid = _ulid()
    cols = ["id", "name"] + list(kwargs.keys())
    vals = [ulid, name] + list(kwargs.values())
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO harnesses ({col_names}) VALUES ({placeholders})", vals)
    return ulid


def get_or_create_workspace(conn: sqlite3.Connection, path: str, discovered_at: str) -> str:
    """Get or create workspace, return id (ULID)."""
    cur = conn.execute("SELECT id FROM workspaces WHERE path = ?", (path,))
    row = cur.fetchone()
    if row:
        return row["id"]

    ulid = _ulid()
    conn.execute(
        "INSERT INTO workspaces (id, path, discovered_at) VALUES (?, ?, ?)",
        (ulid, path, discovered_at)
    )
    return ulid


def get_or_create_model(conn: sqlite3.Connection, raw_name: str, **kwargs) -> str:
    """Get or create model, return id (ULID).

    On creation, parses raw_name into structured fields (name, creator,
    family, version, variant, released) using parse_model_name().
    Explicit kwargs override parsed values.
    """
    cur = conn.execute("SELECT id FROM models WHERE raw_name = ?", (raw_name,))
    row = cur.fetchone()
    if row:
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
    return ulid


def get_or_create_provider(conn: sqlite3.Connection, name: str, **kwargs) -> str:
    """Get or create provider, return id (ULID)."""
    cur = conn.execute("SELECT id FROM providers WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]

    ulid = _ulid()
    cols = ["id", "name"] + list(kwargs.keys())
    vals = [ulid, name] + list(kwargs.values())
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO providers ({col_names}) VALUES ({placeholders})", vals)
    return ulid


def get_or_create_tool(conn: sqlite3.Connection, name: str, **kwargs) -> str:
    """Get or create tool, return id (ULID)."""
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


def get_or_create_tool_by_alias(conn: sqlite3.Connection, raw_name: str, harness_id: str) -> str:
    """Look up tool by alias for this harness, or create with raw name as canonical."""
    # Check alias first (harness-specific)
    cur = conn.execute(
        "SELECT tool_id FROM tool_aliases WHERE raw_name = ? AND harness_id = ?",
        (raw_name, harness_id)
    )
    row = cur.fetchone()
    if row:
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


def ensure_canonical_tools(conn: sqlite3.Connection) -> None:
    """Insert all canonical tools if not already present. Idempotent."""
    for tool in CANONICAL_TOOLS:
        conn.execute(
            "INSERT OR IGNORE INTO tools (id, name, category, description) VALUES (?, ?, ?, ?)",
            (_ulid(), tool["name"], tool["category"], tool["description"]),
        )
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
    ended_at: str | None = None,
) -> str:
    """Insert conversation, return id (ULID)."""
    ulid = _ulid()
    conn.execute(
        """INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at, ended_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ulid, external_id, harness_id, workspace_id, started_at, ended_at)
    )
    return ulid


def insert_prompt(
    conn: sqlite3.Connection,
    conversation_id: str,
    external_id: str | None,
    timestamp: str,
) -> str:
    """Insert prompt, return id (ULID)."""
    ulid = _ulid()
    conn.execute(
        "INSERT INTO prompts (id, conversation_id, external_id, timestamp) VALUES (?, ?, ?, ?)",
        (ulid, conversation_id, external_id, timestamp)
    )
    return ulid


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
    """Insert response, return id (ULID)."""
    ulid = _ulid()
    conn.execute(
        """INSERT INTO responses
           (id, conversation_id, prompt_id, model_id, provider_id, external_id, timestamp, input_tokens, output_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ulid, conversation_id, prompt_id, model_id, provider_id, external_id, timestamp, input_tokens, output_tokens)
    )
    return ulid


def insert_prompt_content(
    conn: sqlite3.Connection,
    prompt_id: str,
    block_index: int,
    block_type: str,
    content: str,
) -> str:
    """Insert prompt content block, return id (ULID)."""
    ulid = _ulid()
    conn.execute(
        "INSERT INTO prompt_content (id, prompt_id, block_index, block_type, content) VALUES (?, ?, ?, ?, ?)",
        (ulid, prompt_id, block_index, block_type, content)
    )
    return ulid


def insert_response_content(
    conn: sqlite3.Connection,
    response_id: str,
    block_index: int,
    block_type: str,
    content: str,
) -> str:
    """Insert response content block, return id (ULID)."""
    ulid = _ulid()
    conn.execute(
        "INSERT INTO response_content (id, response_id, block_index, block_type, content) VALUES (?, ?, ?, ?, ?)",
        (ulid, response_id, block_index, block_type, content)
    )
    return ulid


def insert_response_attribute(
    conn: sqlite3.Connection,
    response_id: str,
    key: str,
    value: str,
    scope: str | None = None,
) -> str:
    """Insert a response attribute, return id (ULID). Upserts on conflict."""
    ulid = _ulid()
    conn.execute(
        """INSERT INTO response_attributes (id, response_id, key, value, scope)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (response_id, key, scope) DO UPDATE SET value = excluded.value""",
        (ulid, response_id, key, value, scope)
    )
    return ulid


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
) -> str:
    """Insert tool call, return id (ULID).

    Args:
        dedupe_result: If True (default), stores result in content_blobs for
            deduplication. If False, stores inline in result column.
    """
    from siftd.storage.blobs import store_content

    ulid = _ulid()
    result_hash = None

    if result_json is not None and dedupe_result:
        # Store in content_blobs and reference by hash
        result_hash = store_content(conn, result_json)
        conn.execute(
            """INSERT INTO tool_calls
               (id, response_id, conversation_id, tool_id, external_id, input, result_hash, status, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ulid, response_id, conversation_id, tool_id, external_id, input_json, result_hash, status, timestamp)
        )
    else:
        # Store inline (legacy behavior or dedupe disabled)
        conn.execute(
            """INSERT INTO tool_calls
               (id, response_id, conversation_id, tool_id, external_id, input, result, status, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ulid, response_id, conversation_id, tool_id, external_id, input_json, result_json, status, timestamp)
        )
    return ulid


# =============================================================================
# High-level storage functions
# =============================================================================


def store_conversation(conn: sqlite3.Connection, conversation: Conversation, *, commit: bool = False) -> str:
    """Store a complete Conversation domain object.

    Walks the nested tree and calls insert_* functions.
    Caller controls commit (default: no commit).
    """
    # Get or create harness
    harness_kwargs = {}
    if conversation.harness.source:
        harness_kwargs["source"] = conversation.harness.source
    if conversation.harness.log_format:
        harness_kwargs["log_format"] = conversation.harness.log_format
    if conversation.harness.display_name:
        harness_kwargs["display_name"] = conversation.harness.display_name

    harness_id = get_or_create_harness(conn, conversation.harness.name, **harness_kwargs)

    # Get or create provider (derived from harness source)
    provider_id = None
    if conversation.harness.source:
        provider_id = get_or_create_provider(conn, conversation.harness.source)

    # Get or create workspace
    workspace_id = None
    if conversation.workspace_path:
        workspace_id = get_or_create_workspace(
            conn, conversation.workspace_path, conversation.started_at
        )

    # Create conversation
    conversation_id = insert_conversation(
        conn,
        external_id=conversation.external_id,
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at=conversation.started_at,
        ended_at=conversation.ended_at,
    )

    # Process prompts
    for prompt in conversation.prompts:
        prompt_id = insert_prompt(
            conn,
            conversation_id=conversation_id,
            external_id=prompt.external_id,
            timestamp=prompt.timestamp,
        )

        # Insert prompt content blocks
        for idx, block in enumerate(prompt.content):
            content_id = insert_prompt_content(
                conn, prompt_id, idx, block.block_type, json.dumps(block.content)
            )
            if block.block_type == "text" and block.content.get("text"):
                insert_fts_content(conn, content_id, "prompt", conversation_id, block.content["text"])

        # Process responses for this prompt
        for response in prompt.responses:
            # Get or create model if specified
            model_id = None
            if response.model:
                model_id = get_or_create_model(conn, response.model)

            # Extract usage
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
                content_id = insert_response_content(
                    conn, response_id, idx, block.block_type, json.dumps(block.content)
                )
                if block.block_type == "text" and block.content.get("text"):
                    insert_fts_content(conn, content_id, "response", conversation_id, block.content["text"])

            # Insert response attributes
            for attr_key, attr_value in response.attributes.items():
                insert_response_attribute(
                    conn, response_id, attr_key, attr_value, scope="provider"
                )

            # Insert tool calls
            for tool_call in response.tool_calls:
                tool_id = get_or_create_tool_by_alias(
                    conn, tool_call.tool_name, harness_id
                )
                tool_call_id = insert_tool_call(
                    conn,
                    response_id=response_id,
                    conversation_id=conversation_id,
                    tool_id=tool_id,
                    external_id=tool_call.external_id,
                    input_json=json.dumps(tool_call.input),
                    result_json=json.dumps(tool_call.result) if tool_call.result else None,
                    status=tool_call.status,
                    timestamp=tool_call.timestamp,
                )

                # Auto-tag shell commands at ingest time
                canonical_name = conn.execute(
                    "SELECT name FROM tools WHERE id = ?", (tool_id,)
                ).fetchone()["name"]
                tag_shell_command(conn, tool_call_id, canonical_name, tool_call.input)

                # Auto-tag derivative conversations (contain siftd ask/query)
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

    Uses ON DELETE CASCADE to automatically remove child records:
    prompts, responses, tool_calls, content blocks, attributes, tags,
    and ingested_files.

    Note: FTS index (content_fts) must be cleaned up manually since
    it's a virtual table without FK support.
    """
    # Clean up FTS index entries (virtual tables don't support CASCADE)
    conn.execute(
        "DELETE FROM content_fts WHERE conversation_id = ?", (conversation_id,)
    )

    # Delete conversation - CASCADE handles all child tables
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# =============================================================================
# File deduplication functions
# =============================================================================


def compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def check_file_ingested(conn: sqlite3.Connection, path: str) -> bool:
    """Check if a file has already been ingested."""
    cur = conn.execute("SELECT 1 FROM ingested_files WHERE path = ?", (path,))
    return cur.fetchone() is not None


def get_ingested_file_info(conn: sqlite3.Connection, path: str) -> dict | None:
    """Get stored info for an ingested file.

    Returns dict with {file_hash, conversation_id, error} or None if not found.
    """
    cur = conn.execute(
        "SELECT file_hash, conversation_id, error FROM ingested_files WHERE path = ?",
        (path,)
    )
    row = cur.fetchone()
    if row:
        return {
            "file_hash": row["file_hash"],
            "conversation_id": row["conversation_id"],
            "error": row["error"],
        }
    return None


def record_ingested_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    conversation_id: str,
    *,
    commit: bool = False,
) -> str:
    """Record that a file has been ingested. Returns the record id.

    Derives harness_id from the conversation record.
    Caller controls commit (default: no commit).
    """
    from datetime import datetime

    # Look up harness_id from conversation
    row = conn.execute(
        "SELECT harness_id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Conversation not found: {conversation_id}")
    harness_id = row[0]

    ulid = _ulid()
    ingested_at = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ulid, path, file_hash, harness_id, conversation_id, ingested_at)
    )
    if commit:
        conn.commit()
    return ulid


def record_empty_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    harness_id: str,
    *,
    commit: bool = False,
) -> str:
    """Record an empty file (no conversation). Returns the record id.

    Used for files that parse to zero conversations (e.g., empty JSONL files).
    Stores with conversation_id=NULL so they're tracked but can be re-ingested
    if content appears later.
    """
    from datetime import datetime

    ulid = _ulid()
    ingested_at = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at)
           VALUES (?, ?, ?, ?, NULL, ?)""",
        (ulid, path, file_hash, harness_id, ingested_at)
    )
    if commit:
        conn.commit()
    return ulid


def record_failed_file(
    conn: sqlite3.Connection,
    path: str,
    file_hash: str,
    harness_id: str,
    error: str,
    *,
    commit: bool = False,
) -> str:
    """Record a file that failed ingestion. Returns the record id.

    Stores with conversation_id=NULL and error message so the file is tracked
    and won't retry unless its hash changes.
    """
    from datetime import datetime

    ulid = _ulid()
    ingested_at = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at, error)
           VALUES (?, ?, ?, ?, NULL, ?, ?)""",
        (ulid, path, file_hash, harness_id, ingested_at, error)
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
