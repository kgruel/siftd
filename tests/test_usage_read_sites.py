"""Read-site regressions for the S2 rollup re-point (api.stats).

The rollup itself is tested in test_usage_rollup.py; these tests guard the *read
functions* that group over it — the sites that carried the bugs S2 fixes:

- get_usage_by_model / get_usage_by_workspace summed a per-conversation cs.cost
  joined to per-response rows, fanning each conversation's cost out once per
  response (the live 290x: $913,780 vs $3,144). The fan is detectable only with
  >1 response per conversation, which these fixtures have.
- workspace_detail hardcoded model_mix cost=0.0 next to a separate cs.cost
  headline, so the per-model rows and the headline could disagree. The headline
  is now the sum of the model mix.
"""

from painted import Fidelity

import siftd.storage.sqlite as sq
from siftd.api.stats import (
    get_usage_by_model,
    get_usage_by_workspace,
    get_usage_summary,
    workspace_detail,
)
from siftd.storage.usage_rollup import rebuild_rollups

_F = Fidelity()


def _seed_pricing(conn, model_id, provider_id, in_rate, out_rate, pid="pr-test"):
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, model_id, provider_id, in_rate, out_rate),
    )


def _build_multi_response(db_path, *, n_responses=3):
    """One workspace, one conversation, `n_responses` responses of one priced
    model (1M input tokens each at $3/Mtok → $3 each, $3*n total)."""
    conn = sq.create_database(db_path)
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    h = sq.get_or_create_harness(conn, "tool", source="anthropic")
    m = sq.get_or_create_model(conn, "claude-x")
    p = sq.get_or_create_provider(conn, "anthropic")
    _seed_pricing(conn, m, p, 3.0, 15.0)
    cid = sq.insert_conversation(conn, "c1", h, ws, "2024-01-01T00:00:00Z")
    pid = sq.insert_prompt(conn, cid, "p1", "2024-01-01T00:00:00Z")
    for i in range(n_responses):
        sq.insert_response(
            conn, cid, pid, m, p, f"r{i}", f"2024-01-01T00:00:0{i + 1}Z", 1_000_000, 0
        )
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    return ws, cid


def test_usage_by_model_cost_not_fanned_by_response_count(tmp_path):
    db = tmp_path / "m.db"
    _build_multi_response(db, n_responses=3)

    rows = get_usage_by_model(db_path=db)
    assert [r.name for r in rows] == ["claude-x"]
    # 3 responses × $3 = $9, summed once. The pre-S2 fan-out reported 3×$9 = $27.
    assert abs(rows[0].cost - 9.0) < 1e-6
    assert rows[0].conversations == 1  # COUNT(DISTINCT conversation_id), not response count


def test_usage_by_workspace_cost_not_fanned_by_response_count(tmp_path):
    db = tmp_path / "w.db"
    ws, _ = _build_multi_response(db, n_responses=3)

    rows = get_usage_by_workspace(db_path=db)
    assert [r.name for r in rows] == ["/proj"]
    assert abs(rows[0].cost - 9.0) < 1e-6
    assert rows[0].conversations == 1


def test_summary_cost_and_tokens_not_fanned(tmp_path):
    db = tmp_path / "s.db"
    _build_multi_response(db, n_responses=3)

    s = get_usage_summary(db_path=db)
    assert s.total_conversations == 1
    assert s.total_input_tokens == 3_000_000  # 3 × 1M, summed not fanned
    assert abs(s.total_cost - 9.0) < 1e-6


def test_workspace_detail_model_mix_cost_filled_and_headline_is_sum(tmp_path):
    db = tmp_path / "d.db"
    ws, _ = _build_multi_response(db, n_responses=3)

    d = workspace_detail(ws, fidelity=_F, db_path=db)
    assert d is not None
    assert len(d.model_mix) == 1
    # model_mix cost was a hardcoded 0.0 before S2.
    assert abs(d.model_mix[0].cost - 9.0) < 1e-6
    # Headline is the sum of the mix — it can never disagree with the rows.
    assert abs(d.cost - sum(g.cost for g in d.model_mix)) < 1e-9
    assert abs(d.cost - 9.0) < 1e-6


def _build_two_owners(db_path):
    """alice (claude_code: 2 responses, 1 token-bearing) + bob (aider: 1
    token-bearing response), ownership recorded so owner-scoped coverage reads
    run against genuinely-built rollup rows (not hand-inserted)."""
    conn = sq.create_database(db_path)
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    hA = sq.get_or_create_harness(conn, "claude_code", source="anthropic")
    hB = sq.get_or_create_harness(conn, "aider", source="anthropic")
    m = sq.get_or_create_model(conn, "claude-x")
    p = sq.get_or_create_provider(conn, "anthropic")
    cA = sq.insert_conversation(conn, "cA", hA, ws, "2024-01-01T00:00:00Z")
    pA = sq.insert_prompt(conn, cA, "pA", "2024-01-01T00:00:00Z")
    sq.insert_response(conn, cA, pA, m, p, "rA1", "2024-01-01T00:00:01Z", 100, 200)
    sq.insert_response(conn, cA, pA, m, p, "rA2", "2024-01-01T00:00:02Z", None, None)
    cB = sq.insert_conversation(conn, "cB", hB, ws, "2024-01-02T00:00:00Z")
    pB = sq.insert_prompt(conn, cB, "pB", "2024-01-02T00:00:00Z")
    sq.insert_response(conn, cB, pB, m, p, "rB1", "2024-01-02T00:00:01Z", 50, 50)
    rebuild_rollups(conn)
    conn.executemany(
        "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at) "
        "VALUES (?, ?, NULL, ?)",
        [(cA, "alice", "2024-01-01T00:00:00Z"), (cB, "bob", "2024-01-02T00:00:00Z")],
    )
    conn.commit()
    conn.close()


def test_token_coverage_scopes_to_owner(tmp_path):
    """fetch_response_token_coverage / fetch_token_coverage_by_harness (S3: now
    GROUP BYs over usage_by_conv_model) scope to owner and count token presence
    via responses_with_tokens. Live parity is owner=None only (no owners table on
    the real DB), so this is the discriminating owner-scoped exercise."""
    from siftd.storage import queries as q
    from siftd.storage.sqlite import open_database

    db = tmp_path / "o.db"
    _build_two_owners(db)
    conn = open_database(db, read_only=True)
    try:
        # (response_count, responses_with_tokens): rA2 has no tokens → 2 vs 1.
        assert q.fetch_response_token_coverage(conn, owner="alice") == (2, 1)
        assert q.fetch_response_token_coverage(conn, owner="bob") == (1, 1)
        assert q.fetch_response_token_coverage(conn) == (3, 2)  # unscoped: both

        def _by_harness(owner):
            return {
                r["harness"]: (r["responses"], r["with_tokens"])
                for r in q.fetch_token_coverage_by_harness(conn, owner=owner)
            }

        assert _by_harness("alice") == {"claude_code": (2, 1)}
        assert _by_harness(None) == {"claude_code": (2, 1), "aider": (1, 1)}
    finally:
        conn.close()
