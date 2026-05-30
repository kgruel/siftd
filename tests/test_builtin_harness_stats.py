"""Correctness guard for the harness-stats builtin query (I20).

The query was rewritten from three correlated LEFT JOINs (a P*R*T cross-product
per conversation) to a single events scan with per-kind conditional
aggregation. Counts must stay identical; this test seeds known per-kind event
counts and asserts the query reports them.
"""

from __future__ import annotations

import importlib.resources

from siftd.storage.sqlite import create_database, get_or_create_harness


def _seed_conversation(conn, harness_id, conv_id, *, prompts, responses, tool_calls):
    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, branch, started_at, ended_at)"
        " VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
        (conv_id, f"ext-{conv_id}", harness_id, "2024-01-01T00:00:00Z"),
    )
    plan = [("prompt", prompts), ("response", responses), ("tool_call", tool_calls)]
    for kind, count in plan:
        for i in range(count):
            conn.execute(
                "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, ?, ?, ?)",
                (f"{conv_id}-{kind}-{i}", kind, conv_id, "2024-01-01T00:00:00Z"),
            )


def test_harness_stats_counts_per_kind(tmp_path):
    conn = create_database(tmp_path / "stats.db")
    hid = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    # Two conversations; per-kind counts chosen so a cross-product would inflate
    # intermediate rows but COUNT(DISTINCT ...) must still report these exactly.
    _seed_conversation(conn, hid, "c1", prompts=2, responses=3, tool_calls=4)
    _seed_conversation(conn, hid, "c2", prompts=1, responses=1, tool_calls=0)
    conn.commit()

    sql = importlib.resources.files("siftd.builtin_queries").joinpath("harness-stats.sql").read_text()
    row = conn.execute(sql).fetchone()
    conn.close()

    # row: (harness, conversations, prompts, responses, tool_calls)
    assert row[0] == "claude_code"
    assert row[1] == 2  # conversations
    assert row[2] == 3  # prompts (2 + 1)
    assert row[3] == 4  # responses (3 + 1)
    assert row[4] == 4  # tool_calls (4 + 0)
