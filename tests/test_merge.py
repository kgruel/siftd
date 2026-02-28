"""Tests for siftd db merge — importing a slice into the main database."""

import sqlite3

import pytest

from conftest import make_db as _make_db

from siftd.api.merge import merge_database
from siftd.cli import main
from siftd.storage.tags import apply_tag, get_or_create_tag


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
    """Same (harness, external_id) with replace=False is skipped, child rows not copied."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Duplicate"}],
    )

    result = merge_database(target, source, replace=False)

    assert result["conversations"] == 0
    assert result["skipped_conversations"] == 1
    assert result["replaced_conversations"] == 0

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
    """When a conversation is skipped (no-replace), its children don't create orphans.

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

    result = merge_database(target, source, replace=False)
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


def test_replace_stale_conversation(tmp_path):
    """Source with newer ULID for same (harness, external_id) replaces target's version."""
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original v1"}],
    )

    # Small delay so source gets a later ULID
    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Updated v2"}],
    )

    result = merge_database(target, source)
    assert result["replaced_conversations"] == 1
    assert result["conversations"] == 1  # the replacement counts as new
    assert result["skipped_conversations"] == 0

    # Target should have the source's version
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    assert convs == 1  # not duplicated

    # Prompt text should be from source
    text = conn.execute(
        "SELECT content FROM prompt_content LIMIT 1"
    ).fetchone()[0]
    assert "Updated v2" in text

    # FK integrity
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_no_replace_keeps_original(tmp_path):
    """With replace=False, existing conversations are kept even when source is newer."""
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original v1"}],
    )

    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Updated v2"}],
    )

    result = merge_database(target, source, replace=False)
    assert result["replaced_conversations"] == 0
    assert result["conversations"] == 0
    assert result["skipped_conversations"] == 1

    # Target should still have original
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    text = conn.execute(
        "SELECT content FROM prompt_content LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert "Original v1" in text


def test_replace_cascades_children(tmp_path):
    """Replacing a conversation removes old children and brings in new ones."""
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{
            "external_id": "conv-1",
            "prompt_text": "Old prompt",
            "tool_name": "shell.execute",
            "tags": ["review"],
        }],
    )

    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{
            "external_id": "conv-1",
            "prompt_text": "New prompt",
            "tool_name": "file.read",
            "tags": ["research"],
        }],
    )

    result = merge_database(target, source)
    assert result["replaced_conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row

    # Should have exactly 1 of each — old children deleted, new ones inserted
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1

    # Tool should be the source's
    tool_name = conn.execute("""
        SELECT t.name FROM tool_calls tc
        JOIN tools t ON t.id = tc.tool_id
    """).fetchone()[0]
    assert tool_name == "file.read"

    # Tag should be from source
    tag_name = conn.execute("""
        SELECT t.name FROM conversation_tags ct
        JOIN tags t ON t.id = ct.tag_id
    """).fetchone()[0]
    assert tag_name == "research"

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_replace_cascades_grandchildren(tmp_path):
    """Replacing a conversation removes grandchild rows: content, attributes, tags, ingested_files."""
    import time

    from siftd.ids import ulid as _ulid
    from siftd.storage.sqlite import open_database as _open

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{
            "external_id": "conv-1",
            "prompt_text": "Old prompt",
            "tool_name": "shell.execute",
            "tags": ["review"],
        }],
    )

    # Seed grandchild rows in target: prompt_attributes, response_attributes,
    # tool_call_attributes, tool_call_tags, ingested_files
    tgt_conn = _open(target)
    prompt_id = tgt_conn.execute("SELECT id FROM prompts LIMIT 1").fetchone()["id"]
    response_id = tgt_conn.execute("SELECT id FROM responses LIMIT 1").fetchone()["id"]
    tc_id = tgt_conn.execute("SELECT id FROM tool_calls LIMIT 1").fetchone()["id"]
    conv_id = tgt_conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    harness_id = tgt_conn.execute("SELECT harness_id FROM conversations LIMIT 1").fetchone()[0]

    tgt_conn.execute(
        "INSERT INTO prompt_attributes (id, prompt_id, key, value) VALUES (?, ?, 'k', 'v')",
        (_ulid(), prompt_id),
    )
    tgt_conn.execute(
        "INSERT INTO response_attributes (id, response_id, key, value) VALUES (?, ?, 'k', 'v')",
        (_ulid(), response_id),
    )
    tgt_conn.execute(
        "INSERT INTO tool_call_attributes (id, tool_call_id, key, value) VALUES (?, ?, 'k', 'v')",
        (_ulid(), tc_id),
    )
    tag_id = get_or_create_tag(tgt_conn, "tc-tag")
    apply_tag(tgt_conn, "tool_call", tc_id, tag_id)
    tgt_conn.execute(
        "INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at) "
        "VALUES (?, '/fake/path', 'abc123', ?, ?, '2024-01-15T10:00:00Z')",
        (_ulid(), harness_id, conv_id),
    )
    tgt_conn.commit()

    # Verify seeded rows exist
    assert tgt_conn.execute("SELECT COUNT(*) FROM prompt_content").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM response_content").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM prompt_attributes").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM response_attributes").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM tool_call_attributes").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1
    tgt_conn.close()

    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{
            "external_id": "conv-1",
            "prompt_text": "New prompt",
            "tool_name": "file.read",
            "tags": ["research"],
        }],
    )

    result = merge_database(target, source)
    assert result["replaced_conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row

    # Old grandchildren must be gone; only source's children remain
    assert conn.execute("SELECT COUNT(*) FROM prompt_content").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM response_content").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM prompt_attributes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM response_attributes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_call_attributes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0

    # Content should be from source
    text = conn.execute("SELECT content FROM prompt_content LIMIT 1").fetchone()[0]
    assert "New prompt" in text

    # FK integrity
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_cli_no_replace(tmp_path, capsys):
    """CLI --no-replace flag is passed through."""
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original"}],
    )

    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Newer"}],
    )

    rc = main(["--db", str(target), "db", "merge", str(source), "--no-replace"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "0 new" in out
    assert "1 skipped" in out


def test_schema_version_mismatch(tmp_path):
    """Merge rejects source with different schema version."""
    target = _make_db(tmp_path / "target.db", conversations=[])
    source = _make_db(tmp_path / "source.db", conversations=[])

    # Tamper with source schema version
    conn = sqlite3.connect(str(source))
    conn.execute("PRAGMA user_version = 9999")
    conn.close()

    with pytest.raises(RuntimeError, match="Schema version mismatch"):
        merge_database(target, source)


def test_cli_invalid_file(tmp_path, capsys):
    """siftd db merge rejects non-SQLite files."""
    target = _make_db(tmp_path / "target.db", conversations=[])
    bad_source = tmp_path / "not-sqlite.db"
    bad_source.write_text("this is not a database")

    rc = main(["--db", str(target), "db", "merge", str(bad_source)])
    assert rc == 1
    assert "Not a valid SQLite" in capsys.readouterr().err
