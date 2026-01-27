"""SQLite storage adapter for strata."""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from strata.domain import Conversation
from strata.models import parse_model_name

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# ULID generation (inline, no dependency)
_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODING_LEN = len(_ENCODING)


def _ulid() -> str:
    """Generate a ULID (Universally Unique Lexicographically Sortable Identifier).

    Format: 10 chars timestamp (48 bits, ms precision) + 16 chars randomness (80 bits)
    Total: 26 chars, sortable by creation time, no collisions in practice.
    """
    # Timestamp: milliseconds since Unix epoch
    timestamp_ms = int(time.time() * 1000)

    # Encode timestamp (10 chars)
    ts_chars = []
    for _ in range(10):
        ts_chars.append(_ENCODING[timestamp_ms % _ENCODING_LEN])
        timestamp_ms //= _ENCODING_LEN
    ts_part = "".join(reversed(ts_chars))

    # Random part (16 chars, 80 bits)
    rand_bytes = os.urandom(10)
    rand_int = int.from_bytes(rand_bytes, "big")
    rand_chars = []
    for _ in range(16):
        rand_chars.append(_ENCODING[rand_int % _ENCODING_LEN])
        rand_int //= _ENCODING_LEN
    rand_part = "".join(reversed(rand_chars))

    return ts_part + rand_part


def open_database(db_path: Path) -> sqlite3.Connection:
    """Open database connection, creating schema if needed."""
    is_new = not db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if is_new:
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)
        conn.commit()

    _migrate_labels_to_tags(conn)
    _migrate_add_error_column(conn)
    ensure_fts_table(conn)
    ensure_pricing_table(conn)
    ensure_canonical_tools(conn)
    ensure_tool_call_tags_table(conn)
    return conn


def _migrate_labels_to_tags(conn: sqlite3.Connection) -> None:
    """Migrate old label tables to tag tables if they exist.

    Renames: labels → tags, conversation_labels → conversation_tags,
    workspace_labels → workspace_tags, and updates column names.
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


# Alias for backwards compatibility
create_database = open_database


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

    aliases: dict of raw_name → canonical_name
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
) -> str:
    """Insert tool call, return id (ULID)."""
    ulid = _ulid()
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

    Cascades to: prompts, responses, tool_calls, content blocks,
    attributes, tags, ingested_files.
    """
    # Delete in order to respect foreign keys (or rely on CASCADE if defined)
    # Content and attributes first
    conn.execute("""
        DELETE FROM prompt_content
        WHERE prompt_id IN (SELECT id FROM prompts WHERE conversation_id = ?)
    """, (conversation_id,))
    conn.execute("""
        DELETE FROM response_content
        WHERE response_id IN (SELECT id FROM responses WHERE conversation_id = ?)
    """, (conversation_id,))
    conn.execute("""
        DELETE FROM prompt_attributes
        WHERE prompt_id IN (SELECT id FROM prompts WHERE conversation_id = ?)
    """, (conversation_id,))
    conn.execute("""
        DELETE FROM response_attributes
        WHERE response_id IN (SELECT id FROM responses WHERE conversation_id = ?)
    """, (conversation_id,))
    conn.execute("""
        DELETE FROM tool_call_attributes
        WHERE tool_call_id IN (SELECT id FROM tool_calls WHERE conversation_id = ?)
    """, (conversation_id,))

    # Then tool_calls, responses, prompts
    conn.execute("DELETE FROM tool_calls WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM responses WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM prompts WHERE conversation_id = ?", (conversation_id,))

    # Conversation attributes and tags
    conn.execute("DELETE FROM conversation_attributes WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM conversation_tags WHERE conversation_id = ?", (conversation_id,))

    # Ingested files pointing to this conversation
    conn.execute("DELETE FROM ingested_files WHERE conversation_id = ?", (conversation_id,))

    # Finally the conversation itself
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


# =============================================================================
# FTS5 Full-Text Search
# =============================================================================


def ensure_pricing_table(conn: sqlite3.Connection) -> None:
    """Create the pricing table if it doesn't exist. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing (
            id              TEXT PRIMARY KEY,
            model_id        TEXT NOT NULL REFERENCES models(id),
            provider_id     TEXT NOT NULL REFERENCES providers(id),
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
            tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id),
            tag_id          TEXT NOT NULL REFERENCES tags(id),
            applied_at      TEXT NOT NULL,
            UNIQUE (tool_call_id, tag_id)
        )
    """)


def ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create the FTS5 virtual table if it doesn't exist. Idempotent."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
            text_content,
            content_id UNINDEXED,
            side UNINDEXED,
            conversation_id UNINDEXED
        )
    """)


def rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """Drop and rebuild the FTS index from all text content blocks.

    Reads prompt_content and response_content where block_type='text',
    extracts the text from JSON content, and populates content_fts.
    """
    conn.execute("DELETE FROM content_fts")

    # Index prompt text blocks
    conn.execute("""
        INSERT INTO content_fts (text_content, content_id, side, conversation_id)
        SELECT
            json_extract(pc.content, '$.text'),
            pc.id,
            'prompt',
            p.conversation_id
        FROM prompt_content pc
        JOIN prompts p ON p.id = pc.prompt_id
        WHERE pc.block_type = 'text'
          AND json_extract(pc.content, '$.text') IS NOT NULL
    """)

    # Index response text blocks
    conn.execute("""
        INSERT INTO content_fts (text_content, content_id, side, conversation_id)
        SELECT
            json_extract(rc.content, '$.text'),
            rc.id,
            'response',
            r.conversation_id
        FROM response_content rc
        JOIN responses r ON r.id = rc.response_id
        WHERE rc.block_type = 'text'
          AND json_extract(rc.content, '$.text') IS NOT NULL
    """)

    conn.commit()


def insert_fts_content(
    conn: sqlite3.Connection,
    content_id: str,
    side: str,
    conversation_id: str,
    text: str,
) -> None:
    """Insert a single text entry into the FTS index."""
    conn.execute(
        "INSERT INTO content_fts (text_content, content_id, side, conversation_id) VALUES (?, ?, ?, ?)",
        (text, content_id, side, conversation_id),
    )


def backfill_models(conn: sqlite3.Connection) -> int:
    """Backfill parsed fields for existing model rows with NULL fields.

    Updates rows where creator/family/version/variant are NULL.
    Returns count of rows updated.
    """
    cur = conn.execute(
        "SELECT id, raw_name FROM models WHERE creator IS NULL OR family IS NULL"
    )
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        parsed = parse_model_name(row["raw_name"])
        # Skip if parsing produced no useful info (fallback case)
        if parsed["creator"] is None:
            continue
        conn.execute(
            """UPDATE models
               SET name = ?, creator = ?, family = ?, version = ?, variant = ?, released = ?
               WHERE id = ?""",
            (parsed["name"], parsed["creator"], parsed["family"],
             parsed["version"], parsed["variant"], parsed["released"], row["id"]),
        )
        updated += 1
    conn.commit()
    return updated


def backfill_providers(conn: sqlite3.Connection) -> int:
    """Backfill provider_id on responses where it's NULL.

    Derives provider from the conversation's harness source field.
    Returns count of rows updated.
    """
    # Get harness name → source mapping
    cur = conn.execute("SELECT id, name, source FROM harnesses WHERE source IS NOT NULL")
    harness_rows = cur.fetchall()
    if not harness_rows:
        return 0

    updated = 0
    for harness_row in harness_rows:
        harness_id = harness_row["id"]
        source = harness_row["source"]
        provider_id = get_or_create_provider(conn, source)

        # Update responses that belong to conversations from this harness
        cur = conn.execute("""
            UPDATE responses SET provider_id = ?
            WHERE provider_id IS NULL
              AND conversation_id IN (
                  SELECT id FROM conversations WHERE harness_id = ?
              )
        """, (provider_id, harness_id))
        updated += cur.rowcount

    conn.commit()
    return updated


# =============================================================================
# Tag functions
# =============================================================================


def get_or_create_tag(conn: sqlite3.Connection, name: str, description: str | None = None) -> str:
    """Get or create a tag by name, return id (ULID)."""
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row["id"]

    from datetime import datetime
    ulid = _ulid()
    conn.execute(
        "INSERT INTO tags (id, name, description, created_at) VALUES (?, ?, ?, ?)",
        (ulid, name, description, datetime.now().isoformat())
    )
    return ulid


def apply_tag(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> str | None:
    """Apply a tag to an entity. Returns assignment id or None if already applied.

    entity_type: 'conversation', 'workspace', or 'tool_call'
    """
    from datetime import datetime

    if entity_type == "conversation":
        table = "conversation_tags"
        fk_col = "conversation_id"
    elif entity_type == "workspace":
        table = "workspace_tags"
        fk_col = "workspace_id"
    elif entity_type == "tool_call":
        table = "tool_call_tags"
        fk_col = "tool_call_id"
    else:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    # Check if already applied
    cur = conn.execute(
        f"SELECT id FROM {table} WHERE {fk_col} = ? AND tag_id = ?",
        (entity_id, tag_id)
    )
    if cur.fetchone():
        return None

    ulid = _ulid()
    conn.execute(
        f"INSERT INTO {table} (id, {fk_col}, tag_id, applied_at) VALUES (?, ?, ?, ?)",
        (ulid, entity_id, tag_id, datetime.now().isoformat())
    )
    if commit:
        conn.commit()
    return ulid


def remove_tag(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Remove a tag from an entity. Returns True if a row was deleted, False if not applied.

    entity_type: 'conversation', 'workspace', or 'tool_call'
    """
    if entity_type == "conversation":
        table = "conversation_tags"
        fk_col = "conversation_id"
    elif entity_type == "workspace":
        table = "workspace_tags"
        fk_col = "workspace_id"
    elif entity_type == "tool_call":
        table = "tool_call_tags"
        fk_col = "tool_call_id"
    else:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    cur = conn.execute(
        f"DELETE FROM {table} WHERE {fk_col} = ? AND tag_id = ?",
        (entity_id, tag_id)
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def rename_tag(conn: sqlite3.Connection, old_name: str, new_name: str, *, commit: bool = False) -> bool:
    """Rename a tag. Returns True if renamed, False if old_name not found.

    Raises ValueError if new_name already exists.
    """
    # Check new_name doesn't already exist
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (new_name,))
    if cur.fetchone():
        raise ValueError(f"Tag '{new_name}' already exists")

    cur = conn.execute("UPDATE tags SET name = ? WHERE name = ?", (new_name, old_name))
    if commit:
        conn.commit()
    return cur.rowcount > 0


def delete_tag(conn: sqlite3.Connection, name: str, *, commit: bool = False) -> int:
    """Delete a tag and all its associations. Returns count of entity associations removed."""
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if not row:
        return -1  # tag not found

    tag_id = row["id"]

    # Count and delete associations
    removed = 0
    for table in ("conversation_tags", "workspace_tags", "tool_call_tags"):
        cur = conn.execute(f"DELETE FROM {table} WHERE tag_id = ?", (tag_id,))
        removed += cur.rowcount

    # Delete the tag itself
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    if commit:
        conn.commit()
    return removed



def list_tags(conn: sqlite3.Connection) -> list[dict]:
    """List all tags with usage counts."""
    cur = conn.execute("""
        SELECT
            t.name,
            t.description,
            t.created_at,
            (SELECT COUNT(*) FROM conversation_tags ct WHERE ct.tag_id = t.id) as conversation_count,
            (SELECT COUNT(*) FROM workspace_tags wt WHERE wt.tag_id = t.id) as workspace_count,
            (SELECT COUNT(*) FROM tool_call_tags tt WHERE tt.tag_id = t.id) as tool_call_count
        FROM tags t
        ORDER BY t.name
    """)
    return [
        {
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
            "conversation_count": row["conversation_count"],
            "workspace_count": row["workspace_count"],
            "tool_call_count": row["tool_call_count"],
        }
        for row in cur.fetchall()
    ]


# =============================================================================
# Shell command categorization
# =============================================================================

# Namespace prefix for auto-generated shell tags
SHELL_TAG_PREFIX = "shell:"

# Categories and their identifying commands/patterns
SHELL_CATEGORIES = {
    "test": {
        "keywords": ["pytest", "jest", "vitest", "mocha"],
        "patterns": [r"\bcargo\s+test\b", r"\bgo\s+test\b", r"\bnpm\s+test\b"],
    },
    "lint": {
        "commands": ["ruff", "eslint", "mypy", "pylint", "flake8", "black", "isort"],
        "patterns": [r"\buv\s+run\s+ty\b", r"\buv\s+run\s+ruff\b"],
    },
    "vcs": {
        "commands": ["git", "yadm", "gh"],
    },
    "search": {
        "commands": ["grep", "rg", "find", "ag"],
        "pipe_commands": ["grep", "rg"],
    },
    "file": {
        "commands": ["ls", "cat", "head", "tail", "mv", "cp", "rm", "mkdir", "tree", "wc", "nl", "touch", "chmod", "chown", "ln", "sed", "awk"],
        "pipe_commands": ["head", "tail", "wc", "nl", "sed", "awk"],
    },
    "remote": {
        "commands": ["ssh", "scp", "rsync", "curl", "wget", "ping", "dig", "nc", "netstat"],
    },
    "db": {
        "commands": ["sqlite3", "sqlite-utils", "psql", "mysql"],
        "pipe_commands": ["sqlite3"],
    },
    "infra": {
        "commands": ["docker", "terraform", "ansible", "kubectl", "k9s", "helm"],
    },
    "ai": {
        "commands": ["claude", "gemini", "aider", "codex"],
    },
    "python": {
        "commands": ["python", "python3"],
        "patterns": [r"\buv\s+run\s+python"],
    },
    "node": {
        "commands": ["npm", "node", "yarn", "pnpm", "npx", "bun"],
    },
    "package": {
        "commands": ["pip", "brew", "apt", "cargo"],
        "patterns": [r"^uv\s+(?!run)"],  # uv but not uv run
    },
    "shell": {
        "commands": ["echo", "sleep", "source", ".", "date", "which", "pwd", "env", "export",
                     "bash", "zsh", "sh", "tmux", "screen", "open", "pbcopy", "pbpaste",
                     "for", "while", "if", "case", "test", "["],
    },
}


def categorize_shell_command(cmd: str) -> str | None:
    """Categorize a shell command string into a category.

    Returns the category name (without prefix) or None if uncategorized.
    """
    import re

    if not cmd:
        return None

    # Normalize: strip leading "cd <path> && " pattern
    cmd_norm = re.sub(r"^cd\s+[^&]+&&\s*", "", cmd).strip()
    parts = cmd_norm.split()
    first_word = parts[0] if parts else ""

    # Check each category in order of specificity
    # Test/lint first (they often use other tools like uv run)
    for category in ["test", "lint"]:
        spec = SHELL_CATEGORIES[category]

        # Check keywords anywhere in command
        if "keywords" in spec:
            for kw in spec["keywords"]:
                if kw in cmd:
                    return category

        # Check regex patterns
        if "patterns" in spec:
            for pattern in spec["patterns"]:
                if re.search(pattern, cmd):
                    return category

        # Check first-word commands
        if "commands" in spec and first_word in spec["commands"]:
            return category

    # Check remaining categories
    for category, spec in SHELL_CATEGORIES.items():
        if category in ("test", "lint"):
            continue  # Already checked

        # Check first-word commands
        if "commands" in spec and first_word in spec["commands"]:
            return category

        # Check pipe commands (| cmd)
        if "pipe_commands" in spec:
            for pipe_cmd in spec["pipe_commands"]:
                if re.search(rf"\|\s*{pipe_cmd}\b", cmd):
                    return category

        # Check regex patterns
        if "patterns" in spec:
            for pattern in spec["patterns"]:
                if re.search(pattern, cmd):
                    return category

    return None


def tag_shell_command(
    conn: sqlite3.Connection,
    tool_call_id: str,
    tool_name: str,
    input_data: dict | None,
) -> str | None:
    """Tag a shell.execute tool call with its category at ingest time.

    Args:
        conn: Database connection
        tool_call_id: The tool_call's ULID
        tool_name: Canonical tool name (e.g., "shell.execute")
        input_data: The tool call input dict

    Returns:
        The category name if tagged, None otherwise.
    """
    if tool_name != "shell.execute":
        return None

    if not input_data:
        return None

    # Extract command
    cmd = input_data.get("command") or input_data.get("cmd") or ""
    if not cmd:
        return None

    # Categorize
    category = categorize_shell_command(cmd)
    if not category:
        return None

    # Get or create tag and apply
    tag_name = f"{SHELL_TAG_PREFIX}{category}"
    tag_id = get_or_create_tag(conn, tag_name)
    apply_tag(conn, "tool_call", tool_call_id, tag_id)

    return category


def backfill_shell_tags(conn: sqlite3.Connection) -> dict[str, int]:
    """Backfill shell command tags for all shell.execute tool calls.

    Categorizes each shell.execute call and applies the appropriate shell:* tag.
    Skips tool calls that already have a shell:* tag.

    Returns dict of category -> count of newly tagged calls.
    """
    import json

    # Get shell.execute tool id
    cur = conn.execute("SELECT id FROM tools WHERE name = 'shell.execute'")
    row = cur.fetchone()
    if not row:
        return {}
    shell_tool_id = row["id"]

    # Find all shell.execute calls that don't already have a shell:* tag
    cur = conn.execute("""
        SELECT tc.id, tc.input
        FROM tool_calls tc
        WHERE tc.tool_id = ?
        AND tc.id NOT IN (
            SELECT tct.tool_call_id
            FROM tool_call_tags tct
            JOIN tags t ON t.id = tct.tag_id
            WHERE t.name LIKE 'shell:%'
        )
    """, (shell_tool_id,))

    # Cache for tag IDs
    tag_cache: dict[str, str] = {}
    counts: dict[str, int] = {}

    for row in cur.fetchall():
        tool_call_id = row["id"]
        raw_input = row["input"]

        # Extract command from JSON input
        try:
            data = json.loads(raw_input)
            cmd = data.get("command") or data.get("cmd") or ""
        except (json.JSONDecodeError, TypeError):
            cmd = raw_input or ""

        # Categorize
        category = categorize_shell_command(cmd)
        if not category:
            continue

        # Get or create tag
        tag_name = f"{SHELL_TAG_PREFIX}{category}"
        if tag_name not in tag_cache:
            tag_cache[tag_name] = get_or_create_tag(conn, tag_name)

        # Apply tag
        result = apply_tag(conn, "tool_call", tool_call_id, tag_cache[tag_name])
        if result:
            counts[category] = counts.get(category, 0) + 1

    conn.commit()
    return counts


def backfill_response_attributes(conn: sqlite3.Connection) -> int:
    """Backfill cache token attributes by re-reading raw JSONL files.

    For each ingested claude_code file, re-parses the JSONL and extracts
    cache_creation_input_tokens / cache_read_input_tokens from message.usage,
    then stores them as response_attributes.

    Returns count of attributes inserted.
    """
    from pathlib import Path

    from strata.adapters import claude_code

    # Find all ingested claude_code files
    harness_row = conn.execute(
        "SELECT id FROM harnesses WHERE name = ?", (claude_code.NAME,)
    ).fetchone()
    if not harness_row:
        return 0
    harness_id = harness_row["id"]

    files = conn.execute(
        "SELECT path, conversation_id FROM ingested_files WHERE harness_id = ?",
        (harness_id,)
    ).fetchall()

    inserted = 0
    for file_row in files:
        file_path = Path(file_row["path"])
        conversation_id = file_row["conversation_id"]
        if not file_path.exists():
            continue

        # Re-read the raw JSONL to extract cache tokens
        records = claude_code._load_jsonl(file_path)

        # Match responses by external_id
        for record in records:
            if record.get("type") != "assistant":
                continue
            message_data = record.get("message") or {}
            usage_data = message_data.get("usage") or {}
            external_msg_id = record.get("uuid")
            if not external_msg_id:
                continue

            cache_creation = usage_data.get("cache_creation_input_tokens")
            cache_read = usage_data.get("cache_read_input_tokens")
            if not cache_creation and not cache_read:
                continue

            # Find the response in DB
            response_external_id = f"{claude_code.NAME}::{external_msg_id}"
            row = conn.execute(
                "SELECT id FROM responses WHERE conversation_id = ? AND external_id = ?",
                (conversation_id, response_external_id)
            ).fetchone()
            if not row:
                continue
            response_id = row["id"]

            if cache_creation:
                insert_response_attribute(
                    conn, response_id, "cache_creation_input_tokens",
                    str(cache_creation), scope="provider"
                )
                inserted += 1
            if cache_read:
                insert_response_attribute(
                    conn, response_id, "cache_read_input_tokens",
                    str(cache_read), scope="provider"
                )
                inserted += 1

    conn.commit()
    return inserted


def search_content(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search text content using FTS5 MATCH.

    Returns list of dicts with: conversation_id, side, snippet, rank.
    """
    cur = conn.execute(
        """
        SELECT
            conversation_id,
            side,
            snippet(content_fts, 0, '>>>', '<<<', '...', 64) as snippet,
            rank
        FROM content_fts
        WHERE content_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    )
    return [
        {
            "conversation_id": row["conversation_id"],
            "side": row["side"],
            "snippet": row["snippet"],
            "rank": row["rank"],
        }
        for row in cur.fetchall()
    ]


def _fts5_or_rewrite(query: str) -> str | None:
    """Split query into tokens, filter short ones, join with OR for broad recall."""
    import re
    tokens = re.findall(r"\w+", query)
    tokens = [t for t in tokens if len(t) >= 3]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def _fts5_conversation_ids(
    conn: sqlite3.Connection, fts_query: str, limit: int
) -> set[str]:
    """Run FTS5 MATCH and return distinct conversation IDs."""
    cur = conn.execute(
        """
        SELECT conversation_id FROM content_fts
        WHERE content_fts MATCH ?
        GROUP BY conversation_id
        ORDER BY MIN(rank)
        LIMIT ?
        """,
        (fts_query, limit),
    )
    return {row["conversation_id"] for row in cur.fetchall()}


def fts5_recall_conversations(
    conn: sqlite3.Connection, query: str, limit: int = 80
) -> tuple[set[str], str]:
    """FTS5 recall: try AND semantics first, fall back to OR for broader recall.

    Returns (conversation_ids, mode) where mode is "and", "or", or "none".
    """
    # Phase 1: implicit AND (raw query)
    try:
        ids = _fts5_conversation_ids(conn, query, limit)
        if len(ids) >= 10:
            return ids, "and"
    except Exception:
        pass  # malformed FTS query, fall through to OR rewrite

    # Phase 2: OR rewrite for broader recall
    or_query = _fts5_or_rewrite(query)
    if or_query:
        try:
            ids = _fts5_conversation_ids(conn, or_query, limit)
            if ids:
                return ids, "or"
        except Exception:
            pass

    return set(), "none"
