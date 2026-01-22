"""SQLite storage adapter for tbd-v2."""

import hashlib
import json
import sqlite3
import time
import os
from pathlib import Path

from domain import Conversation, ContentBlock

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

    return conn


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
    """Get or create model, return id (ULID)."""
    cur = conn.execute("SELECT id FROM models WHERE raw_name = ?", (raw_name,))
    row = cur.fetchone()
    if row:
        return row["id"]

    ulid = _ulid()
    name = kwargs.pop("name", raw_name)
    cols = ["id", "raw_name", "name"] + list(kwargs.keys())
    vals = [ulid, raw_name, name] + list(kwargs.values())
    placeholders = ", ".join("?" * len(vals))
    col_names = ", ".join(cols)
    conn.execute(f"INSERT INTO models ({col_names}) VALUES ({placeholders})", vals)
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
            insert_prompt_content(
                conn, prompt_id, idx, block.block_type, json.dumps(block.content)
            )

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
                provider_id=None,  # TODO: handle provider
                external_id=response.external_id,
                timestamp=response.timestamp,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            # Insert response content blocks
            for idx, block in enumerate(response.content):
                insert_response_content(
                    conn, response_id, idx, block.block_type, json.dumps(block.content)
                )

            # Insert tool calls
            for tool_call in response.tool_calls:
                tool_id = get_or_create_tool_by_alias(
                    conn, tool_call.tool_name, harness_id
                )
                insert_tool_call(
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
    attributes, labels, ingested_files.
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

    # Conversation attributes and labels
    conn.execute("DELETE FROM conversation_attributes WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM conversation_labels WHERE conversation_id = ?", (conversation_id,))

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
