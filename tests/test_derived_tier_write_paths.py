"""The derived tier (usage_by_conv_model / conversation_stats) is an invariant:
every path that bulk-writes raw rows must rebuild it, exactly as ingest does.

These tests exercise the REAL slice/merge/receive APIs and then read the
rollup-backed stats — they deliberately do NOT call rebuild_rollups in setup.
That fixture habit is precisely what hid the S2 regression: slice() and merge()
copy raw event rows but did not rebuild the rollup, so stats reads either
crashed ("no such table: usage_by_conv_model") or silently reported zero tokens
for real conversations. make_db gives each conversation 100 input + 50 output
tokens and does NOT rebuild the rollup, so a passing assertion here proves the
write-path rebuilt the tier itself.
"""

from pathlib import Path

import pytest
from conftest import make_db

import siftd.storage.sqlite as sq
from siftd.api.receive import receive_database
from siftd.api.slice import slice_database
from siftd.api.stats import (
    get_usage_by_model,
    get_usage_by_workspace,
    get_usage_summary,
)
from siftd.storage import queries as q
from siftd.storage.sqlite import open_database
from siftd.storage.usage_rollup import rebuild_rollups


def _source(path: Path, n: int) -> Path:
    return make_db(
        path,
        conversations=[{"external_id": f"conv-{i}"} for i in range(n)],
    )


@pytest.mark.parametrize("rebuild_fts", [False, True])
def test_slice_rebuilds_derived_tier(tmp_path, rebuild_fts):
    """slice_database leaves the target's rollup built, so stats reads over the
    slice return the real totals (not a crash at rebuild_fts=False, not zero)."""
    source = _source(tmp_path / "src.db", n=2)
    target = tmp_path / "slice.db"

    slice_database(source, target, rebuild_fts=rebuild_fts)

    # 2 conversations × (100 input, 50 output).
    summary = get_usage_summary(db_path=target)
    assert summary.total_conversations == 2
    assert summary.total_input_tokens == 200
    assert summary.total_output_tokens == 100

    by_model = get_usage_by_model(db_path=target)
    assert sum(m.input_tokens for m in by_model) == 200
    assert sum(m.conversations for m in by_model) == 2

    by_ws = get_usage_by_workspace(db_path=target)
    assert sum(w.input_tokens for w in by_ws) == 200

    conn = open_database(target, read_only=True)
    try:
        assert q.fetch_response_token_coverage(conn) == (2, 2)
        assert sum(r["responses"] for r in q.fetch_token_coverage_by_harness(conn)) == 2
    finally:
        conn.close()


def _priced_fallback_source(path: Path) -> None:
    """Source with a NULL-provider response priced ONLY via the harness source
    (providers.name == harnesses.source) — the canonical fallback path. 1M input
    tokens at $3/Mtok = $3.00."""
    conn = sq.create_database(path)
    prov = sq.get_or_create_provider(conn, "anthropic")
    model = sq.get_or_create_model(conn, "claude-test-model")
    harness = sq.get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES (?, ?, ?, ?, ?)",
        ("pr1", model, prov, 3.0, 15.0),
    )
    cid = sq.insert_conversation(conn, "c1", harness, ws, "2024-01-01T00:00:00Z")
    pid = sq.insert_prompt(conn, cid, "p1", "2024-01-01T00:00:00Z")
    sq.insert_response(conn, cid, pid, model, None, "r1", "2024-01-01T00:00:01Z", 1_000_000, 0)
    rebuild_rollups(conn)
    conn.commit()
    conn.close()


def test_slice_preserves_harness_fallback_pricing(tmp_path):
    """The slice must carry the harness-source fallback provider AND its pricing,
    or the target reprices NULL-provider responses to 0. The fallback provider is
    referenced by no response.provider_id, so a referenced-only copy drops it."""
    src = tmp_path / "src.db"
    _priced_fallback_source(src)

    sliced = tmp_path / "slice.db"
    slice_database(src, sliced)
    assert get_usage_summary(db_path=sliced).total_cost == pytest.approx(3.0)

    # And it survives the sync hop (slice -> receive).
    remote = tmp_path / "remote.db"
    receive_database(sliced, remote)
    assert get_usage_summary(db_path=remote).total_cost == pytest.approx(3.0)


def test_empty_slice_returns_zero_stats(tmp_path):
    """A zero-match slice is a valid empty DB: stats return zeros, not a crash.
    The derived-tier rebuild runs even when no conversations match, so the
    rollup table exists (create_empty_database writes schema.sql only, which
    omits the derived tier). This is the empty-corpus case the loud-on-absent
    read policy depends on staying distinct from a malformed DB."""
    source = _source(tmp_path / "src.db", n=2)
    target = tmp_path / "empty.db"

    result = slice_database(source, target, workspace="NO-SUCH-WORKSPACE")
    assert result["conversations"] == 0

    summary = get_usage_summary(db_path=target)
    assert summary.total_conversations == 0
    assert summary.total_input_tokens == 0

    conn = open_database(target, read_only=True)
    try:
        assert q.fetch_response_token_coverage(conn) == (0, 0)
        assert q.fetch_token_coverage_by_harness(conn) == []
    finally:
        conn.close()


def test_receive_rebuilds_derived_tier(tmp_path):
    """receive_database (the live sync path → merge_database) rebuilds the
    target's rollup, picking up BOTH the merged conversations and the target's
    own pre-existing rows (a full rebuild, so prior staleness is repaired)."""
    target = tmp_path / "team.db"
    make_db(target, conversations=[{"external_id": "conv-B"}])
    source = make_db(tmp_path / "incoming.db", conversations=[{"external_id": "conv-A"}])

    result = receive_database(source, target)
    assert result["status"] == "merged"

    # Target now holds both conversations: 2 × (100 input, 50 output).
    summary = get_usage_summary(db_path=target)
    assert summary.total_conversations == 2
    assert summary.total_input_tokens == 200
    assert summary.total_output_tokens == 100

    conn = open_database(target, read_only=True)
    try:
        assert q.fetch_response_token_coverage(conn) == (2, 2)
    finally:
        conn.close()
