"""Behavioral tests for the workspace-detail Operation (api.stats.workspace_detail).

Built on a real schema via the storage builders so the reused list_conversations
(recent sessions) runs for real. Verifies the stat grid + by-model mix + recent,
ULID addressing, owner-scoping, and None on unknown id.
"""

from painted import Fidelity

from siftd.api.stats import workspace_detail
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
)
from siftd.storage.usage_rollup import rebuild_rollups

_F = Fidelity()


def _build(db_path):
    conn = create_database(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    model = get_or_create_model(conn, "claude-3-opus")
    ws_a = get_or_create_workspace(conn, "/test/projA", "2024-01-01T00:00:00Z")
    ws_b = get_or_create_workspace(conn, "/test/projB", "2024-01-01T00:00:00Z")

    def _conv(ext, ws, started, owner, inp, out):
        cid = insert_conversation(
            conn, external_id=ext, harness_id=harness, workspace_id=ws, started_at=started,
        )
        pid = insert_prompt(conn, cid, ext + "p", started)
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hi"}')
        insert_response(
            conn, cid, pid, model, None, ext + "r", started,
            input_tokens=inp, output_tokens=out,
        )
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, started),
        )
        return cid

    _conv("cA1", ws_a, "2024-01-15T10:00:00Z", "alice", 100, 50)
    _conv("cA2", ws_a, "2024-01-16T10:00:00Z", "bob", 1000, 500)
    _conv("cB1", ws_b, "2024-01-17T10:00:00Z", "alice", 7, 3)
    # model_mix + token/cost aggregates now read the rollup, so build it (as a
    # real ingest would) before the read fns run.
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    return ws_a, ws_b


def test_workspace_detail_unscoped_aggregates_whole_workspace(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)

    d = workspace_detail(ws_a, fidelity=_F, db_path=db)
    assert d is not None
    assert d.id == ws_a
    assert d.path == "/test/projA"
    assert d.sessions == 2
    assert d.input_tokens == 1100  # 100 + 1000
    assert d.output_tokens == 550  # 50 + 500
    assert {g.name for g in d.model_mix} == {"claude-3-opus"}
    assert len(d.recent) == 2


def test_workspace_detail_scopes_to_owner(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)

    d = workspace_detail(ws_a, fidelity=_F, db_path=db, owner="alice")
    assert d is not None
    # Only Alice's conversation in workspace A (cA1), not Bob's (cA2).
    assert d.sessions == 1
    assert d.input_tokens == 100
    assert d.output_tokens == 50
    assert len(d.recent) == 1


def test_workspace_detail_unknown_id_returns_none(tmp_path):
    db = tmp_path / "d.db"
    _build(db)
    assert workspace_detail("01HNOPE", fidelity=_F, db_path=db) is None


def test_workspace_detail_carries_scoped_cadence(tmp_path):
    """The cadence strip is the daily series scoped to the workspace, so its
    token sum reconciles to the workspace's tokens (whole-workspace, unscoped)."""
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)
    d = workspace_detail(ws_a, fidelity=_F, db_path=db)
    assert d is not None
    assert d.cadence  # a non-empty daily series
    assert sum(b.tokens for b in d.cadence) == d.input_tokens + d.output_tokens
    # Gap-filled: a strictly increasing, contiguous run of ISO days.
    from datetime import date

    days = [date.fromisoformat(b.label) for b in d.cadence]
    assert days == sorted(days)
    for prev, nxt in zip(days, days[1:], strict=False):
        assert (nxt - prev).days == 1


def test_usage_distributions_shape_totals_and_scoping(tmp_path):
    from siftd.api.stats import get_usage_distributions

    db = tmp_path / "d.db"
    _ws_a, ws_b = _build(db)

    glob = get_usage_distributions(db_path=db)
    assert len(glob.by_hour) == 24 and len(glob.by_dow) == 7
    # Whole corpus: 1650 (ws_a) + 10 (ws_b).
    assert sum(b.tokens for b in glob.by_day) == 1660
    assert sum(b.tokens for b in glob.by_hour) == 1660
    assert sum(b.tokens for b in glob.by_dow) == 1660

    # Workspace-scoped (the cadence source) sums to that workspace only.
    only_b = get_usage_distributions(db_path=db, workspace_id=ws_b)
    assert sum(b.tokens for b in only_b.by_day) == 10

    # Owner-scoped to alice: cA1 (150 in ws_a) + cB1 (10 in ws_b) = 160.
    alice = get_usage_distributions(db_path=db, owner="alice")
    assert sum(b.tokens for b in alice.by_day) == 160

    # Model-scoped (the chart-brushing source): the fixture uses one model, so
    # scoping to it = the whole corpus; an unknown model = empty.
    only_model = get_usage_distributions(db_path=db, model_name="claude-3-opus")
    assert sum(b.tokens for b in only_model.by_day) == 1660
    nope = get_usage_distributions(db_path=db, model_name="no-such-model")
    assert sum(b.tokens for b in nope.by_day) == 0


def test_input_economy_owner_and_model_scoping(tmp_path):
    """get_input_economy sums the true-total input + cache components, scopable by
    owner and model (so the strip follows the brush). This fixture seeds no cache,
    so has_cache is False, but the input total scopes correctly."""
    from siftd.api.stats import get_input_economy

    db = tmp_path / "d.db"
    _build(db)
    glob = get_input_economy(db_path=db)
    assert glob.input_tokens == 1107  # 100 + 1000 + 7 (input only)
    assert glob.cache_read_tokens == 0 and not glob.has_cache
    assert glob.uncached_tokens == 1107  # no cache → all uncached
    # owner-scoped (alice: cA1 input 100 + cB1 input 7 = 107)
    assert get_input_economy(db_path=db, owner="alice").input_tokens == 107
    # model-scoped to the one model = whole corpus; unknown model = empty
    assert get_input_economy(db_path=db, model_name="claude-3-opus").input_tokens == 1107
    assert get_input_economy(db_path=db, model_name="ghost").input_tokens == 0


def test_usage_summary_carries_cache_totals(tmp_path):
    """get_usage_summary sums the rollup's broken-out cache components (the
    input-economy source). This fixture seeds no cache tokens, so they're 0 —
    proving the columns resolve and default honestly, not that cache is absent."""
    from siftd.api.stats import get_usage_summary

    db = tmp_path / "d.db"
    _build(db)
    u = get_usage_summary(db_path=db)
    assert u.total_cache_read_tokens == 0
    assert u.total_cache_creation_tokens == 0
    assert u.total_input_tokens == 1107  # 100 + 1000 + 7
