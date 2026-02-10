"""Tests for siftd db merge — importing a slice into the main database."""

import sqlite3

import pytest

from siftd.api.merge import merge_database
from siftd.cli import main
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
    insert_tool_call,
)
from siftd.storage.tags import apply_tag, get_or_create_tag


def _make_db(path, *, harness_name="test_harness", workspace_path="/test/project",
             workspace_git_remote=None, model_name="test-model",
             conversations=None):
    """Helper to create a database with optional conversations.

    conversations: list of dicts with keys:
        external_id, prompt_text, response_text, started_at (optional),
        tags (optional list of tag names), tool_name (optional)
    """
    conn = create_database(path)

    harness_id = get_or_create_harness(conn, harness_name, source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, workspace_path, "2024-01-01T10:00:00Z")

    # Set git_remote if provided
    if workspace_git_remote:
        conn.execute(
            "UPDATE workspaces SET git_remote = ? WHERE id = ?",
            (workspace_git_remote, workspace_id),
        )

    model_id = get_or_create_model(conn, model_name)
    provider_id = get_or_create_provider(conn, "test_provider")

    for conv in (conversations or []):
        started = conv.get("started_at", "2024-01-15T10:00:00Z")
        conv_id = insert_conversation(
            conn,
            external_id=conv["external_id"],
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at=started,
        )
        prompt_id = insert_prompt(conn, conv_id, f"p-{conv['external_id']}", started)
        insert_prompt_content(
            conn, prompt_id, 0, "text",
            f'{{"text": "{conv.get("prompt_text", "Hello")}"}}',
        )
        response_id = insert_response(
            conn, conv_id, prompt_id, model_id, provider_id,
            f"r-{conv['external_id']}", started,
            input_tokens=100, output_tokens=50,
        )
        insert_response_content(
            conn, response_id, 0, "text",
            f'{{"text": "{conv.get("response_text", "Hi there")}"}}',
        )

        if conv.get("tool_name"):
            tool_id = get_or_create_tool(conn, conv["tool_name"])
            insert_tool_call(
                conn, response_id, conv_id, tool_id, f"tc-{conv['external_id']}",
                '{"cmd": "test"}', '{"output": "ok"}', "success", started,
            )

        for tag_name in conv.get("tags", []):
            tag_id = get_or_create_tag(conn, tag_name)
            apply_tag(conn, "conversation", conv_id, tag_id)

    conn.commit()
    conn.close()
    return path


def test_disjoint_merge(tmp_path):
    """All source conversations appear in target when no overlap."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "prompt_text": "Target question"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "prompt_text": "Source question"}],
    )

    result = merge_database(target, source)

    assert result["conversations"] == 1
    assert result["skipped_conversations"] == 0

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    conn.close()
    assert count == 2


def test_duplicate_skip(tmp_path):
    """Same (harness, external_id) is skipped, child rows not copied."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Duplicate"}],
    )

    result = merge_database(target, source)

    assert result["conversations"] == 0
    assert result["skipped_conversations"] == 1

    # Child rows should not be doubled
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    conn.close()
    assert prompts == 1


def test_vocabulary_remapping(tmp_path):
    """Same harness name with different ULIDs → conversations use target's ULID."""
    target = _make_db(
        tmp_path / "target.db",
        harness_name="claude_code",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        harness_name="claude_code",
        conversations=[{"external_id": "conv-B"}],
    )

    result = merge_database(target, source)
    assert result["conversations"] == 1

    # Both conversations should reference the same harness_id (target's)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    harness_ids = conn.execute(
        "SELECT DISTINCT harness_id FROM conversations"
    ).fetchall()
    conn.close()
    assert len(harness_ids) == 1


def test_workspace_git_remote_match(tmp_path):
    """Different paths, same git_remote → merged to target workspace."""
    target = _make_db(
        tmp_path / "target.db",
        workspace_path="/home/user/project",
        workspace_git_remote="git@github.com:user/repo.git",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        workspace_path="/Users/other/project",
        workspace_git_remote="git@github.com:user/repo.git",
        conversations=[{"external_id": "conv-B"}],
    )

    result = merge_database(target, source)
    assert result["conversations"] == 1
    assert result["workspaces_matched"] == 1

    # Both conversations should reference the same workspace
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    workspace_ids = conn.execute(
        "SELECT DISTINCT workspace_id FROM conversations"
    ).fetchall()
    workspaces = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    conn.close()
    assert len(workspace_ids) == 1
    assert workspaces == 1


def test_tag_name_dedup(tmp_path):
    """Same tag name, different ULIDs → junction tables use target's tag ULID."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "tags": ["research"]}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "tags": ["research"]}],
    )

    result = merge_database(target, source)
    assert result["conversations"] == 1
    assert result["tags"] == 0  # no new tag definitions

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    junctions = conn.execute("SELECT COUNT(*) FROM conversation_tags").fetchone()[0]
    conn.close()
    assert tags == 1  # one "research" tag, not two
    assert junctions == 2  # both conversations tagged


def test_content_blobs_dedup_and_ref_count(tmp_path):
    """Same SHA256 in both DBs → one copy, correct ref_count."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "tool_name": "shell.execute"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "tool_name": "shell.execute"}],
    )

    result = merge_database(target, source)
    assert result["conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    assert tool_calls == 2

    # Verify ref_counts are correct
    for row in conn.execute("SELECT hash, ref_count FROM content_blobs").fetchall():
        actual_refs = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE result_hash = ?", (row["hash"],)
        ).fetchone()[0]
        assert row["ref_count"] == actual_refs, f"hash {row['hash']}: ref_count={row['ref_count']} but actual={actual_refs}"
    conn.close()


def test_fk_integrity(tmp_path):
    """PRAGMA foreign_key_check passes after merge."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "tool_name": "shell.execute", "tags": ["test"]}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "tool_name": "file.read", "tags": ["review"]}],
    )

    merge_database(target, source)

    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_fts_rebuild(tmp_path):
    """Search finds content from both DBs after merge."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "prompt_text": "Python decorators"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "prompt_text": "Rust lifetimes"}],
    )

    merge_database(target, source, rebuild_fts=True)

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    python_hits = conn.execute(
        "SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH 'Python'"
    ).fetchone()[0]
    rust_hits = conn.execute(
        "SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH 'Rust'"
    ).fetchone()[0]
    conn.close()
    assert python_hits >= 1
    assert rust_hits >= 1


def test_dry_run(tmp_path):
    """dry_run returns counts but leaves target unchanged."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B"}, {"external_id": "conv-C"}],
    )

    result = merge_database(target, source, dry_run=True)
    assert result["conversations"] == 2

    # Target should be unchanged
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    conn.close()
    assert count == 1


def test_idempotent(tmp_path):
    """Merging same source twice → second merge has 0 new conversations."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B"}],
    )

    result1 = merge_database(target, source)
    assert result1["conversations"] == 1

    result2 = merge_database(target, source)
    assert result2["conversations"] == 0
    assert result2["skipped_conversations"] == 1


def test_empty_source(tmp_path):
    """Merging empty source succeeds with all counts 0."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(tmp_path / "source.db", conversations=[])

    result = merge_database(target, source)
    assert result["conversations"] == 0
    assert result["skipped_conversations"] == 0
    assert result["prompts"] == 0
    assert result["responses"] == 0
    assert result["tool_calls"] == 0


def test_cli_integration(tmp_path, capsys):
    """siftd db merge via CLI returns 0."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B"}],
    )

    rc = main(["--db", str(target), "db", "merge", str(source)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Merged from:" in out
    assert "1 new" in out


def test_cli_dry_run(tmp_path, capsys):
    """siftd db merge --dry-run shows prefix and leaves DB unchanged."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B"}],
    )

    rc = main(["--db", str(target), "db", "merge", str(source), "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "[dry run]" in out

    conn = sqlite3.connect(str(target))
    count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    conn.close()
    assert count == 1


def test_duplicate_children_not_orphaned(tmp_path):
    """When a conversation is skipped (duplicate), its children don't create orphans.

    Source has same (harness, external_id) but different ULIDs for prompts/responses.
    INSERT OR IGNORE on child tables should skip them, not create FK violations.
    """
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original prompt"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Different prompt"}],
    )

    result = merge_database(target, source)
    assert result["conversations"] == 0
    assert result["skipped_conversations"] == 1

    # No orphaned children — FK check passes
    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()

    # Prompt count should be unchanged (1 from original)
    prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    responses = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    conn.close()

    assert violations == []
    assert prompts == 1
    assert responses == 1


def test_workspace_tag_remapping(tmp_path):
    """Workspace tags are remapped correctly during merge."""
    from siftd.storage.sqlite import open_database as _open

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B"}],
    )

    # Add a workspace tag to the source
    src_conn = _open(source)
    src_ws_id = src_conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()["id"]
    src_tag_id = get_or_create_tag(src_conn, "infra")
    apply_tag(src_conn, "workspace", src_ws_id, src_tag_id)
    src_conn.commit()
    src_conn.close()

    result = merge_database(target, source)
    assert result["conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    wt_count = conn.execute("SELECT COUNT(*) FROM workspace_tags").fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    assert wt_count >= 1
    assert violations == []


def test_tool_call_tag_remapping(tmp_path):
    """Tool call tags are remapped correctly during merge."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "tool_name": "shell.execute"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "tool_name": "shell.execute"}],
    )

    # Add a tool_call tag to the source
    from siftd.storage.sqlite import open_database as _open

    src_conn = _open(source)
    tc_id = src_conn.execute("SELECT id FROM tool_calls LIMIT 1").fetchone()["id"]
    tag_id = get_or_create_tag(src_conn, "shell:test")
    apply_tag(src_conn, "tool_call", tc_id, tag_id)
    src_conn.commit()
    src_conn.close()

    result = merge_database(target, source)
    assert result["conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    tct_count = conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    assert tct_count >= 1
    assert violations == []


def test_cli_invalid_file(tmp_path, capsys):
    """siftd db merge rejects non-SQLite files."""
    target = _make_db(tmp_path / "target.db", conversations=[])
    bad_source = tmp_path / "not-sqlite.db"
    bad_source.write_text("this is not a database")

    rc = main(["--db", str(target), "db", "merge", str(bad_source)])
    assert rc == 1
    assert "Not a valid SQLite" in capsys.readouterr().err
