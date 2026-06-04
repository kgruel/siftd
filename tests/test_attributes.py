"""Tests for storage/attributes.py — set_attribute / get_attributes and downstream consumers."""

import sqlite3
import time
from importlib.resources import files
from pathlib import Path

import pytest
from conftest import make_db as _make_db

from siftd.ids import ulid as _ulid
from siftd.storage.attributes import get_attributes, set_attribute
from siftd.storage.sql_helpers import cost_expr_sql
from siftd.storage.sqlite import open_database


@pytest.fixture()
def db(tmp_path):
    conn = open_database(tmp_path / "attrs.db")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 1. Roundtrip
# ---------------------------------------------------------------------------

def test_set_attribute_roundtrip(db):
    eid = _ulid()
    # attributes has no FK on target_id — no parent row required
    set_attribute(db, "response", eid, "cost", "1.23", scope="provider")
    rows = get_attributes(db, "response", eid)

    assert len(rows) == 1
    assert rows[0]["key"] == "cost"
    assert rows[0]["value"] == "1.23"
    assert rows[0]["scope"] == "provider"
    assert rows[0]["target_kind"] == "response"
    assert rows[0]["target_id"] == eid


# ---------------------------------------------------------------------------
# 2. UPSERT semantics — last write wins, no duplicate row
# ---------------------------------------------------------------------------

def test_set_attribute_upsert(db):
    eid = _ulid()
    set_attribute(db, "response", eid, "cost", "first", scope="provider")
    set_attribute(db, "response", eid, "cost", "second", scope="provider")

    rows = get_attributes(db, "response", eid)
    assert len(rows) == 1
    assert rows[0]["value"] == "second"


# ---------------------------------------------------------------------------
# 3. Migration backfill continuity
# Rows inserted directly into attributes (as slice-1 backfill did) are
# readable via get_attributes without any additional migration step.
# ---------------------------------------------------------------------------

def test_migration_backfill_continuity(db):
    eid = _ulid()
    # Insert directly, bypassing set_attribute — simulates slice-1 backfill
    db.execute(
        "INSERT INTO attributes (id, target_kind, target_id, key, value, scope) "
        "VALUES (?, 'response', ?, 'cache_read_input_tokens', '500', 'provider')",
        (_ulid(), eid),
    )

    rows = get_attributes(db, "response", eid)
    assert len(rows) == 1
    assert rows[0]["key"] == "cache_read_input_tokens"
    assert rows[0]["value"] == "500"


# ---------------------------------------------------------------------------
# 4. Cost parity — cost_expr_sql() and cost.sql both produce 0.0099
#
# Fixture: source='anthropic' (exclusive convention → input is uncached, cache is
#          additive), input_tokens=1000, output_tokens=500, cache_read=200,
#          cache_creation=0; input_per_mtok=3.0, output_per_mtok=15.0
# Expected (v10, 4 components, cache read @0.1× input rate):
#   (1000*3 + 200*0.3 + 500*15) / 1_000_000 = 10560 / 1_000_000 = 0.01056
# ---------------------------------------------------------------------------

def _seed_cost_fixture(conn):
    """Insert minimal rows needed by both cost_expr_sql and cost.sql."""
    harness_id = _ulid()
    ws_id = _ulid()
    conv_id = _ulid()
    model_id = _ulid()
    provider_id = _ulid()
    event_id = _ulid()

    conn.execute("INSERT INTO harnesses (id, name, source, log_format) VALUES (?, 'cc', 'anthropic', 'jsonl')", (harness_id,))
    conn.execute("INSERT INTO workspaces (id, path, discovered_at) VALUES (?, '/p', '2024-01-01')", (ws_id,))
    conn.execute("INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at) VALUES (?, 'c1', ?, ?, '2024-01-01')",
                 (conv_id, harness_id, ws_id))
    conn.execute("INSERT INTO models (id, raw_name, name) VALUES (?, 'test-model', 'test-model')", (model_id,))
    conn.execute("INSERT INTO providers (id, name) VALUES (?, 'test')", (provider_id,))
    conn.execute("INSERT INTO pricing (model_id, provider_id, input_per_mtok, output_per_mtok) VALUES (?, ?, 3.0, 15.0)",
                 (model_id, provider_id))

    conn.execute("INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, 'response', ?, '2024-01-01')",
                 (event_id, conv_id))
    conn.execute("INSERT INTO event_response (event_id, model_id, provider_id, input_tokens, output_tokens) VALUES (?, ?, ?, 1000, 500)",
                 (event_id, model_id, provider_id))

    set_attribute(conn, "response", event_id, "cache_read_input_tokens", "200", scope="provider")

    conn.commit()
    return event_id, conv_id


def test_cost_expr_sql_parity(tmp_path):
    """Both cost_expr_sql() and cost.sql produce the same value on the same fixture."""
    conn = open_database(tmp_path / "cost.db")
    event_id, conv_id = _seed_cost_fixture(conn)

    # Path A: cost_expr_sql() — the flatten must expose source + the two cache
    # components (the v10 contract), matching rebuild_usage_by_conv_model. Fixture
    # is source='anthropic' (exclusive): uncached=input=1000, cache_read=200 ADDITIVE.
    # cost = 1000*3 + 0*ccreate + 200*(3*0.1) + 500*15 = 3000 + 0 + 60 + 7500 = 10560 → 0.01056
    cost_expr = cost_expr_sql("r", "pr", coalesce_pricing=True)
    path_a = conn.execute(f"""
        SELECT ROUND(SUM({cost_expr}) / 1000000.0, 4) AS cost
        FROM (
            SELECT e.id, e.conversation_id, er.input_tokens, er.output_tokens,
                   er.model_id, er.provider_id, h.source AS source,
                   COALESCE((SELECT MAX(CAST(value AS INTEGER)) FROM attributes a
                     WHERE a.target_kind='response' AND a.target_id=e.id
                     AND a.key='cache_read_input_tokens'), 0) AS cache_read,
                   COALESCE((SELECT MAX(CAST(value AS INTEGER)) FROM attributes a
                     WHERE a.target_kind='response' AND a.target_id=e.id
                     AND a.key='cache_creation_input_tokens'), 0) AS cache_creation
            FROM events e JOIN event_response er ON er.event_id = e.id
            LEFT JOIN conversations c ON c.id = e.conversation_id
            LEFT JOIN harnesses h ON h.id = c.harness_id
            WHERE e.kind = 'response'
        ) r
        LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id
        WHERE r.id = ?
    """, (event_id,)).fetchone()[0]

    assert path_a == pytest.approx(0.0106, abs=1e-6), f"cost_expr_sql path gave {path_a}"

    # Path B: cost.sql builtin query — must agree with cost_expr_sql to the cent.
    cost_sql = files("siftd.builtin_queries").joinpath("cost.sql").read_text()
    cost_sql_runnable = cost_sql.replace("$limit", "100").replace("LIMIT $limit", "LIMIT 100")
    rows = conn.execute(cost_sql_runnable).fetchall()
    assert len(rows) == 1
    path_b = rows[0]["approx_cost_usd"]

    assert path_b == pytest.approx(0.0106, abs=1e-6), f"cost.sql path gave {path_b}"

    conn.close()


# ---------------------------------------------------------------------------
# 5. Merge copy — attributes from source appear in target after merge
# ---------------------------------------------------------------------------

def test_merge_copies_attributes(tmp_path):
    from siftd.api.merge import merge_database

    source_path = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Hello"}],
    )

    # Add attributes to source's response event
    src_conn = open_database(source_path)
    event_id = src_conn.execute(
        "SELECT id FROM events WHERE kind = 'response' LIMIT 1"
    ).fetchone()["id"]
    set_attribute(src_conn, "response", event_id, "cache_read_input_tokens", "42", scope="provider")
    src_conn.commit()
    src_conn.close()

    # Empty target
    target_path = tmp_path / "target.db"
    t_conn = open_database(target_path)
    t_conn.close()

    merge_database(target_path, source_path)

    t_conn = open_database(target_path)
    rows = t_conn.execute(
        "SELECT * FROM attributes WHERE target_kind='response' AND key='cache_read_input_tokens'"
    ).fetchall()
    t_conn.close()

    assert len(rows) == 1
    assert rows[0]["value"] == "42"
    assert rows[0]["target_id"] == event_id


# ---------------------------------------------------------------------------
# 6. Merge stale replacement — old attributes deleted, source's attributes
#    are present in target after replacing a stale conversation.
# ---------------------------------------------------------------------------

def test_merge_stale_replacement_clears_attributes(tmp_path):
    from siftd.api.merge import merge_database

    target_path = _make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "Old"}],
    )

    # Seed attributes for the old conversation's response event in target
    tgt_conn = open_database(target_path)
    old_event_id = tgt_conn.execute(
        "SELECT id FROM events WHERE kind = 'response' LIMIT 1"
    ).fetchone()["id"]
    set_attribute(tgt_conn, "response", old_event_id, "cache_read_input_tokens", "99", scope="provider")
    old_conv_id = tgt_conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    set_attribute(tgt_conn, "conversation", old_conv_id, "summary", "stale", scope="analyzer")
    tgt_conn.commit()
    tgt_conn.close()

    # Verify seed
    tgt_conn = open_database(target_path)
    assert tgt_conn.execute("SELECT COUNT(*) FROM attributes").fetchone()[0] == 2
    tgt_conn.close()

    time.sleep(0.01)  # ensure source has a later started_at

    source_path = _make_db(
        tmp_path / "source.db",
        conversations=[{"external_id": "conv-1", "prompt_text": "New"}],
    )

    result = merge_database(target_path, source_path)
    assert result["replaced_conversations"] == 1

    tgt_conn = open_database(target_path)
    # Old attributes (response + conversation level) must be gone
    assert tgt_conn.execute(
        "SELECT COUNT(*) FROM attributes WHERE target_id = ?", (old_event_id,)
    ).fetchone()[0] == 0
    assert tgt_conn.execute(
        "SELECT COUNT(*) FROM attributes WHERE target_id = ?", (old_conv_id,)
    ).fetchone()[0] == 0
    tgt_conn.close()
