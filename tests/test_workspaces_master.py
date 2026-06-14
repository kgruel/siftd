"""Base-lane tests for the Workspaces master list + detail cost honesty.

Covers the data the Workspaces view consumes:
  - ``list_workspaces(with_usage=True)`` adds rollup tokens + *honest* cost
    (None when a workspace has no priced usage, never a fabricated $0) and stays
    grouped at the workspace ULID so two workspaces sharing a git remote (the
    legacy duplicate "twin") remain distinct, drillable rows.
  - ``workspace_detail`` headline cost is None (not 0.0) when no model in the
    workspace is priced — the detail twin of the dashboard-headline em-dash rule.

No serve/litestar dependency: these are core api reads.
"""

from painted import Fidelity

from siftd.api.stats import list_workspaces, workspace_detail
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


def _conv(conn, harness, model, ws, ext, started, inp, out):
    cid = insert_conversation(
        conn, external_id=ext, harness_id=harness, workspace_id=ws, started_at=started,
    )
    pid = insert_prompt(conn, cid, ext + "p", started)
    insert_prompt_content(conn, pid, 0, "text", '{"text": "hi"}')
    insert_response(
        conn, cid, pid, model, None, ext + "r", started,
        input_tokens=inp, output_tokens=out,
    )
    return cid


def _price(conn, conv_id, amount):
    """Stamp a cost on a conversation's rollup rows (simulates priced usage).

    The rollup derives cost from the pricing table on rebuild; these tests don't
    set pricing up, so a direct stamp is the surgical way to exercise the
    priced-vs-unpriced branch of the read fns.
    """
    conn.execute("UPDATE usage_by_conv_model SET cost = ? WHERE conversation_id = ?", (amount, conv_id))


def _build(db_path, *, price=False):
    conn = create_database(db_path)
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    model = get_or_create_model(conn, "claude-3-opus")
    ws_a = get_or_create_workspace(conn, "/test/projA", "2024-01-01T00:00:00Z")
    ws_b = get_or_create_workspace(conn, "/test/projB", "2024-01-01T00:00:00Z")
    a1 = _conv(conn, harness, model, ws_a, "cA1", "2024-01-15T10:00:00Z", 100, 50)
    _conv(conn, harness, model, ws_a, "cA2", "2024-01-16T10:00:00Z", 1000, 500)
    _conv(conn, harness, model, ws_b, "cB1", "2024-01-17T10:00:00Z", 7, 3)
    rebuild_rollups(conn)
    if price:
        _price(conn, a1, 1.25)  # only workspace A is priced; B stays unpriced
    conn.commit()
    conn.close()
    return ws_a, ws_b


def _build_twin(db_path):
    """Two workspaces sharing a git remote at different paths — the legacy
    duplicate the migrate path collapses, inserted directly because
    get_or_create_workspace dedups new ingests by remote."""
    conn = create_database(db_path)
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    model = get_or_create_model(conn, "claude-3-opus")
    twin_a = "01TWINAAAAAAAAAAAAAAAAAAAA"
    twin_b = "01TWINBBBBBBBBBBBBBBBBBBBB"
    for wid, path in ((twin_a, "/code/painted"), (twin_b, "/code/loops/libs/painted")):
        conn.execute(
            "INSERT INTO workspaces (id, path, git_remote, discovered_at) VALUES (?,?,?,?)",
            (wid, path, "git@example.com:painted.git", "2024-01-01T00:00:00Z"),
        )
    _conv(conn, harness, model, twin_a, "p1", "2024-01-15T10:00:00Z", 100, 50)
    _conv(conn, harness, model, twin_b, "p2", "2024-01-16T10:00:00Z", 7, 3)
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    return twin_a, twin_b


# --- master list (with_usage) ----------------------------------------------

def test_list_workspaces_with_usage_adds_tokens_and_honest_cost(tmp_path):
    db = tmp_path / "d.db"
    _build(db, price=True)

    rows = list_workspaces(db_path=db, n=10, with_usage=True)
    by_path = {r["path"]: r for r in rows}

    # Workspace A is priced → a real float; B has no priced usage → None ("—"),
    # never a fabricated $0.
    assert by_path["/test/projA"]["cost"] == 1.25
    assert by_path["/test/projB"]["cost"] is None
    # Tokens come through summed from the rollup.
    assert by_path["/test/projA"]["inp"] == 1100
    assert by_path["/test/projB"]["out"] == 3


def test_list_workspaces_without_usage_omits_usage_columns(tmp_path):
    """Default stays the lean query — the name-only callers don't pay the join."""
    db = tmp_path / "d.db"
    _build(db)

    rows = list_workspaces(db_path=db, n=10)
    assert rows
    assert "cost" not in rows[0].keys()
    assert "convs" in rows[0].keys()  # the existing columns are unchanged


def test_list_workspaces_with_usage_keeps_remote_twins_distinct(tmp_path):
    """ULID-grouped, so two workspaces sharing a git remote stay separate rows —
    each can drill into its own detail (the path-grouped dashboard mix would
    collapse same-path; ULID grouping never silently merges identities)."""
    db = tmp_path / "twin.db"
    twin_a, twin_b = _build_twin(db)

    rows = list_workspaces(db_path=db, n=10, with_usage=True)
    ids = {r["id"] for r in rows}
    assert twin_a in ids and twin_b in ids  # both twins present, not merged


# --- detail cost honesty ----------------------------------------------------

def test_workspace_detail_cost_none_when_unpriced(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)  # no pricing

    d = workspace_detail(ws_a, fidelity=_F, db_path=db)
    assert d is not None
    assert d.cost is None  # not a fabricated 0.0
    assert all(g.cost is None for g in d.model_mix)


def test_workspace_detail_cost_summed_when_priced(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db, price=True)

    d = workspace_detail(ws_a, fidelity=_F, db_path=db)
    assert d is not None
    assert d.cost == 1.25  # the one priced conversation's cost, no fabrication
