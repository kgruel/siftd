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
        schema = re.sub(
            r"\nCREATE INDEX idx_event_tool_call_result_hash[^;]*;\n",
            "\n",
            schema,
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
    prompts = conn.execute("SELECT COUNT(*) FROM events WHERE kind='prompt'").fetchone()[0]
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
    junctions = conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE target_kind='conversation'").fetchone()[0]
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
    tool_calls = conn.execute("SELECT COUNT(*) FROM events WHERE kind='tool_call'").fetchone()[0]
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


def _fts_matches(db_path, term):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH ?", (term,),
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("rebuild_fts", [False, True])
def test_fts_rebuild(tmp_path, rebuild_fts):
    """Search finds content from both DBs after merge, at either flag value."""
    from siftd.storage.fts import rebuild_fts_index

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "prompt_text": "Python decorators"}],
    )
    conn = sqlite3.connect(str(target))
    rebuild_fts_index(conn, commit=True)   # the target arrives already indexed
    conn.close()

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "prompt_text": "Rust lifetimes"}],
    )

    merge_database(target, source, rebuild_fts=rebuild_fts)

    assert _fts_matches(target, "Python") >= 1
    assert _fts_matches(target, "Rust") >= 1


def test_rebuild_fts_true_no_longer_repairs_pre_existing_drift(tmp_path):
    """The trade #49 makes, stated: `rebuild_fts=True` is a no-op.

    It used to run a full corpus rebuild post-commit, which incidentally healed
    index drift the merge did not cause. That side effect is gone — the merge
    indexes what it wrote and nothing else. Repair has two deliberate owners,
    `siftd ingest --rebuild-fts` and `siftd doctor fix`, and O(corpus) on every
    push is exactly the cost that made the push paths skip indexing at all.

    This test exists so removing the knob outright (#74) cannot look like a
    behavior change: it already is one, here.
    """
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A", "prompt_text": "Python decorators"}],
    )
    assert _fts_matches(target, "Python") == 0, "_make_db leaves the target unindexed"

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B", "prompt_text": "Rust lifetimes"}],
    )
    merge_database(target, source, rebuild_fts=True)

    assert _fts_matches(target, "Rust") >= 1, "what the merge wrote is indexed"
    assert _fts_matches(target, "Python") == 0, "what it did not write is left alone"


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


def test_dry_run_of_a_replacing_merge_leaves_the_target_intact(tmp_path):
    """A dry run that would replace rolls the cascade back too (#51).

    The plain dry run above only ever inserts. This one exercises the branch the
    deferred-foreign-key change actually forks on: a replacing merge runs its
    delete inside `SAVEPOINT merge_dry_run` rather than an explicit `BEGIN`,
    because a `BEGIN` there would still be open at `RELEASE` and `DETACH` cannot
    run inside a transaction. Get that fork wrong and the failure is either a
    hard DETACH error or — worse — a dry run that deletes for real.
    """
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old", "tool_name": "sh"}],
    )
    time.sleep(0.01)
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "New", "tool_name": "sh"}],
    )

    conn = sqlite3.connect(str(target))
    before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("conversations", "events", "event_content", "tag_assignments")
    }
    old_id = conn.execute("SELECT id FROM conversations").fetchone()[0]
    conn.close()

    result = merge_database(target, source, dry_run=True)
    assert result["replaced_conversations"] == 1

    conn = sqlite3.connect(str(target))
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before
    assert conn.execute("SELECT id FROM conversations").fetchone()[0] == old_id
    conn.close()


def test_replace_leaves_no_trace_of_the_old_conversation(tmp_path):
    """The delete closure is complete — asked of the database, not a list (#51).

    `_replace_stale_conversations` used to hand-enumerate this closure in 11
    statements beside the two in `delete_conversation`, with nothing keeping the
    copies in sync; it went stale when the v9 derived tier was added and broke
    every replacing merge (#20). It is now one `DELETE FROM conversations` plus
    `content_fts`, and the schema's cascades and `tr_polymorphic_*` triggers do
    the rest.

    So this asks the question that survives either implementation: after the
    merge, does the old conversation's id appear in any TEXT column of any
    table? Derived by sweeping `sqlite_master`, so a table added later is
    covered without editing this test — which is exactly what the old
    enumeration could not say about itself.
    """
    import time

    from siftd.ids import ulid as _ulid
    from siftd.storage.sqlite import open_database as _open

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old",
                        "tool_name": "sh", "tags": ["review"]}],
    )
    tgt = _open(target)
    old_conv = tgt.execute("SELECT id FROM conversations").fetchone()["id"]
    old_ids = {old_conv} | {
        r[0] for r in tgt.execute("SELECT id FROM events WHERE conversation_id = ?", (old_conv,))
    } | {
        r[0] for r in tgt.execute(
            "SELECT ec.id FROM event_content ec JOIN events e ON e.id = ec.event_id"
            " WHERE e.conversation_id = ?", (old_conv,)
        )
    }
    harness_id = tgt.execute("SELECT harness_id FROM conversations").fetchone()[0]
    tgt.execute(
        "INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at)"
        " VALUES (?, '/fake/path', 'abc123', ?, ?, '2024-01-15T10:00:00Z')",
        (_ulid(), harness_id, old_conv),
    )
    tgt.commit()
    assert tgt.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1
    tgt.close()

    time.sleep(0.01)
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "New", "tool_name": "sh"}],
    )
    assert merge_database(target, source)["replaced_conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    tables = [
        n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            " AND name NOT LIKE 'sqlite_%'"
        )
    ]
    survivors: list[tuple[str, str, str]] = []
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not cols:
            continue  # shadow table with no introspectable columns
        for row in conn.execute(f"SELECT {', '.join(f'[{c}]' for c in cols)} FROM [{table}]"):
            for col, value in zip(cols, row, strict=True):
                if isinstance(value, str) and value in old_ids:
                    survivors.append((table, col, value))
    conn.close()
    assert survivors == [], (
        f"the replaced conversation still has rows referencing it: {survivors}. "
        "The delete closure did not reach them — check whether the table declares "
        "ON DELETE CASCADE from conversations (or is covered by a "
        "tr_polymorphic_*_cleanup trigger), since merge no longer enumerates them."
    )


def test_replaced_conversations_blob_is_released_by_the_cascade(tmp_path):
    """A replaced tool call's blob is still GC'd when the delete is a cascade.

    `tr_event_tool_call_delete_release_blob` fires on `event_tool_call` deletes.
    Merge used to delete those rows itself; since #51 they go by the cascade
    from `conversations`, and this pins that the trigger still fires — the one
    of #51's stated open costs that a passing suite would not otherwise notice,
    because the blob is re-supplied by the source and the row count looks right
    either way.
    """
    import time

    from siftd.storage.blobs import store_content
    from siftd.storage.sqlite import open_database as _open

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old", "tool_name": "sh"}],
    )
    # Re-point the target's tool call at a result no source will re-supply.
    # `make_db` writes the same canned result on both sides, so their blobs are
    # the same content-addressed row and the merge's own insert would put it
    # back — a test that could not tell a released blob from a re-supplied one.
    tgt = _open(target)
    unique = store_content(tgt, '{"output": "only the replaced version had this"}')
    tc_event = tgt.execute("SELECT event_id FROM event_tool_call").fetchone()[0]
    tgt.execute(
        "UPDATE event_tool_call SET result_hash = ? WHERE event_id = ?", (unique, tc_event)
    )
    # `store_content` counted a reference and so did the UPDATE trigger, which
    # would leave the blob at 2 and surviving the delete for a reason that has
    # nothing to do with the merge. Recompute from the referrers — the same
    # expression the merge's own step 5 uses.
    tgt.execute(
        "UPDATE content_blobs SET ref_count ="
        " (SELECT COUNT(*) FROM event_tool_call WHERE result_hash = content_blobs.hash)"
    )
    tgt.commit()
    old_hashes = {unique}
    assert tgt.execute(
        "SELECT ref_count FROM content_blobs WHERE hash = ?", (unique,)
    ).fetchone()[0] == 1
    tgt.close()

    time.sleep(0.01)
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "New", "tool_name": "sh"}],
    )
    assert merge_database(target, source)["replaced_conversations"] == 1

    conn = sqlite3.connect(str(target))
    remaining = {r[0] for r in conn.execute("SELECT hash FROM content_blobs")}
    counts = dict(conn.execute("SELECT hash, ref_count FROM content_blobs"))
    conn.close()
    assert not (old_hashes & remaining), (
        f"the replaced version's blob(s) {sorted(old_hashes & remaining)} were not "
        "released — the AFTER DELETE trigger did not fire for the cascaded "
        "event_tool_call row"
    )
    assert all(c > 0 for c in counts.values()), f"orphan blobs left behind: {counts}"


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
    assert result["content_blobs"] == 0


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
    assert "[Dry run]" in out

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

    # Event count should be unchanged (1 prompt + 1 response from original)
    prompts = conn.execute("SELECT COUNT(*) FROM events WHERE kind='prompt'").fetchone()[0]
    responses = conn.execute("SELECT COUNT(*) FROM events WHERE kind='response'").fetchone()[0]
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
    wt_count = conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE target_kind='workspace'").fetchone()[0]
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
    tc_id = src_conn.execute("SELECT id FROM events WHERE kind='tool_call' LIMIT 1").fetchone()["id"]
    tag_id = get_or_create_tag(src_conn, "shell:test")
    apply_tag(src_conn, "tool_call", tc_id, tag_id)
    src_conn.commit()
    src_conn.close()

    result = merge_database(target, source)
    assert result["conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    tct_count = conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE target_kind='tool_call'").fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    assert tct_count >= 1
    assert violations == []


def test_block_tag_merge(tmp_path):
    """Block tags (target_kind='block', target_id=event_content.id) survive merge.

    Regression: the tag_assignments copy enumerated only conversation/workspace/
    event kinds, so block tags were silently dropped — and a block-only tag
    vanished entirely from the target.
    """
    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-A"}],
    )
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-B"}],
    )

    from siftd.storage.sqlite import open_database as _open

    src_conn = _open(source)
    block_id = src_conn.execute(
        "SELECT ec.id FROM event_content ec JOIN events e ON e.id = ec.event_id "
        "WHERE e.kind = 'response' LIMIT 1"
    ).fetchone()["id"]
    tag_id = get_or_create_tag(src_conn, "docs:block-only")
    apply_tag(src_conn, "block", block_id, tag_id)
    src_conn.commit()
    src_conn.close()

    result = merge_database(target, source)
    assert result["conversations"] == 1

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    n = conn.execute(
        "SELECT COUNT(*) FROM tag_assignments WHERE target_kind = 'block' AND target_id = ?",
        (block_id,),
    ).fetchone()[0]
    tag_present = conn.execute(
        "SELECT COUNT(*) FROM tags WHERE name = 'docs:block-only'"
    ).fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()

    assert n == 1
    assert tag_present == 1
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

    # Prompt text should be from source (event_content stores JSON blocks)
    import json
    text = conn.execute(
        "SELECT ec.content FROM event_content ec"
        " JOIN events e ON e.id = ec.event_id WHERE e.kind='prompt' LIMIT 1"
    ).fetchone()[0]
    assert "Updated v2" in json.loads(text).get("text", "")

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
    import json
    text = conn.execute(
        "SELECT ec.content FROM event_content ec"
        " JOIN events e ON e.id = ec.event_id WHERE e.kind='prompt' LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert "Original v1" in json.loads(text).get("text", "")


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
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='prompt'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='response'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='tool_call'").fetchone()[0] == 1

    # Tool should be the source's
    tool_name = conn.execute("""
        SELECT t.name FROM event_tool_call etc
        JOIN tools t ON t.id = etc.tool_id
    """).fetchone()[0]
    assert tool_name == "file.read"

    # Both tags: the source's, and the one the target already carried. This
    # asserted `== "research"` off a bare fetchone() until #77 — which read as
    # "the source wins" but was really "the target's tag was destroyed", and
    # could not have distinguished the two.
    tag_names = {
        row[0] for row in conn.execute("""
            SELECT t.name FROM tag_assignments ta
            JOIN tags t ON t.id = ta.tag_id
            WHERE ta.target_kind = 'conversation'
        """)
    }
    assert tag_names == {"research", "review"}

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

    # Seed grandchild rows in target: attributes, tool_call tag_assignments, ingested_files
    tgt_conn = _open(target)
    prompt_id = tgt_conn.execute("SELECT id FROM events WHERE kind='prompt' LIMIT 1").fetchone()["id"]
    response_id = tgt_conn.execute("SELECT id FROM events WHERE kind='response' LIMIT 1").fetchone()["id"]
    tc_id = tgt_conn.execute("SELECT id FROM events WHERE kind='tool_call' LIMIT 1").fetchone()["id"]
    conv_id = tgt_conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    harness_id = tgt_conn.execute("SELECT harness_id FROM conversations LIMIT 1").fetchone()[0]

    tgt_conn.execute(
        "INSERT INTO attributes (id, target_kind, target_id, key, value) VALUES (?, 'prompt', ?, 'k', 'v')",
        (_ulid(), prompt_id),
    )
    tgt_conn.execute(
        "INSERT INTO attributes (id, target_kind, target_id, key, value) VALUES (?, 'response', ?, 'k', 'v')",
        (_ulid(), response_id),
    )
    tgt_conn.execute(
        "INSERT INTO attributes (id, target_kind, target_id, key, value) VALUES (?, 'tool_call', ?, 'k', 'v')",
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
    assert tgt_conn.execute("SELECT COUNT(*) FROM event_content").fetchone()[0] >= 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM attributes WHERE target_kind='prompt'").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM attributes WHERE target_kind='response'").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM attributes WHERE target_kind='tool_call'").fetchone()[0] == 1
    assert tgt_conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE target_kind='tool_call'").fetchone()[0] == 1
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
    assert conn.execute("SELECT COUNT(*) FROM event_content").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM attributes WHERE target_kind='prompt'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM attributes WHERE target_kind='response'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM attributes WHERE target_kind='tool_call'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0

    # The tool_call *tag*, unlike the attributes beside it, is re-pointed onto
    # the replacement's event rather than destroyed (#77). A person put it
    # there; no re-parse of the source puts it back, which is the whole line
    # `storage/replacement.py` draws. This asserted `== 0` before, making the
    # loss look deliberate.
    carried = conn.execute("""
        SELECT ta.target_id, t.name FROM tag_assignments ta
        JOIN tags t ON t.id = ta.tag_id
        WHERE ta.target_kind = 'tool_call'
    """).fetchall()
    new_tc_id = conn.execute("SELECT id FROM events WHERE kind='tool_call'").fetchone()[0]
    assert [(r[0], r[1]) for r in carried] == [(new_tc_id, "tc-tag")]

    # Content should be from source (event_content stores JSON blocks)
    import json
    content_json = conn.execute(
        "SELECT ec.content FROM event_content ec"
        " JOIN events e ON e.id = ec.event_id WHERE e.kind='prompt' LIMIT 1"
    ).fetchone()[0]
    assert "New prompt" in json.loads(content_json).get("text", "")

    # FK integrity
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_replace_clears_derived_tier(tmp_path):
    """After a replace, the derived tier describes the replacement (siftd#20).

    The tier's rows for the stale conversation used to survive the delete — both
    tables declare ON DELETE CASCADE, which the merge's `foreign_keys = OFF`
    disables — and foreign_key_check rolled the whole merge back.

    That rollback is now caught by every foreign_key_check assertion in this
    file, since `make_db` populates the tier. What this test adds is the
    positive half: the surviving rows are keyed to the *new* conversation.
    """
    import time

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Original"}],
    )
    time.sleep(0.01)
    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Updated"}],
    )

    conn = sqlite3.connect(str(target))
    old_id = conn.execute("SELECT id FROM conversations").fetchone()[0]
    # Precondition, not setup: if the fixture ever stops rebuilding the tier,
    # this test — and the file's foreign_key_check assertions — go vacuous.
    assert conn.execute("SELECT COUNT(*) FROM usage_by_conv_model").fetchone()[0] > 0
    conn.close()

    result = merge_database(target, source)
    assert result["replaced_conversations"] == 1

    conn = sqlite3.connect(str(target))
    new_id = conn.execute("SELECT id FROM conversations").fetchone()[0]
    assert new_id != old_id
    for table in ("usage_by_conv_model", "conversation_stats"):
        ids = {r[0] for r in conn.execute(f"SELECT conversation_id FROM {table}")}
        assert ids == {new_id}, table
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_replace_carries_tags_and_ownership(tmp_path):
    """A merge that replaces a conversation keeps what a person attached (#77).

    The homelab shape: a reviewer tags a conversation on the server, the machine
    that produced it appends one more turn, and the next push replaces it. Every
    tag used to go with the old rows — while ownership, carried by a temp-table
    special case right beside the delete, survived. This is the mirror of #54,
    which carried tags and dropped ownership; both are now one snapshot.

    Element tags are the discriminating half: a conversation tag only needs the
    new conversation id, but an event tag has to be *re-pointed* by rejoining on
    the identifier the replacement shares with its predecessor.
    """
    import time

    from siftd.storage.sqlite import ensure_conversation_owners_table
    from siftd.storage.sqlite import open_database as _open
    from siftd.storage.tags import get_tag_assignments

    target = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old", "tool_name": "sh"}],
    )
    tgt = _open(target)
    ensure_conversation_owners_table(tgt)
    old_conv = tgt.execute("SELECT id FROM conversations").fetchone()["id"]
    old_prompt = tgt.execute("SELECT id FROM events WHERE kind='prompt'").fetchone()["id"]
    apply_tag(tgt, "conversation", old_conv, get_or_create_tag(tgt, "decision:auth"))
    apply_tag(tgt, "prompt", old_prompt, get_or_create_tag(tgt, "needs-followup"))
    tgt.execute(
        "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at)"
        " VALUES (?, 'alice', NULL, ?)",
        (old_conv, "2024-01-01T00:00:00Z"),
    )
    tgt.commit()
    tgt.close()

    time.sleep(0.01)

    source = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old and then more",
                        "tool_name": "sh"}],
    )

    result = merge_database(target, source)
    assert result["replaced_conversations"] == 1

    conn = _open(target)
    new_conv = conn.execute("SELECT id FROM conversations").fetchone()["id"]
    new_prompt = conn.execute("SELECT id FROM events WHERE kind='prompt'").fetchone()["id"]
    assert new_conv != old_conv, "the replacement should be a different row"

    conv_tags = {
        r[0] for r in conn.execute(
            "SELECT t.name FROM tag_assignments ta JOIN tags t ON t.id = ta.tag_id"
            " WHERE ta.target_kind = 'conversation' AND ta.target_id = ?", (new_conv,)
        )
    }
    assert "decision:auth" in conv_tags

    assert len(get_tag_assignments(conn, "prompt", new_prompt)) == 1, (
        "the element tag was not re-pointed onto the replacement's event"
    )
    assert not get_tag_assignments(conn, "prompt", old_prompt)

    assert conn.execute(
        "SELECT user_id FROM conversation_owners WHERE conversation_id = ?", (new_conv,)
    ).fetchone()[0] == "alice"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


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


def test_merge_rejects_source_missing_event_tool_call_table(tmp_path):
    """Source DB missing event_tool_call table is rejected before merge SQL runs."""
    import sqlite3 as _sqlite3
    target = _make_db(tmp_path / "target.db", conversations=[])
    source_path = tmp_path / "source-no-etc.db"
    conn = _sqlite3.connect(str(source_path))
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE content_blobs (hash TEXT PRIMARY KEY, content TEXT, ref_count INTEGER, created_at TEXT)")
    conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT, conversation_id TEXT, parent_id TEXT, external_id TEXT, timestamp TEXT)")
    # Intentionally omit event_tool_call
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="event_tool_call table"):
        merge_database(target, source_path, preflight=False)


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
        "INSERT INTO events (id, kind, conversation_id, external_id, timestamp) "
        "VALUES (?, 'prompt', 'nonexistent-conv', 'ext-p-1', '2024-01-01T00:00:00Z')",
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
