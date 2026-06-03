"""Tests for the usage_by_conv_model rollup — the keystone derived tier.

Acceptance criteria from docs/dev/plans/2026-06-02-rollup-layer.md (S1):
- cost lives at (conv, model, provider) grain — summed, never fanned by response
  count (the 290x regression, structurally impossible here);
- provider is in the grain (a conv×model spanning two providers → two rows);
- responses_with_tokens distinguishes present-but-zero from both-null tokens;
- conversation_stats is re-derived from the rollup (parity against hand truth);
- model_name is the response-count argmax (family label).
"""

import siftd.storage.sqlite as sq
from siftd.storage.usage_rollup import rebuild_rollups, rebuild_usage_by_conv_model


def _db(tmp_path):
    return sq.create_database(tmp_path / "rollup.db")


def _seed_pricing(conn, model_id, provider_id, in_rate, out_rate, pid="pr-test"):
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, model_id, provider_id, in_rate, out_rate),
    )


def _conv(conn, harness_id, ws_id, ext):
    cid = sq.insert_conversation(conn, ext, harness_id, ws_id, "2024-01-01T00:00:00Z")
    pid = sq.insert_prompt(conn, cid, f"p-{ext}", "2024-01-01T00:00:00Z")
    return cid, pid


class TestGrain:
    def test_provider_in_grain_splits_rows(self, tmp_path):
        """A (conv, model) that spans two providers yields two rollup rows."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="anthropic")
        m = sq.get_or_create_model(conn, "claude-x")
        pa = sq.get_or_create_provider(conn, "anthropic")
        pb = sq.get_or_create_provider(conn, "bedrock")
        cid, pid = _conv(conn, h, ws, "c1")
        sq.insert_response(conn, cid, pid, m, pa, "r1", "2024-01-01T00:00:01Z", 100, 50)
        sq.insert_response(conn, cid, pid, m, pb, "r2", "2024-01-01T00:00:02Z", 200, 60)
        conn.commit()

        rebuild_usage_by_conv_model(conn, commit=True)
        rows = conn.execute(
            "SELECT provider_id, response_count, input_tokens FROM usage_by_conv_model "
            "WHERE conversation_id=? ORDER BY input_tokens",
            (cid,),
        ).fetchall()
        assert len(rows) == 2  # one row per provider, same model
        by_prov = {r["provider_id"]: r for r in rows}
        assert by_prov[pa]["input_tokens"] == 100 and by_prov[pa]["response_count"] == 1
        assert by_prov[pb]["input_tokens"] == 200 and by_prov[pb]["response_count"] == 1

    def test_responses_with_tokens_zero_vs_null(self, tmp_path):
        """Present-but-zero tokens count as covered; both-null does not."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="x")
        m = sq.get_or_create_model(conn, "m")
        p = sq.get_or_create_provider(conn, "prov")
        cid, pid = _conv(conn, h, ws, "c1")
        sq.insert_response(conn, cid, pid, m, p, "r0", "2024-01-01T00:00:01Z", 0, 0)
        sq.insert_response(conn, cid, pid, m, p, "rN", "2024-01-01T00:00:02Z", None, None)
        conn.commit()

        rebuild_usage_by_conv_model(conn, commit=True)
        row = conn.execute(
            "SELECT response_count, responses_with_tokens FROM usage_by_conv_model "
            "WHERE conversation_id=?",
            (cid,),
        ).fetchone()
        assert row["response_count"] == 2
        assert row["responses_with_tokens"] == 1  # the (0,0) response, not the (NULL,NULL)

    def test_rebuild_idempotent(self, tmp_path):
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="x")
        m = sq.get_or_create_model(conn, "m")
        p = sq.get_or_create_provider(conn, "prov")
        cid, pid = _conv(conn, h, ws, "c1")
        sq.insert_response(conn, cid, pid, m, p, "r1", "2024-01-01T00:00:01Z", 100, 50)
        conn.commit()
        n1 = rebuild_usage_by_conv_model(conn, commit=True)
        n2 = rebuild_usage_by_conv_model(conn, commit=True)
        assert n1 == n2 == 1


class TestCost:
    def test_no_fanout_cost_is_sum_not_fanned(self, tmp_path):
        """Cost at (conv,model,provider) grain = sum of per-response costs, never
        multiplied by response count (the 290x fan-out, structurally)."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="anthropic")
        m = sq.get_or_create_model(conn, "claude-x")
        p = sq.get_or_create_provider(conn, "anthropic")
        _seed_pricing(conn, m, p, 3.0, 15.0)  # $3/Mtok in, $15/Mtok out
        cid, pid = _conv(conn, h, ws, "c1")
        for i in range(3):  # 3 responses, each 1M input → $3 each → $9 total
            sq.insert_response(conn, cid, pid, m, p, f"r{i}", f"2024-01-01T00:00:0{i + 1}Z", 1_000_000, 0)
        conn.commit()

        rebuild_usage_by_conv_model(conn, commit=True)
        rows = conn.execute(
            "SELECT response_count, cost FROM usage_by_conv_model WHERE conversation_id=?",
            (cid,),
        ).fetchall()
        assert len(rows) == 1  # one (conv, model, provider) group
        assert rows[0]["response_count"] == 3
        assert abs(rows[0]["cost"] - 9.0) < 1e-9  # 3 × $3 summed, not fanned to $27 or $81

    def test_unpriced_cost_is_null(self, tmp_path):
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="unknown")
        m = sq.get_or_create_model(conn, "unpriced")
        cid, pid = _conv(conn, h, ws, "c1")
        sq.insert_response(conn, cid, pid, m, None, "r1", "2024-01-01T00:00:01Z", 1000, 500)
        conn.commit()
        rebuild_usage_by_conv_model(conn, commit=True)
        row = conn.execute(
            "SELECT cost FROM usage_by_conv_model WHERE conversation_id=?", (cid,)
        ).fetchone()
        assert row["cost"] is None  # NULL, not 0.0

    def test_harness_fallback_prices_null_provider(self, tmp_path):
        """provider_id NULL → priced via the harness source's provider; the grain
        keeps provider_id NULL (captured value), distinct from the price source."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        prov = sq.get_or_create_provider(conn, "anthropic")
        m = sq.get_or_create_model(conn, "claude-x")
        h = sq.get_or_create_harness(conn, "claude_code", source="anthropic")
        _seed_pricing(conn, m, prov, 3.0, 15.0)
        cid, pid = _conv(conn, h, ws, "c1")
        sq.insert_response(conn, cid, pid, m, None, "r1", "2024-01-01T00:00:01Z", 1_000_000, 0)
        conn.commit()
        rebuild_usage_by_conv_model(conn, commit=True)
        row = conn.execute(
            "SELECT provider_id, cost FROM usage_by_conv_model WHERE conversation_id=?",
            (cid,),
        ).fetchone()
        assert row["provider_id"] is None  # captured provider stays NULL
        assert abs(row["cost"] - 3.0) < 1e-9  # but priced via harness fallback


class TestConversationStatsDerived:
    def test_stats_match_rollup_sums_and_argmax(self, tmp_path):
        """conversation_stats is the rollup summed to conversation grain; cost,
        tokens, and the model_name argmax all match hand-computed truth."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="anthropic")
        ma = sq.get_or_create_model(conn, "model-a")
        mb = sq.get_or_create_model(conn, "model-b")
        p = sq.get_or_create_provider(conn, "anthropic")
        _seed_pricing(conn, ma, p, 3.0, 15.0)
        _seed_pricing(conn, mb, p, 3.0, 15.0, pid="pr-b")
        cid, pid = _conv(conn, h, ws, "c1")
        for i in range(3):  # model-a dominates (3 responses)
            sq.insert_response(conn, cid, pid, ma, p, f"ra{i}", f"2024-01-01T00:0{i}:01Z", 1000, 500)
        sq.insert_response(conn, cid, pid, mb, p, "rb", "2024-01-01T00:09:01Z", 2000, 100)
        conn.commit()

        rebuild_rollups(conn, commit=True)
        row = conn.execute(
            "SELECT prompt_count, response_count, total_tokens, model_name, cost "
            "FROM conversation_stats WHERE conversation_id=?",
            (cid,),
        ).fetchone()
        assert row["prompt_count"] == 1
        assert row["response_count"] == 4
        assert row["total_tokens"] == 6600  # 3*(1000+500) + (2000+100)
        assert row["model_name"] == "model-a"  # 3 responses > 1
        # model-a: (3000 in + 1500 out) micro→ a-in 3*1000*3=9000, a-out 3*500*15=22500
        # model-b: 2000*3=6000 in, 100*15=1500 out → total 39000 micro = $0.039
        assert abs(row["cost"] - 0.039) < 1e-6

    def test_stats_response_count_matches_response_events(self, tmp_path):
        """The rollup-derived response_count equals COUNT(response events)."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="x")
        m = sq.get_or_create_model(conn, "m")
        p = sq.get_or_create_provider(conn, "prov")
        cid, pid = _conv(conn, h, ws, "c1")
        for i in range(5):
            sq.insert_response(conn, cid, pid, m, p, f"r{i}", f"2024-01-01T00:00:0{i}Z", 10, 5)
        conn.commit()

        rebuild_rollups(conn, commit=True)
        stats_n = conn.execute(
            "SELECT response_count FROM conversation_stats WHERE conversation_id=?", (cid,)
        ).fetchone()["response_count"]
        events_n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='response' AND conversation_id=?", (cid,)
        ).fetchone()[0]
        assert stats_n == events_n == 5

    def test_model_name_prefers_named_over_null_on_tie(self, tmp_path):
        """On a response-count tie between a named model and NULL-model responses,
        model_name picks the NAMED model (NULL = 'unknown' is the least useful
        label). Guards the (m.name IS NULL) tiebreak: without it, NULL sorts first
        in SQLite and would win the tie."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="x")
        m = sq.get_or_create_model(conn, "known-model")
        p = sq.get_or_create_provider(conn, "prov")
        cid, pid = _conv(conn, h, ws, "c1")
        # 2 named-model + 2 NULL-model responses → tie on response_count
        sq.insert_response(conn, cid, pid, m, p, "r1", "2024-01-01T00:00:01Z", 10, 5)
        sq.insert_response(conn, cid, pid, m, p, "r2", "2024-01-01T00:00:02Z", 10, 5)
        sq.insert_response(conn, cid, pid, None, p, "r3", "2024-01-01T00:00:03Z", 10, 5)
        sq.insert_response(conn, cid, pid, None, p, "r4", "2024-01-01T00:00:04Z", 10, 5)
        conn.commit()

        rebuild_rollups(conn, commit=True)
        name = conn.execute(
            "SELECT model_name FROM conversation_stats WHERE conversation_id=?", (cid,)
        ).fetchone()["model_name"]
        assert name == "known-model"  # named beats the NULL group on a tie

    def test_model_name_null_when_all_responses_lack_model(self, tmp_path):
        """An all-NULL-model conversation still resolves to a NULL label (LEFT JOIN
        preserved — not dropped by switching to an inner JOIN)."""
        conn = _db(tmp_path)
        ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        h = sq.get_or_create_harness(conn, "tool", source="x")
        p = sq.get_or_create_provider(conn, "prov")
        cid, pid = _conv(conn, h, ws, "c1")
        sq.insert_response(conn, cid, pid, None, p, "r1", "2024-01-01T00:00:01Z", 10, 5)
        sq.insert_response(conn, cid, pid, None, p, "r2", "2024-01-01T00:00:02Z", 10, 5)
        conn.commit()

        rebuild_rollups(conn, commit=True)
        row = conn.execute(
            "SELECT model_name, response_count FROM conversation_stats WHERE conversation_id=?", (cid,)
        ).fetchone()
        assert row["model_name"] is None
        assert row["response_count"] == 2  # the responses are still counted
