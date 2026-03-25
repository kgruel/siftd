"""End-to-end sync tests: db send → db receive with real subprocesses.

Exercises the full pipeline — argparse, slice, binary transport, merge —
without mocking, so filter flags, multi-value args, and the sync header
are tested against the real code paths.
"""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from siftd.storage.sqlite import open_database
from siftd.storage.tags import apply_tag, get_or_create_tag


@pytest.fixture
def isolated_env(tmp_path):
    """Return an env dict with XDG dirs rooted under tmp_path.

    Prevents e2e subprocesses from reading/writing the developer's real
    XDG directories or triggering update checks.
    """
    env = {**os.environ}
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["SIFTD_NO_UPDATE_CHECK"] = "1"
    return env


def _run_siftd(
    *args: str, stdin: bytes | None = None, env: dict | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["uv", "run", "siftd", *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=30, env=env)


def _seed_db(tmp_path: Path, name: str = "source.db") -> Path:
    """Create a source DB with 3 tagged conversations for filter tests.

    Layout:
      conv_a: tagged "public", "release"
      conv_b: tagged "private"
      conv_c: no tags
    """
    from siftd.storage.sqlite import (
        get_or_create_harness,
        get_or_create_model,
        get_or_create_workspace,
        insert_conversation,
        insert_prompt,
        insert_prompt_content,
        insert_response,
        insert_response_content,
    )

    db = tmp_path / name
    conn = open_database(db)

    h = get_or_create_harness(conn, "test")
    w = get_or_create_workspace(conn, "/test/project", "2024-01-01")
    m = get_or_create_model(conn, "test-model")

    conv_a = insert_conversation(conn, external_id="a", harness_id=h,
                                 workspace_id=w, started_at="2024-01-01T01:00:00Z")
    p = insert_prompt(conn, conv_a, "pa", "2024-01-01T01:00:00Z")
    insert_prompt_content(conn, p, 0, "text", '{"text": "conv a"}')
    r = insert_response(conn, conv_a, p, m, None, "ra", "2024-01-01T01:00:01Z",
                        input_tokens=10, output_tokens=5)
    insert_response_content(conn, r, 0, "text", '{"text": "resp a"}')

    conv_b = insert_conversation(conn, external_id="b", harness_id=h,
                                 workspace_id=w, started_at="2024-01-02T01:00:00Z")
    p = insert_prompt(conn, conv_b, "pb", "2024-01-02T01:00:00Z")
    insert_prompt_content(conn, p, 0, "text", '{"text": "conv b"}')
    r = insert_response(conn, conv_b, p, m, None, "rb", "2024-01-02T01:00:01Z",
                        input_tokens=10, output_tokens=5)
    insert_response_content(conn, r, 0, "text", '{"text": "resp b"}')

    conv_c = insert_conversation(conn, external_id="c", harness_id=h,
                                 workspace_id=w, started_at="2024-01-03T01:00:00Z")
    p = insert_prompt(conn, conv_c, "pc", "2024-01-03T01:00:00Z")
    insert_prompt_content(conn, p, 0, "text", '{"text": "conv c"}')
    r = insert_response(conn, conv_c, p, m, None, "rc", "2024-01-03T01:00:01Z",
                        input_tokens=10, output_tokens=5)
    insert_response_content(conn, r, 0, "text", '{"text": "resp c"}')

    # Tags
    tag_public = get_or_create_tag(conn, "public")
    tag_release = get_or_create_tag(conn, "release")
    tag_private = get_or_create_tag(conn, "private")

    apply_tag(conn, "conversation", conv_a, tag_public)
    apply_tag(conn, "conversation", conv_a, tag_release)
    apply_tag(conn, "conversation", conv_b, tag_private)

    conn.commit()
    conn.close()
    return db


def _count_conversations(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    conn.close()
    return n


def _external_ids(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    ids = {r["external_id"] for r in conn.execute("SELECT external_id FROM conversations")}
    conn.close()
    return ids


class TestSendReceivePipeline:
    """Full e2e: db send | db receive via subprocesses."""

    def test_send_all_receive(self, tmp_path, isolated_env):
        source = _seed_db(tmp_path)
        target = tmp_path / "target.db"

        send = _run_siftd("--db", str(source), "db", "send", env=isolated_env)
        assert send.returncode == 0
        assert len(send.stdout) > 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 3

        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0

        assert _count_conversations(target) == 3
        assert _external_ids(target) == {"a", "b", "c"}

    def test_send_with_tag_filter(self, tmp_path, isolated_env):
        source = _seed_db(tmp_path)

        send = _run_siftd("--db", str(source), "db", "send", "--tag", "public", env=isolated_env)
        assert send.returncode == 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 1

        target = tmp_path / "target.db"
        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0
        assert _external_ids(target) == {"a"}

    def test_send_with_multi_tag_filter(self, tmp_path, isolated_env):
        """Multiple --tag flags select conversations matching any tag."""
        source = _seed_db(tmp_path)

        send = _run_siftd(
            "--db", str(source), "db", "send",
            "--tag", "public", "--tag", "private",
            env=isolated_env,
        )
        assert send.returncode == 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 2

        target = tmp_path / "target.db"
        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0
        assert _external_ids(target) == {"a", "b"}

    def test_send_with_no_tag_filter(self, tmp_path, isolated_env):
        source = _seed_db(tmp_path)

        send = _run_siftd("--db", str(source), "db", "send", "--no-tag", "private", env=isolated_env)
        assert send.returncode == 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 2  # a and c (b excluded)

        target = tmp_path / "target.db"
        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0
        assert _external_ids(target) == {"a", "c"}

    def test_send_with_multi_no_tag_filter(self, tmp_path, isolated_env):
        """Multiple --no-tag flags exclude conversations matching any excluded tag."""
        source = _seed_db(tmp_path)

        send = _run_siftd(
            "--db", str(source), "db", "send",
            "--no-tag", "private", "--no-tag", "release",
            env=isolated_env,
        )
        assert send.returncode == 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 1  # only c (a has release, b has private)

        target = tmp_path / "target.db"
        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0
        assert _external_ids(target) == {"c"}

    def test_send_with_since_filter(self, tmp_path, isolated_env):
        source = _seed_db(tmp_path)

        send = _run_siftd(
            "--db", str(source), "db", "send", "--since", "2024-01-02",
            env=isolated_env,
        )
        assert send.returncode == 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 2  # b and c

        target = tmp_path / "target.db"
        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0
        assert _external_ids(target) == {"b", "c"}

    def test_send_combined_filters(self, tmp_path, isolated_env):
        """--since + --no-tag combine correctly."""
        source = _seed_db(tmp_path)

        send = _run_siftd(
            "--db", str(source), "db", "send",
            "--since", "2024-01-02", "--no-tag", "private",
            env=isolated_env,
        )
        assert send.returncode == 0

        meta = json.loads(send.stderr)
        assert meta["conversations"] == 1  # only c

        target = tmp_path / "target.db"
        receive = _run_siftd("--db", str(target), "db", "receive", stdin=send.stdout, env=isolated_env)
        assert receive.returncode == 0
        assert _external_ids(target) == {"c"}


class TestSyncStatusCommand:
    """e2e: db sync-status returns valid JSON with capabilities."""

    def test_sync_status_output(self, tmp_path, isolated_env):
        db = tmp_path / "test.db"
        open_database(db).close()

        result = _run_siftd("--db", str(db), "db", "sync-status", env=isolated_env)
        assert result.returncode == 0

        status = json.loads(result.stdout)
        assert "staged" in status["capabilities"]
        assert status["inbox"]["pending"] == 0
        assert "protocol_version" in status


class TestStagedReceive:
    """e2e: db receive --stage + db process pipeline."""

    def test_stage_then_process(self, tmp_path, isolated_env):
        source = _seed_db(tmp_path)
        target = tmp_path / "target.db"

        # Send all conversations
        send = _run_siftd("--db", str(source), "db", "send", env=isolated_env)
        assert send.returncode == 0

        # Stage (fast ACK, no merge yet)
        stage = _run_siftd(
            "--db", str(target), "db", "receive", "--stage",
            stdin=send.stdout, env=isolated_env,
        )
        assert stage.returncode == 0
        result = json.loads(stage.stdout)
        assert result["status"] == "staged"

        # Target should exist but have no conversations yet
        assert _count_conversations(target) == 0

        # Check status shows pending
        status = _run_siftd("--db", str(target), "db", "sync-status", env=isolated_env)
        inbox = json.loads(status.stdout)["inbox"]
        assert inbox["pending"] == 1

        # Process the inbox
        process = _run_siftd("--db", str(target), "db", "process", env=isolated_env)
        assert process.returncode == 0

        # Now conversations should be there
        assert _count_conversations(target) == 3
        assert _external_ids(target) == {"a", "b", "c"}

        # Status shows no pending
        status = _run_siftd("--db", str(target), "db", "sync-status", env=isolated_env)
        inbox = json.loads(status.stdout)["inbox"]
        assert inbox["pending"] == 0
        assert inbox["last"]["status"] == "done"
