"""Owner-scoping regression tests for the /stats usage breakdowns.

These fns (`get_usage_summary`, `get_usage_by_model`, `get_usage_by_workspace`,
`get_cost_coverage`) were owner-blind — the serve `/stats` route showed
cross-owner token/cost totals on a multi-tenant server. Each now takes an
``owner`` and scopes via ``owner_predicate``; ``owner=None`` stays unscoped (the
single-tenant/local default). The isolation assertions below fail against the
pre-fix owner-blind versions.
"""

import sqlite3

import pytest

from siftd.api.stats import (
    get_cost_coverage,
    get_usage_by_model,
    get_usage_by_workspace,
    get_usage_summary,
)

ALICE = "alice"
BOB = "bob"


def _seed(db, *, with_owners: bool) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT);"
        "CREATE TABLE models (id TEXT PRIMARY KEY, raw_name TEXT, name TEXT);"
        "CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT);"
        "CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT, conversation_id TEXT,"
        " parent_id TEXT, external_id TEXT, timestamp TEXT);"
        "CREATE TABLE event_response (event_id TEXT PRIMARY KEY, model_id TEXT,"
        " provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER);"
        "CREATE TABLE conversation_stats (conversation_id TEXT, cost REAL, total_tokens INTEGER);"
        # Alice's conversation
        "INSERT INTO models VALUES ('mA','model-a','model-a');"
        "INSERT INTO workspaces VALUES ('wA','/tmp/wsA');"
        "INSERT INTO conversations VALUES ('cA','wA');"
        "INSERT INTO events VALUES ('eA','response','cA',NULL,NULL,'2024-01-01T00:00:00Z');"
        "INSERT INTO event_response VALUES ('eA','mA',NULL,10,20);"
        "INSERT INTO conversation_stats VALUES ('cA',1.5,30);"
        # Bob's conversation
        "INSERT INTO models VALUES ('mB','model-b','model-b');"
        "INSERT INTO workspaces VALUES ('wB','/tmp/wsB');"
        "INSERT INTO conversations VALUES ('cB','wB');"
        "INSERT INTO events VALUES ('eB','response','cB',NULL,NULL,'2024-01-02T00:00:00Z');"
        "INSERT INTO event_response VALUES ('eB','mB',NULL,100,200);"
        "INSERT INTO conversation_stats VALUES ('cB',9.0,300);"
    )
    if with_owners:
        conn.executescript(
            "CREATE TABLE conversation_owners (conversation_id TEXT, user_id TEXT,"
            " push_id TEXT, assigned_at TEXT);"
            "INSERT INTO conversation_owners VALUES ('cA','alice',NULL,'2024-01-01T00:00:00Z');"
            "INSERT INTO conversation_owners VALUES ('cB','bob',NULL,'2024-01-02T00:00:00Z');"
        )
    conn.commit()
    conn.close()


def test_usage_summary_scopes_to_owner(tmp_path):
    db = tmp_path / "u.db"
    _seed(db, with_owners=True)

    alice = get_usage_summary(db_path=db, owner=ALICE)
    assert alice.total_conversations == 1
    assert alice.total_input_tokens == 10
    assert alice.total_output_tokens == 20
    assert alice.total_cost == 1.5

    # Unscoped default sees everything (byte-identical to pre-fix behavior).
    both = get_usage_summary(db_path=db)
    assert both.total_conversations == 2
    assert both.total_cost == 10.5


def test_usage_by_model_and_workspace_scope_to_owner(tmp_path):
    db = tmp_path / "u.db"
    _seed(db, with_owners=True)

    models = get_usage_by_model(db_path=db, owner=ALICE)
    assert [m.name for m in models] == ["model-a"]

    ws = get_usage_by_workspace(db_path=db, owner=ALICE)
    assert [w.name for w in ws] == ["/tmp/wsA"]
    assert ws[0].cost == 1.5


def test_cost_coverage_scopes_to_owner(tmp_path):
    db = tmp_path / "u.db"
    _seed(db, with_owners=True)

    cc = get_cost_coverage(db_path=db, owner=ALICE)
    assert cc is not None
    assert cc.total_with_tokens == 1
    assert cc.with_positive_cost == 1

    assert get_cost_coverage(db_path=db).total_with_tokens == 2


@pytest.mark.parametrize(
    "fn",
    [get_usage_by_model, get_usage_by_workspace],
)
def test_owner_set_but_no_owners_table_returns_empty(tmp_path, fn):
    # Pre-migration DB: owner requested but no conversation_owners table.
    # Safe behavior is "nothing attributable to this owner", never unscoped data.
    db = tmp_path / "noowners.db"
    _seed(db, with_owners=False)
    assert fn(db_path=db, owner=ALICE) == []


def test_summary_and_coverage_no_owners_table_returns_zero(tmp_path):
    db = tmp_path / "noowners.db"
    _seed(db, with_owners=False)
    assert get_usage_summary(db_path=db, owner=ALICE).total_conversations == 0
    assert get_cost_coverage(db_path=db, owner=ALICE).total_with_tokens == 0
