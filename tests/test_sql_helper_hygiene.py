"""H4: SQL helper hygiene — table and column allowlist tests."""

import pytest

from siftd.storage.queries import _COUNTABLE_TABLES, fetch_table_count
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_tool,
)


# ---------------------------------------------------------------------------
# H2 — fetch_table_count table-name allowlist
# ---------------------------------------------------------------------------


def test_fetch_table_count_rejects_unknown_table(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="unknown table"):
        fetch_table_count(conn, "not_a_table")
    conn.close()


def test_fetch_table_count_error_includes_offending_name_and_allowed_set(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="'bad_table'") as exc_info:
        fetch_table_count(conn, "bad_table")
    assert "conversations" in str(exc_info.value)
    conn.close()


def test_fetch_table_count_accepts_all_known_tables(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    for table in _COUNTABLE_TABLES:
        count = fetch_table_count(conn, table)
        assert isinstance(count, int)
    conn.close()


# ---------------------------------------------------------------------------
# H3 — get_or_create_harness column allowlist
# ---------------------------------------------------------------------------


def test_get_or_create_harness_rejects_unknown_kwarg(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="unknown column") as exc_info:
        get_or_create_harness(conn, "test_harness", typo_col="x")
    assert "typo_col" in str(exc_info.value)
    assert "log_format" in str(exc_info.value)
    conn.close()


def test_get_or_create_harness_accepts_valid_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    hid = get_or_create_harness(
        conn, "claude_code", source="anthropic", log_format="jsonl", display_name="Claude Code"
    )
    assert hid
    # idempotent
    assert get_or_create_harness(conn, "claude_code") == hid
    conn.close()


def test_get_or_create_harness_no_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    hid = get_or_create_harness(conn, "minimal_harness")
    assert hid
    conn.close()


# ---------------------------------------------------------------------------
# H3 — get_or_create_provider column allowlist
# ---------------------------------------------------------------------------


def test_get_or_create_provider_rejects_unknown_kwarg(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="unknown column") as exc_info:
        get_or_create_provider(conn, "anthropic", wrong_field="x")
    assert "wrong_field" in str(exc_info.value)
    assert "billing_model" in str(exc_info.value)
    conn.close()


def test_get_or_create_provider_accepts_valid_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    pid = get_or_create_provider(
        conn, "anthropic", display_name="Anthropic API", billing_model="token"
    )
    assert pid
    assert get_or_create_provider(conn, "anthropic") == pid
    conn.close()


def test_get_or_create_provider_no_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    pid = get_or_create_provider(conn, "openai")
    assert pid
    conn.close()


# ---------------------------------------------------------------------------
# H3 — get_or_create_tool column allowlist
# ---------------------------------------------------------------------------


def test_get_or_create_tool_rejects_unknown_kwarg(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="unknown column") as exc_info:
        get_or_create_tool(conn, "file.read", bad_col="x")
    assert "bad_col" in str(exc_info.value)
    assert "category" in str(exc_info.value)
    conn.close()


def test_get_or_create_tool_accepts_valid_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    tid = get_or_create_tool(conn, "file.read", category="file", description="Read a file")
    assert tid
    assert get_or_create_tool(conn, "file.read") == tid
    conn.close()


def test_get_or_create_tool_no_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    tid = get_or_create_tool(conn, "shell.execute")
    assert tid
    conn.close()


# ---------------------------------------------------------------------------
# H3 — get_or_create_model column allowlist (bug_001)
# ---------------------------------------------------------------------------


def test_get_or_create_model_rejects_unknown_kwarg(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="unknown column") as exc_info:
        get_or_create_model(conn, "claude-3-opus-20240229", wrong_field="x")
    assert "wrong_field" in str(exc_info.value)
    assert "creator" in str(exc_info.value)
    conn.close()


def test_get_or_create_model_accepts_valid_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    mid = get_or_create_model(conn, "claude-3-opus-20240229", creator="anthropic")
    assert mid
    assert get_or_create_model(conn, "claude-3-opus-20240229") == mid
    conn.close()


def test_get_or_create_model_no_kwargs(tmp_path):
    conn = create_database(tmp_path / "db.sqlite")
    mid = get_or_create_model(conn, "gpt-4")
    assert mid
    conn.close()
