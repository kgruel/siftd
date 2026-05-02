"""Tests for siftd db merge — importing a slice into the main database."""

import re
import sqlite3

import pytest
from conftest import make_db as _make_db

from siftd.api.merge import merge_database
from siftd.cli import main
from siftd.storage.sqlite import SCHEMA_PATH, SCHEMA_VERSION
from siftd.storage.tags import apply_tag, get_or_create_tag


def _legacy_merge_schema(*, include_content_blobs: bool, include_result_hash: bool) -> str:
    schema = SCHEMA_PATH.read_text()
    if not include_content_blobs:
        schema = re.sub(r"\nCREATE TABLE content_blobs \(\n.*?\n\);\n", "\n", schema, flags=re.S)
        schema = re.sub(
            r"\nCREATE INDEX idx_content_blobs_ref_count ON content_blobs\(ref_count\);\n",
            "\n",
            schema,
        )
        schema = re.sub(
            r"\nCREATE TRIGGER tr_event_tool_call_[^\n]+\n.*?\nEND;\n",
            "\n",
            schema,
            flags=re.S,
        )
    if not include_result_hash:
        schema = re.sub(
            r"^\s*result_hash\s+TEXT REFERENCES content_blobs\(hash\),.*$",
            "",
            schema,
            flags=re.M,
        )
        schema = re.sub(
            r"\nCREATE TRIGGER tr_event_tool_call_[^\n]+\n.*?\nEND;\n",
            "\n",
            schema,
            flags=re.S,
        )
        schema = re.sub(r",(\s*\n\s*\))", r"\1", schema)
    return schema


def _write_legacy_merge_db(path, *, include_content_blobs: bool, include_result_hash: bool) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            _legacy_merge_schema(
                include_content_blobs=include_content_blobs,
                include_result_hash=include_result_hash,
            )
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


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


def test_workspace_path_fallback_skips_conflicting_non_null_remotes(tmp_path):
    """Same path but conflicting non-null remotes should stay as distinct workspaces."""
    target = _make_db(
        tmp_path / "target.db",
        workspace_path="/shared/project",
        workspace_git_remote="git@github.com:user/target.git",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        workspace_path="/shared/project",
        workspace_git_remote="git@github.com:user/source.git",
        conversations=[{"external_id": "conv-B"}],
    )

    result = merge_database(target, source)
    assert result["conversations"] == 1
    assert result["workspaces_matched"] == 0

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    workspace_count = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    workspace_ids = conn.execute(
        "SELECT DISTINCT workspace_id FROM conversations"
    ).fetchall()
    remotes = {
        row["git_remote"] for row in conn.execute("SELECT git_remote FROM workspaces").fetchall()
    }
    conn.close()

    assert workspace_count == 2
    assert len(workspace_ids) == 2
    assert remotes == {
        "git@github.com:user/target.git",
        "git@github.com:user/source.git",
    }


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

    # Verify ref_counts are correct (event_tool_call is authoritative; tool_calls.result_hash is NULL for new data)
    for row in conn.execute("SELECT hash, ref_count FROM content_blobs").fetchall():
        actual_refs = conn.execute(
            "SELECT COUNT(*) FROM event_tool_call WHERE result_hash = ?", (row["hash"],)
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
    assert result["conversations"] == 0  # replacement not double-counted as new
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


def test_merge_rejects_source_missing_runtime_schema(tmp_path):
    """Same user_version but missing required runtime tables is rejected early."""
    target = _make_db(tmp_path / "target.db", conversations=[])
    source = tmp_path / "source.db"
    _write_legacy_merge_db(
        source,
        include_content_blobs=False,
        include_result_hash=False,
    )

    with pytest.raises(RuntimeError, match="missing required runtime schema: content_blobs table"):
        merge_database(target, source, preflight=False)


def test_merge_rejects_source_missing_result_hash_column(tmp_path):
    """Same user_version but missing runtime columns is rejected before merge SQL runs."""
    target = _make_db(tmp_path / "target.db", conversations=[])
    source = tmp_path / "source.db"
    _write_legacy_merge_db(
        source,
        include_content_blobs=True,
        include_result_hash=False,
    )

    with pytest.raises(RuntimeError, match="tool_calls.result_hash column"):
        merge_database(target, source, preflight=False)


def test_cli_invalid_file(tmp_path, capsys):
    """siftd db merge rejects non-SQLite files."""
    target = _make_db(tmp_path / "target.db", conversations=[])
    bad_source = tmp_path / "not-sqlite.db"
    bad_source.write_text("this is not a database")

    rc = main(["--db", str(target), "db", "merge", str(bad_source)])
    assert rc == 1
    assert "Not a valid SQLite" in capsys.readouterr().err


# =============================================================================
# Conversation ID tracking
# =============================================================================


def test_merge_returns_new_conversation_ids(tmp_path):
    """merge_database returns IDs of newly inserted conversations."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "existing"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "new-a"}, {"external_id": "new-b"}],
    )

    result = merge_database(target, source)

    assert result["conversations"] == 2
    assert len(result["new_conversation_ids"]) == 2
    assert result["replaced_conversation_ids"] == []

    # Verify the returned IDs actually exist in the target
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    for cid in result["new_conversation_ids"]:
        row = conn.execute("SELECT id FROM conversations WHERE id = ?", (cid,)).fetchone()
        assert row is not None
    conn.close()


def test_merge_returns_replaced_conversation_ids(tmp_path):
    """merge_database returns IDs of replacement conversations."""
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old"}],
    )

    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "New"}],
    )

    result = merge_database(target, source)

    assert result["replaced_conversations"] == 1
    assert len(result["replaced_conversation_ids"]) == 1

    # The replacement ID should be the source's conversation ID
    replacement_id = result["replaced_conversation_ids"][0]
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM conversations WHERE id = ?", (replacement_id,)).fetchone()
    assert row is not None
    conn.close()


def test_merge_no_new_returns_empty_ids(tmp_path):
    """Idempotent merge returns empty ID lists."""
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1"}],
    )

    # Second merge — same data, nothing new
    result = merge_database(target, source, replace=False)

    assert result["conversations"] == 0
    assert result["new_conversation_ids"] == []
    assert result["replaced_conversation_ids"] == []


def test_missing_target_db(tmp_path):
    """L41: target DB doesn't exist."""
    import pytest

    from siftd.api.database import create_database
    from siftd.api.merge import merge_database

    source = tmp_path / "source.db"
    create_database(source).close()
    with pytest.raises(FileNotFoundError, match="Target"):
        merge_database(target_db=tmp_path / "nonexistent.db", source_path=source)


def test_missing_source_db(tmp_path):
    """L43: source DB doesn't exist."""
    import pytest

    from siftd.api.database import create_database
    from siftd.api.merge import merge_database

    target = tmp_path / "target.db"
    create_database(target).close()
    with pytest.raises(FileNotFoundError, match="Source"):
        merge_database(target_db=target, source_path=tmp_path / "nonexistent.db")


# =============================================================================
# Preflight gate
# =============================================================================


def _make_fk_corrupt_source(tmp_path):
    from siftd.ids import ulid
    from siftd.storage.sqlite import create_empty_database

    p = tmp_path / "corrupt-source.db"
    create_empty_database(p)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO prompts (id, conversation_id, external_id, timestamp) "
        "VALUES (?, 'nonexistent-conv', 'ext-p-1', '2024-01-01T00:00:00Z')",
        (ulid(),),
    )
    conn.commit()
    conn.close()
    return p


def _make_no_triggers_source(tmp_path):
    from siftd.storage.sqlite import create_empty_database

    p = tmp_path / "notriggers-source.db"
    create_empty_database(p)
    conn = sqlite3.connect(str(p))
    conn.execute("DROP TRIGGER IF EXISTS tr_event_tool_call_delete_release_blob")
    conn.execute("DROP TRIGGER IF EXISTS tr_event_tool_call_update_release_blob")
    conn.commit()
    conn.close()
    return p


def test_merge_preflight_fk_violation(tmp_path):
    """FK violation in source raises PreflightError; target is unmodified."""
    from siftd.api.database import PreflightError

    target = _make_db(tmp_path / "target.db", conversations=[{"external_id": "conv-A"}])
    source = _make_fk_corrupt_source(tmp_path)

    with pytest.raises(PreflightError, match="FK violation"):
        merge_database(target, source)

    conn = sqlite3.connect(str(target))
    count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    conn.close()
    assert count == 1


def test_merge_preflight_missing_trigger(tmp_path):
    """Missing blob triggers in source raises PreflightError."""
    from siftd.api.database import PreflightError

    target = _make_db(tmp_path / "target.db", conversations=[{"external_id": "conv-A"}])
    source = _make_no_triggers_source(tmp_path)

    with pytest.raises(PreflightError, match="trigger"):
        merge_database(target, source)


def test_merge_preflight_disabled(tmp_path):
    """preflight=False bypasses the gate; merge succeeds on trigger-less source."""
    target = _make_db(tmp_path / "target.db", conversations=[{"external_id": "conv-A"}])
    source = _make_no_triggers_source(tmp_path)

    result = merge_database(target, source, preflight=False)
    assert result["conversations"] == 0  # empty source, nothing to merge
