"""v10 cache-coherence: the rollup folds Anthropic cache tokens into the usage
fact and bills them.

These exercise the convention CASE (uncached_input_sql) at the row level — the
piece the live-DB aggregate can't isolate — plus the four-component cost and the
NULL-for-unpriced invariant. The convention is keyed on harness source:
  - anthropic: input_tokens EXCLUDES cache (additive) → total = input + cr + cc
  - openai:    input_tokens INCLUDES cache_read (subset) → total = input (+ cc)
  - other/multi: per-row fallback (input < cache_read ⇒ exclusive)
"""

import siftd.storage.sqlite as sq
from siftd.storage.attributes import set_attribute
from siftd.storage.usage_rollup import rebuild_rollups


def _seed_pricing(conn, model_id, provider_id, in_rate, out_rate, *, cread=None, ccreate=None):
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok, "
        "cache_read_per_mtok, cache_creation_per_mtok) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sq._ulid() if hasattr(sq, "_ulid") else "pr-x", model_id, provider_id,
         in_rate, out_rate, cread, ccreate),
    )


def _resp(conn, cid, pid, m, p, ext, ts, inp, out, *, cache_read=0, cache_creation=0):
    eid = sq.insert_response(conn, cid, pid, m, p, ext, ts, inp, out)
    if cache_read:
        set_attribute(conn, "response", eid, "cache_read_input_tokens", str(cache_read), scope="provider")
    if cache_creation:
        set_attribute(conn, "response", eid, "cache_creation_input_tokens", str(cache_creation), scope="provider")
    return eid


def _one_conv(db_path, source, inp, out, *, cache_read=0, cache_creation=0,
              in_rate=3.0, out_rate=15.0, priced=True, cread_rate=None, ccreate_rate=None):
    conn = sq.create_database(db_path)
    ws = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    h = sq.get_or_create_harness(conn, "tool", source=source)
    m = sq.get_or_create_model(conn, "the-model")
    p = sq.get_or_create_provider(conn, source)
    if priced:
        _seed_pricing(conn, m, p, in_rate, out_rate, cread=cread_rate, ccreate=ccreate_rate)
    cid = sq.insert_conversation(conn, "c1", h, ws, "2024-01-01T00:00:00Z")
    pid = sq.insert_prompt(conn, cid, "p1", "2024-01-01T00:00:00Z")
    _resp(conn, cid, pid, m, p, "r0", "2024-01-01T00:00:01Z", inp, out,
          cache_read=cache_read, cache_creation=cache_creation)
    rebuild_rollups(conn)
    conn.commit()
    row = conn.execute(
        "SELECT input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost "
        "FROM usage_by_conv_model"
    ).fetchone()
    conn.close()
    return row  # (input_total, output, cache_read, cache_creation, cost)


def test_anthropic_cache_is_additive(tmp_path):
    # input EXCLUDES cache → total = 1000 + 5000 + 2000 = 8000.
    inp_total, out, cr, cc, cost = _one_conv(
        tmp_path / "a.db", "anthropic", 1000, 100, cache_read=5000, cache_creation=2000
    )
    assert (inp_total, out, cr, cc) == (8000, 100, 5000, 2000)
    # cost = 1000*3 + 2000*(3*1.25) + 5000*(3*0.1) + 100*15 = 3000+7500+1500+1500 = 13500 → /1e6
    assert abs(cost - 13500 / 1e6) < 1e-12


def test_openai_cache_read_not_double_counted(tmp_path):
    # input INCLUDES cache_read → total stays 8000 (NOT 13000); uncached = 3000.
    inp_total, out, cr, cc, cost = _one_conv(
        tmp_path / "o.db", "openai", 8000, 100, cache_read=5000
    )
    assert (inp_total, out, cr, cc) == (8000, 100, 5000, 0)
    # cost = (8000-5000)*3 + 5000*(3*0.1) + 100*15 = 9000+1500+1500 = 12000 → /1e6
    assert abs(cost - 12000 / 1e6) < 1e-12


def test_multi_source_falls_back_to_per_row_signature(tmp_path):
    # source='multi' (Claude-backed): input(100) < cache_read(5000) ⇒ exclusive.
    inp_total, out, cr, cc, _ = _one_conv(
        tmp_path / "m.db", "multi", 100, 50, cache_read=5000
    )
    assert (inp_total, out, cr, cc) == (5100, 50, 5000, 0)


def test_unpriced_model_cost_is_null_not_zero(tmp_path):
    # No pricing row → cost NULL (the em-dash invariant), but tokens still fold cache.
    inp_total, out, cr, cc, cost = _one_conv(
        tmp_path / "u.db", "anthropic", 1000, 100, cache_read=5000, priced=False
    )
    assert (inp_total, cr, cc) == (6000, 5000, 0)
    assert cost is None


def test_explicit_cache_rate_overrides_multiplier(tmp_path):
    # cache_read_per_mtok set explicitly (0.5/Mtok) overrides the 0.1× default.
    _, _, _, _, cost = _one_conv(
        tmp_path / "ov.db", "anthropic", 0, 0, cache_read=1_000_000,
        in_rate=3.0, out_rate=15.0, cread_rate=0.5,
    )
    # 1M cache_read * $0.5/Mtok = $0.5 ; multiplier would have given 1M*0.3 = $0.3.
    assert abs(cost - 0.5) < 1e-12


def test_no_cache_data_matches_plain_input(tmp_path):
    # A response with no cache attrs: total == input, cost == input+output billed.
    inp_total, out, cr, cc, cost = _one_conv(
        tmp_path / "n.db", "anthropic", 4000, 200
    )
    assert (inp_total, out, cr, cc) == (4000, 200, 0, 0)
    assert abs(cost - (4000 * 3 + 200 * 15) / 1e6) < 1e-12
