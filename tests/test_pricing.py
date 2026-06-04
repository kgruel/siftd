"""v11 pricing-as-reference: the pricing table is a projection of the version-controlled
reference (siftd/data/pricing.toml + user override), UPSERT-applied on every open.

These cover the dissolution's load-bearing behaviors: the reference loads, the user
override wins, the projection CORRECTS a stale row (the born-frozen pathology the live
DB exhibited), an unknown model stays NULL (never a fabricated $0), the v11 migration
reprices a stale row, and merge no longer imports a source DB's prices.
"""

import sqlite3
from pathlib import Path

import siftd.pricing as pricing_mod
import siftd.storage.sqlite as sq
from siftd.pricing import load_pricing_reference
from siftd.storage.sqlite import open_database


def _price_of(conn, model_id):
    return conn.execute(
        "SELECT input_per_mtok, output_per_mtok, source FROM pricing WHERE model_id = ?",
        (model_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# 1. Reference loads and parses, with provenance
# ---------------------------------------------------------------------------

def test_reference_loads_with_provenance():
    entries = load_pricing_reference()
    by_key = {(e.model, e.provider): e for e in entries}
    opus = by_key[("claude-opus-4-5", "anthropic")]
    assert (opus.input_per_mtok, opus.output_per_mtok) == (5.0, 25.0)
    assert opus.source and opus.as_of  # provenance present


# ---------------------------------------------------------------------------
# 2. User override beats the shipped reference, per (model, provider)
# ---------------------------------------------------------------------------

def test_user_override_beats_reference(tmp_path, monkeypatch):
    override = tmp_path / "pricing.toml"
    override.write_text(
        '[[price]]\nmodel = "claude-opus-4-5"\nprovider = "anthropic"\n'
        'input_per_mtok = 99.0\noutput_per_mtok = 199.0\nsource = "me"\nas_of = "2026-06-03"\n'
    )
    monkeypatch.setattr(pricing_mod, "pricing_override_file", lambda: override)

    by_key = {(e.model, e.provider): e for e in load_pricing_reference()}
    assert by_key[("claude-opus-4-5", "anthropic")].input_per_mtok == 99.0
    # a non-overridden entry is untouched
    assert by_key[("claude-haiku-4-5", "anthropic")].input_per_mtok == 1.0


# ---------------------------------------------------------------------------
# 3. Projection UPSERT corrects a stale row (the born-frozen fix)
# ---------------------------------------------------------------------------

def test_projection_overwrites_stale_price(tmp_path):
    conn = sq.create_database(tmp_path / "t.db")
    m = sq.get_or_create_model(conn, "claude-opus-4-5-20251101")  # canonical: claude-opus-4-5
    p = sq.get_or_create_provider(conn, "anthropic")
    # A stale frozen price that disagrees with the reference (reference is 5/25).
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES (?, ?, ?, 99.0, 199.0)",
        (sq._ulid(), m, p),
    )
    sq.ensure_pricing_table(conn)  # reproject the reference
    row = _price_of(conn, m)
    assert (row["input_per_mtok"], row["output_per_mtok"]) == (5.0, 25.0)
    assert row["source"]  # provenance written
    conn.close()


# ---------------------------------------------------------------------------
# 4. Unknown model stays unpriced (NULL), never a fabricated $0
# ---------------------------------------------------------------------------

def test_unknown_model_stays_unpriced(tmp_path):
    conn = sq.create_database(tmp_path / "t.db")
    m = sq.get_or_create_model(conn, "totally-unknown-model-x")
    sq.get_or_create_provider(conn, "anthropic")
    sq.ensure_pricing_table(conn)
    assert _price_of(conn, m) is None  # no row → cost NULL downstream, not $0
    conn.close()


# ---------------------------------------------------------------------------
# 5. Dot-spelled model is priced once backfill canonicalizes its name
# ---------------------------------------------------------------------------

def test_dot_spelled_model_priced_after_canonicalize(tmp_path):
    conn = sq.create_database(tmp_path / "t.db")
    sq.get_or_create_provider(conn, "anthropic")  # needed for the reference JOIN
    # Force a fallback-named row as a pre-parser-fix DB would have stored it.
    mid = sq._ulid()
    conn.execute(
        "INSERT INTO models (id, raw_name, name) VALUES (?, 'claude-haiku-4.5', 'claude-haiku-4.5')",
        (mid,),
    )
    sq.ensure_pricing_table(conn)
    assert _price_of(conn, mid) is None  # not priced while name is the dot form

    from siftd.backfill import backfill_models
    backfill_models(conn, commit=False)  # canonicalize → claude-haiku-4-5
    sq.ensure_pricing_table(conn)
    row = _price_of(conn, mid)
    assert (row["input_per_mtok"], row["output_per_mtok"]) == (1.0, 5.0)
    conn.close()


# ---------------------------------------------------------------------------
# 5b. Two spellings → one canonical name: BOTH model_ids get priced, no PK clash
# ---------------------------------------------------------------------------

def test_two_spellings_one_name_both_priced(tmp_path):
    conn = sq.create_database(tmp_path / "t.db")
    m_dash = sq.get_or_create_model(conn, "claude-haiku-4-5")   # canonical claude-haiku-4-5
    m_dot = sq.get_or_create_model(conn, "claude-haiku-4.5")    # ALSO claude-haiku-4-5
    assert m_dash != m_dot  # distinct model_ids, one canonical name
    sq.get_or_create_provider(conn, "anthropic")
    sq.ensure_pricing_table(conn)  # must not raise on the shared-name fresh-insert
    for mid in (m_dash, m_dot):
        row = _price_of(conn, mid)
        assert (row["input_per_mtok"], row["output_per_mtok"]) == (1.0, 5.0)
    conn.close()


# ---------------------------------------------------------------------------
# 6. v11 migration reprices a stale row on a v10 DB
# ---------------------------------------------------------------------------

def test_v11_migration_reprices_stale_row(tmp_path):
    fixture = Path("tests/fixtures/schemas/v10.sql")
    db = tmp_path / "v10.db"
    raw = sqlite3.connect(db)
    raw.executescript(fixture.read_text())
    # A v10 DB with a stale opus-4-5 price that disagrees with the reference (5/25).
    raw.execute(
        "INSERT INTO models (id, raw_name, name, creator, family, version, variant) "
        "VALUES ('m1', 'claude-opus-4-5-20251101', 'claude-opus-4-5', 'anthropic', 'claude', '4.5', 'opus')"
    )
    raw.execute("INSERT INTO providers (id, name) VALUES ('p1', 'anthropic')")
    raw.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES ('pr1', 'm1', 'p1', 99.0, 199.0)"
    )
    raw.commit()
    raw.close()

    conn = open_database(db)  # triggers v10 → v11 migration
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        row = _price_of(conn, "m1")
        assert (row["input_per_mtok"], row["output_per_mtok"]) == (5.0, 25.0)
        assert row["source"]  # provenance attached by the reprojection
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. Merge no longer imports the source DB's prices (contamination gone)
# ---------------------------------------------------------------------------

def test_merge_does_not_import_source_pricing(tmp_path):
    from conftest import make_db

    from siftd.api.merge import merge_database

    # Source DB carries a WRONG opus-4-5 price (99/199) for a model with usage —
    # distinct from this machine's reference (5/25) so the assertion discriminates.
    source = make_db(
        tmp_path / "source.db",
        model_name="claude-opus-4-5-20251101",
        conversations=[{"external_id": "c1", "prompt_text": "hi", "response_text": "yo"}],
    )
    s_conn = open_database(source)
    s_m = s_conn.execute("SELECT id FROM models WHERE name = 'claude-opus-4-5'").fetchone()["id"]
    # Name the provider 'anthropic' so the reference (keyed by provider name) can match.
    s_p = s_conn.execute("SELECT id FROM providers LIMIT 1").fetchone()["id"]
    s_conn.execute("UPDATE providers SET name = 'anthropic' WHERE id = ?", (s_p,))
    s_conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES (?, ?, ?, 99.0, 199.0)",
        (sq._ulid(), s_m, s_p),
    )
    s_conn.commit()
    s_conn.close()

    target = tmp_path / "target.db"
    open_database(target).close()
    merge_database(target, source)

    t_conn = open_database(target)
    try:
        # The merged-in opus-4-5 is priced from THIS machine's reference (5/25),
        # not the source DB's 99/199.
        row = t_conn.execute(
            "SELECT pr.input_per_mtok FROM pricing pr JOIN models m ON m.id = pr.model_id "
            "WHERE m.name = 'claude-opus-4-5'"
        ).fetchone()
        assert row is not None and row["input_per_mtok"] == 5.0
    finally:
        t_conn.close()


# ---------------------------------------------------------------------------
# 8. Reprice op (#2): rebuild cost from the reference without re-ingesting
# ---------------------------------------------------------------------------

def _seed_one_response(conn, raw_name, source, inp, out):
    h = sq.get_or_create_harness(conn, "tool", source=source)
    ws = sq.get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
    m = sq.get_or_create_model(conn, raw_name)
    p = sq.get_or_create_provider(conn, source)
    cid = sq.insert_conversation(conn, "c1", h, ws, "2024-01-01T00:00:00Z")
    pid = sq.insert_prompt(conn, cid, "p1", "2024-01-01T00:00:00Z")
    sq.insert_response(conn, cid, pid, m, p, "r0", "2024-01-01T00:00:01Z", inp, out)
    return m, p


def test_reprice_op_rebuilds_cost_from_reference(tmp_path):
    from siftd.api import run_backfill

    db = tmp_path / "t.db"
    conn = sq.create_database(db)
    _seed_one_response(conn, "claude-opus-4-5-20251101", "anthropic", 1_000_000, 0)
    conn.commit()
    conn.close()

    result = run_backfill(db_path=db, operation="pricing")
    assert result.repriced_rows >= 1

    conn = open_database(db)
    cost = conn.execute("SELECT SUM(cost) FROM usage_by_conv_model").fetchone()[0]
    conn.close()
    # 1M input tokens @ opus-4-5 $5/Mtok (reference) = $5.00
    assert abs(cost - 5.0) < 1e-6


# ---------------------------------------------------------------------------
# 9. Provenance visibility (#3): priced rows with source IS NULL are surfaced
# ---------------------------------------------------------------------------

def test_priced_without_provenance_is_surfaced(tmp_path):
    conn = sq.create_database(tmp_path / "t.db")
    # A governed model (reference prices it, source set) + an out-of-band model
    # (priced by hand, source NULL), both with usage.
    _m_gov, p = _seed_one_response(conn, "claude-opus-4-5-20251101", "anthropic", 100, 10)
    m_orphan = sq.get_or_create_model(conn, "some-synced-only-model")
    # give the orphan model a response so it counts as "with usage"
    cid = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]
    pid = conn.execute("SELECT id FROM events WHERE kind='prompt' LIMIT 1").fetchone()[0]
    sq.insert_response(conn, cid, pid, m_orphan, p, "r1", "2024-01-01T00:00:02Z", 50, 5)
    sq.ensure_pricing_table(conn)  # governs opus-4-5 (source set)
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
        "VALUES (?, ?, ?, 2.0, 8.0)",  # source NULL → out-of-band
        (sq._ulid(), m_orphan, p),
    )
    conn.commit()

    rows = sq.get_priced_models_without_provenance(conn)
    names = {r["model_name"] for r in rows}
    assert "some-synced-only-model" in names
    assert "claude-opus-4-5" not in names  # governed → has provenance
    conn.close()
