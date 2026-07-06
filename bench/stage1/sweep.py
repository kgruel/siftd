#!/usr/bin/env python3
"""Stage-1 offline sweep: rank-space fusion grid over cached per-query artifacts.

Runs entirely offline over the artifacts cached by cache_artifacts.py (query
embeddings + FTS recall/content lists) and the in-memory arm chunk matrix. No API
calls, no re-searching: the grid fuses thousands of configs in minutes. Reports
against the PRE-COMMITTED promote rules in docs/dev/bench-plan-2026-07-05.md; it does
not adjust them.

RECALL@10 DEFINITION (judgment-session review item)
---------------------------------------------------
Mirrors bench/ab_rrf.py:recall_at_10 exactly — the existing bench's definition:

    got = {result.conversation_id for result in results[:10]}   # first 10 result
                                                                 # slots -> distinct
                                                                 # conversation set
    recall@10 = |got ∩ labels| / min(10, |labels|)

Note this caps at the first 10 *result slots* (chunks/entrants) and THEN projects to
the distinct conversation set — it does not take the first 10 *distinct* conversations.
Because the fidelity gate scores at conversation granularity over the top-10 chunks,
this is the same granularity the gate validated. Chosen over the task's fallback
definition ("top-10 distinct conversations in order") because ab_rrf already ships one
and consistency with the slice-4 harness is the point of the gate.

FIDELITY / SELF-CHECK
---------------------
The RRF family here is a NEW generalized weighted fusion:

    score(item) = w_kw / (k_kw + keyword_rank) + 1 / (k_vec + vector_rank)

with per-list k. It mirrors offline_lib._fuse's mechanics EXACTLY — same fusion keys
(chunk_id for chunks, "entrant:<event_id>" for uncovered FTS hits), same best (lowest)
keyword-rank rule, same (-score, key) tie-break — and only generalizes the score
formula. Self-check A (required): w_kw=1, k_kw=k_vec=60, pool=30, λ=0.7 must reproduce
offline_lib.replica_rrf's ordering on a probe of queries (the k=60 unit-weight formula
IS offline_lib._FusionItem.fused_score). Self-check B: the fast vector-list builder
here (precomputed cosine, then pinned replica_mmr_rerank) must match
offline_lib._vector_list chunk-for-chunk across pools/λ. Both run before the grid.

The MMR-λ="off" path replicates the engine's use_mmr=False branch
(siftd.api.search._build_vector_list: sorted by (-score, chunk_id), truncate). It is a
faithful source-mirror; it is NOT gate-covered (both live knobs use MMR) and it is NOT
identical to λ=1.0 (tie-break direction differs — MMR breaks ties toward larger
chunk_id via strict tuple >, the off path sorts chunk_id ascending).

Usage (from worktree root):
    env -u VIRTUAL_ENV UV_NO_SYNC=1 uv run --no-sync python bench/stage1/sweep.py \\
        --backend voyage
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import offline_lib  # noqa: E402
from gen_queries import FtsLexicon  # noqa: E402
from offline_lib import (  # noqa: E402
    MMR_CAP,
    ArmData,
    replica_mmr_rerank,
    replica_narrow,
    replica_rrf,
)

RUN_DIR = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
SNAPSHOT = RUN_DIR / "siftd-snapshot.db"
FTS_DEPTH = 500  # the cached FTS recall/content depth — swept depths must stay <= this
N = 10  # top-n; recall@10, MRR@10
SEED = 20260705  # fixed literal seed for the bootstrap (pre-committed)
BOOT = 1000  # bootstrap resamples
CLASSES = ["identifier", "tool", "topical", "paraphrase", "mixed"]

# --- grid (plan §Sweep architecture) ---------------------------------------
NARROW_RECALLS = [40, 80, 160, 320]  # narrow-then-rank candidate-width knob; 80 = ship
NARROW_BASELINE_RECALL = 80  # THE baseline row; all deltas/CIs are vs this
W_KW = [0.25, 0.5, 1.0, 1.5, 2.0]
K_KW = [10, 20, 60, 120]
K_VEC = [20, 60, 120]
POOLS = [30, 100, 300]  # vector pool depth (never above MMR_CAP=1000)
LAMBDAS = ["off", 0.5, 0.7, 1.0]  # "off" = use_mmr=False path
H2_D = [2, 5, 10, 20]  # min-DF step threshold for query-conditional w(q)


# --------------------------------------------------------------------------- #
# Metrics (recall@10 mirrors ab_rrf; see module docstring).
# --------------------------------------------------------------------------- #


def recall_at_10(convs_in_order: list[str], labels: set[str]) -> float:
    got = set(convs_in_order[:N])
    denom = min(N, len(labels)) or 1
    return len(got & labels) / denom


def mrr_at_10(convs_in_order: list[str], labels: set[str]) -> float:
    for i, c in enumerate(convs_in_order[:N]):
        if c in labels:
            return 1.0 / (i + 1)
    return 0.0


# --------------------------------------------------------------------------- #
# Fast vector list: precompute cosine once per query, reuse the pinned MMR.
# --------------------------------------------------------------------------- #


def vector_list_fast(arm: ArmData, scores: np.ndarray, qv_list: list[float], pool: int, lam) -> list[dict]:
    """Top-`pool` vector list, mirroring offline_lib._vector_list (MMR path) and the
    engine's use_mmr=False else-branch ("off"). Takes precomputed full-set cosine
    ``scores`` so the sweep computes the 203k matvec once per query instead of per
    (pool, λ). Selection reproduces replica_search_similar's full-set argpartition
    exactly; MMR delegates to the pinned replica_mmr_rerank; both are asserted equal to
    offline_lib._vector_list in self-check B."""
    n = len(scores)
    if n <= pool:
        top = np.argsort(-scores)
    else:
        k = min(pool, n)
        part = np.argpartition(-scores, k)[:k]
        top = part[np.argsort(-scores[part])]

    results: list[dict] = []
    want_emb = lam != "off"
    for gi in top:
        gi = int(gi)
        raw = arm.source_ids_raw[gi]
        r = {
            "chunk_id": arm.chunk_ids[gi],
            "conversation_id": arm.conversation_ids[gi],
            "score": float(scores[gi]),
            "source_ids": json.loads(raw) if raw else [],
        }
        if want_emb:
            r["embedding"] = arm.matrix[gi]
        results.append(r)

    if lam == "off":
        # Engine use_mmr=False: deterministic relevance order, chunk_id breaks ties.
        results = sorted(results, key=lambda r: (-r["score"], r.get("chunk_id", "")))[:pool]
    else:
        if len(results) > MMR_CAP:  # never binds at pool <= 300, kept faithful
            results = sorted(results, key=lambda r: -r["score"])[:MMR_CAP]
        results = replica_mmr_rerank(results, qv_list, lambda_=lam, limit=pool)
    for i, r in enumerate(results):
        r["vector_rank"] = i + 1
    return results


# --------------------------------------------------------------------------- #
# Fusion universe: the (vr, kr, cosine, conv, key) records for one (pool, λ).
# --------------------------------------------------------------------------- #


class Universe:
    """The fusion items for one query at one (pool, λ), pre-arranged for vectorized
    re-scoring across weight combos. Built once per (pool, λ); the weight grid only
    changes the score formula, not the item set or the ranks."""

    __slots__ = ("vr", "kr", "cosine", "conv", "key_rank")

    def __init__(self, items: list[dict]):
        m = len(items)
        self.vr = np.full(m, -1, dtype=np.int64)
        self.kr = np.full(m, -1, dtype=np.int64)
        self.cosine = np.zeros(m, dtype=np.float64)
        self.conv: list[str] = [None] * m  # type: ignore[list-item]
        keys: list[str] = [None] * m  # type: ignore[list-item]
        for i, it in enumerate(items):
            if it["vr"] is not None:
                self.vr[i] = it["vr"]
            if it["kr"] is not None:
                self.kr[i] = it["kr"]
            self.cosine[i] = it["cosine"]
            self.conv[i] = it["conv"]
            keys[i] = it["key"]
        # rank[i] = ascending rank of keys[i]; lexsort secondary key == key string order.
        order = np.argsort(np.array(keys, dtype=object), kind="stable")
        self.key_rank = np.empty(m, dtype=np.int64)
        self.key_rank[order] = np.arange(m)

    def topn_convs(self, w_kw: float, k_kw: int, k_vec: int) -> tuple[list[str], float]:
        """Return (ordered top-N conversation ids, top-1 cosine) under one weight combo,
        with the exact (-score, key) ordering of offline_lib._fuse."""
        score = np.zeros(len(self.vr), dtype=np.float64)
        has_v = self.vr >= 0
        has_k = self.kr >= 0
        score[has_v] += 1.0 / (k_vec + self.vr[has_v])
        score[has_k] += w_kw / (k_kw + self.kr[has_k])
        order = np.lexsort((self.key_rank, -score))[:N]
        convs = [self.conv[i] for i in order]
        top1_cosine = float(self.cosine[order[0]]) if len(order) else 0.0
        return convs, top1_cosine


def build_universe(arm: ArmData, scores: np.ndarray, qv_list: list[float], pool: int, lam,
                   content: list[dict]) -> Universe:
    """Build the fusion universe for one query at one (pool, λ). Mirrors _fuse's
    construction: vector items keyed by chunk_id, then FTS hits bridge to covering
    chunk(s) (best keyword rank) or become "entrant:<eid>" items."""
    kw_depth = min(max(pool, N), FTS_DEPTH)  # keyword list depth = max(pool, 10), <= 500
    kw = content[:kw_depth]
    vector_results = vector_list_fast(arm, scores, qv_list, pool, lam)

    fusion: dict[str, dict] = {}
    for r in vector_results:
        fusion[r["chunk_id"]] = {
            "vr": r["vector_rank"], "kr": None, "cosine": r["score"],
            "conv": r["conversation_id"], "key": r["chunk_id"],
        }
    event_ids = [h["e"] for h in kw if h.get("e")]
    covering = arm.chunks_for_events(event_ids)
    for pos, h in enumerate(kw):
        kr = pos + 1
        eid = h.get("e")
        chunks = covering.get(eid) if eid else None
        if chunks:
            for ch in chunks:
                cid = ch["chunk_id"]
                item = fusion.get(cid)
                if item is None:
                    item = {"vr": None, "kr": None, "cosine": 0.0,
                            "conv": ch["conversation_id"], "key": cid}
                    fusion[cid] = item
                if item["kr"] is None or kr < item["kr"]:
                    item["kr"] = kr
        elif eid:
            ekey = f"entrant:{eid}"
            item = fusion.get(ekey)
            if item is None:
                fusion[ekey] = {"vr": None, "kr": kr, "cosine": 0.0, "conv": h["c"], "key": ekey}
            elif item["kr"] is None or kr < item["kr"]:
                item["kr"] = kr
    return Universe(list(fusion.values()))


# --------------------------------------------------------------------------- #
# Loading.
# --------------------------------------------------------------------------- #


def load_artifacts(backend: str):
    art = RUN_DIR / f"artifacts-{backend}"
    embed_db = RUN_DIR / f"embed-{backend}.db"
    if not (art / "query_emb.npy").exists():
        sys.exit(f"artifacts not found: {art} (run cache_artifacts.py first)")
    if not embed_db.exists():
        sys.exit(f"arm DB not found: {embed_db}")

    q_lines = [json.loads(x) for x in (art / "queries.jsonl").read_text().splitlines() if x.strip()]
    emb = np.load(art / "query_emb.npy").astype(np.float32)
    if emb.shape[0] != len(q_lines):
        sys.exit(f"emb rows {emb.shape[0]} != queries {len(q_lines)}")

    fts_by_qid: dict[str, dict] = {}
    for line in (art / "fts.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            fts_by_qid[row["qid"]] = row

    print("loading arm matrix...", file=sys.stderr)
    arm = ArmData.load(embed_db)
    print(f"arm: {len(arm.chunk_ids)} chunks x {arm.dim} dim", file=sys.stderr)
    if arm.dim != emb.shape[1]:
        sys.exit(f"dim mismatch: arm {arm.dim} != query emb {emb.shape[1]}")
    arm.event_map()  # build once up front (a few seconds over 203k source_ids)
    return q_lines, emb, fts_by_qid, arm


def compute_min_df(q_lines: list[dict]) -> dict[str, int]:
    """min-DF over each query's porter-stemmed tokens (FtsLexicon against the snapshot
    vocab — the same DF machinery gen_queries.py/ground_truth.py use). A token absent
    from the corpus vocab counts as df 0 (maximally distinctive); a query with no
    tokens gets a large sentinel (falls to w_low)."""
    import sqlite3

    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    lex = FtsLexicon(conn)
    out: dict[str, int] = {}
    for q in q_lines:
        terms = lex.terms(q["query"])
        if terms:
            out[q["qid"]] = min(lex.df.get(t, 0) for t in terms)
        else:
            out[q["qid"]] = 10**9
    conn.close()
    return out


# --------------------------------------------------------------------------- #
# Self-checks.
# --------------------------------------------------------------------------- #


def self_check(arm: ArmData, emb: np.ndarray, q_lines: list[dict], fts_by_qid: dict[str, dict]) -> dict:
    """A: generalized fuse (w=1, k=60, pool=30, λ=0.7) reproduces replica_rrf ordering.
    B: vector_list_fast matches offline_lib._vector_list across pools/λ."""
    probe_idx = list(range(0, len(q_lines), max(1, len(q_lines) // 20)))[:20]

    # --- B: vector list parity ---
    b_checked = 0
    for qi in probe_idx:
        qv = emb[qi]
        qn = np.linalg.norm(qv)
        scores = arm.matrix @ (qv / qn) if qn else np.zeros(arm.matrix.shape[0], dtype=np.float32)
        for pool in POOLS:
            for lam in (0.5, 0.7, 1.0):
                fast = vector_list_fast(arm, scores, qv.tolist(), pool, lam)
                ref = offline_lib._vector_list(  # noqa: SLF001 — parity target
                    arm, qv.tolist(), None, search_limit=pool, mmr_limit=pool, lambda_=lam,
                )
                if [r["chunk_id"] for r in fast] != [r["chunk_id"] for r in ref]:
                    return {"pass": False, "which": "B",
                            "detail": f"qid={q_lines[qi]['qid']} pool={pool} lam={lam}"}
                b_checked += 1

    # --- A: generalized fuse == replica_rrf at k=60, w=1, pool=30, λ=0.7 ---
    a_checked = 0
    for qi in probe_idx:
        qv = emb[qi]
        qn = np.linalg.norm(qv)
        scores = arm.matrix @ (qv / qn) if qn else np.zeros(arm.matrix.shape[0], dtype=np.float32)
        fts = fts_by_qid[q_lines[qi]["qid"]]
        content = fts["content"]
        uni = build_universe(arm, scores, qv.tolist(), pool=30, lam=0.7, content=content)
        gen_convs, _ = uni.topn_convs(w_kw=1.0, k_kw=60, k_vec=60)
        kw = [{"conversation_id": h["c"], "event_id": h["e"]} for h in content]
        ref = replica_rrf(arm, qv.tolist(), kw)
        ref_convs = [r["conversation_id"] for r in ref[:N]]
        if gen_convs != ref_convs:
            return {"pass": False, "which": "A", "detail": f"qid={q_lines[qi]['qid']}",
                    "gen": gen_convs, "ref": ref_convs}
        a_checked += 1
    return {"pass": True, "a_checked": a_checked, "b_checked": b_checked, "probe": len(probe_idx)}


# --------------------------------------------------------------------------- #
# Bootstrap CIs.
# --------------------------------------------------------------------------- #


def make_resamples(class_idx: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Per class, a (BOOT x n_class) matrix of resample positions INTO that class's
    query list (seeded once, reused across all configs so CIs are paired/comparable)."""
    rng = np.random.default_rng(SEED)
    out: dict[str, np.ndarray] = {}
    for cls in CLASSES:
        n = len(class_idx[cls])
        out[cls] = rng.integers(0, n, size=(BOOT, n)) if n else np.zeros((BOOT, 0), dtype=int)
    return out


def class_ci(delta: np.ndarray, cls_positions: np.ndarray, resample: np.ndarray) -> tuple[float, float, float]:
    """(mean_delta, lo, hi) of a per-query recall@10 delta over one class's queries."""
    d = delta[cls_positions]
    if d.size == 0:
        return 0.0, 0.0, 0.0
    boot = d[resample].mean(axis=1)
    return float(d.mean()), float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #


def cfg_id(c: dict) -> str:
    return f"w{c['w_kw']}_kkw{c['k_kw']}_kvec{c['k_vec']}_p{c['pool']}_l{c['lambda']}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="voyage", help="arm backend (preset name)")
    args = ap.parse_args()
    t_start = time.time()

    q_lines, emb, fts_by_qid, arm = load_artifacts(args.backend)
    nq = len(q_lines)
    labels = [set(q["labels"]) for q in q_lines]
    classes = [q["class"] for q in q_lines]
    for r in NARROW_RECALLS:
        assert r <= FTS_DEPTH, f"narrow recall {r} exceeds cached FTS depth {FTS_DEPTH}"
    assert max(POOLS) <= MMR_CAP, "pool exceeds MMR_CAP"
    assert min(max(p, N) for p in POOLS) <= FTS_DEPTH and max(max(p, N) for p in POOLS) <= FTS_DEPTH

    class_idx = {cls: np.array([i for i in range(nq) if classes[i] == cls], dtype=int) for cls in CLASSES}
    print("class counts: " + ", ".join(f"{c}={len(class_idx[c])}" for c in CLASSES), file=sys.stderr)

    print("computing min-DF per query...", file=sys.stderr)
    min_df = compute_min_df(q_lines)

    print("running self-checks...", file=sys.stderr)
    sc = self_check(arm, emb, q_lines, fts_by_qid)
    if not sc["pass"]:
        print(f"SELF-CHECK FAILED: {sc}", file=sys.stderr)
        sys.exit(1)
    print(f"  self-check A ({sc['a_checked']} probes) + B ({sc['b_checked']} probes): PASS", file=sys.stderr)

    # --- enumerate H1 configs, grouped by (pool, λ) for universe reuse ---
    h1_configs: list[dict] = []
    grp: dict[tuple, list[tuple[int, dict]]] = {}
    for pool in POOLS:
        for lam in LAMBDAS:
            for w in W_KW:
                for kk in K_KW:
                    for kv in K_VEC:
                        idx = len(h1_configs)
                        c = {"w_kw": w, "k_kw": kk, "k_vec": kv, "pool": pool, "lambda": lam}
                        h1_configs.append(c)
                        grp.setdefault((pool, lam), []).append((idx, c))
    n_h1 = len(h1_configs)
    print(f"H1 grid: {n_h1} configs; narrow baselines: {len(NARROW_RECALLS)}", file=sys.stderr)

    recall_h1 = np.zeros((n_h1, nq), dtype=np.float64)
    mrr_h1 = np.zeros((n_h1, nq), dtype=np.float64)
    top1_h1 = np.zeros((n_h1, nq), dtype=np.float64)
    recall_narrow = {r: np.zeros(nq, dtype=np.float64) for r in NARROW_RECALLS}
    mrr_narrow = {r: np.zeros(nq, dtype=np.float64) for r in NARROW_RECALLS}
    top1_narrow = {r: np.zeros(nq, dtype=np.float64) for r in NARROW_RECALLS}

    # --- PASS 1: narrow baselines + full H1 grid ---
    print("pass 1: narrow baselines + H1 grid...", file=sys.stderr)
    t0 = time.time()
    for qi in range(nq):
        qv = emb[qi]
        qv_list = qv.tolist()
        qn = np.linalg.norm(qv)
        scores = arm.matrix @ (qv / qn) if qn else np.zeros(arm.matrix.shape[0], dtype=np.float32)
        fts = fts_by_qid[q_lines[qi]["qid"]]
        recall_ids = fts["recall"]["conversation_ids"]
        content = fts["content"]
        lab = labels[qi]

        for r in NARROW_RECALLS:
            res = replica_narrow(arm, qv_list, recall_ids, recall=r)
            convs = [x["conversation_id"] for x in res]
            recall_narrow[r][qi] = recall_at_10(convs, lab)
            mrr_narrow[r][qi] = mrr_at_10(convs, lab)
            top1_narrow[r][qi] = float(res[0]["score"]) if res else 0.0

        for (pool, lam), members in grp.items():
            uni = build_universe(arm, scores, qv_list, pool, lam, content)
            for idx, c in members:
                convs, t1 = uni.topn_convs(c["w_kw"], c["k_kw"], c["k_vec"])
                recall_h1[idx, qi] = recall_at_10(convs, lab)
                mrr_h1[idx, qi] = mrr_at_10(convs, lab)
                top1_h1[idx, qi] = t1
        if (qi + 1) % 25 == 0:
            rate = (qi + 1) / (time.time() - t0)
            print(f"  {qi + 1}/{nq}  ({rate:.1f} q/s, eta {int((nq - qi - 1) / rate)}s)", file=sys.stderr)
    print(f"pass 1 done in {time.time() - t0:.0f}s", file=sys.stderr)

    # --- per-class means + composite ---
    def per_class(arr_row: np.ndarray) -> dict[str, float]:
        return {cls: float(arr_row[class_idx[cls]].mean()) if len(class_idx[cls]) else 0.0 for cls in CLASSES}

    def composite(pc: dict[str, float]) -> float:
        return float(np.mean([pc[c] for c in CLASSES]))

    base_recall = recall_narrow[NARROW_BASELINE_RECALL]
    resamples = make_resamples(class_idx)

    def ci_block(delta_row: np.ndarray) -> dict[str, dict]:
        out = {}
        for cls in CLASSES:
            md, lo, hi = class_ci(delta_row, class_idx[cls], resamples[cls])
            out[cls] = {"mean_delta": md, "lo": lo, "hi": hi}
        return out

    def promote_conditions(pc: dict[str, float], ci: dict[str, dict]) -> dict:
        base_pc = per_class(base_recall)
        no_worse = {}
        for cls in ("identifier", "topical", "tool"):
            # "no worse" = bootstrap CI of the delta includes 0 or favors RRF (hi >= 0).
            no_worse[cls] = {"met": ci[cls]["hi"] >= 0.0, "ci": ci[cls]}
        base_para = base_pc["paraphrase"]
        rel = (pc["paraphrase"] - base_para) / base_para if base_para else None
        return {
            "identifier_no_worse": no_worse["identifier"],
            "topical_no_worse": no_worse["topical"],
            "tool_no_worse": no_worse["tool"],
            "paraphrase_rel_delta": rel,
            "paraphrase_meets_20pct_IF_margin_holds": (rel is not None and rel >= 0.20),
            "paraphrase_margin_note": "+20% margin PENDING RE-RATIFICATION (plan promote rule 2)",
            "identifier_tool_topical_all_no_worse": all(no_worse[c]["met"] for c in ("identifier", "topical", "tool")),
        }

    # --- narrow baseline rows ---
    narrow_rows = []
    for r in NARROW_RECALLS:
        pc = per_class(recall_narrow[r])
        row = {
            "recall": r, "is_baseline": r == NARROW_BASELINE_RECALL,
            "per_class_recall": pc, "composite": composite(pc),
            "mrr": per_class(mrr_narrow[r]), "avg_top1_cosine": per_class(top1_narrow[r]),
            "ci_vs_narrow80": ci_block(recall_narrow[r] - base_recall),
        }
        narrow_rows.append(row)

    # --- H1 rows ---
    h1_rows = []
    for idx, c in enumerate(h1_configs):
        pc = per_class(recall_h1[idx])
        h1_rows.append({
            "id": cfg_id(c), "config": c, "family": "H1",
            "per_class_recall": pc, "composite": composite(pc),
            "mrr": per_class(mrr_h1[idx]), "avg_top1_cosine": per_class(top1_h1[idx]),
        })
    h1_rows.sort(key=lambda r: -r["composite"])
    # CIs + promote conditions for the shortlist (top-15) only, to keep the JSON lean.
    for row in h1_rows[:15]:
        idx = next(i for i, c in enumerate(h1_configs) if cfg_id(c) == row["id"])
        row["ci_vs_narrow80"] = ci_block(recall_h1[idx] - base_recall)
        row["promote"] = promote_conditions(row["per_class_recall"], row["ci_vs_narrow80"])

    # --- H2: pre-declared step-function family, fixed to best H1's non-w params ---
    best_h1 = h1_rows[0]["config"]
    top_w = []
    for row in h1_rows:
        w = row["config"]["w_kw"]
        if w not in top_w:
            top_w.append(w)
        if len(top_w) == 3:
            break
    pairs = [(hi, lo) for hi in top_w for lo in top_w if hi != lo]  # crossed, w_high != w_low
    print(f"H2: base=(k_kw={best_h1['k_kw']}, k_vec={best_h1['k_vec']}, pool={best_h1['pool']}, "
          f"lambda={best_h1['lambda']}); top_w={top_w}; {len(pairs)} pairs x {len(H2_D)} D "
          f"= {len(pairs) * len(H2_D)} configs", file=sys.stderr)

    h2_configs = []
    for (w_high, w_low) in pairs:
        for d in H2_D:
            h2_configs.append({
                "w_high": w_high, "w_low": w_low, "D": d,
                "k_kw": best_h1["k_kw"], "k_vec": best_h1["k_vec"],
                "pool": best_h1["pool"], "lambda": best_h1["lambda"],
            })
    n_h2 = len(h2_configs)
    recall_h2 = np.zeros((n_h2, nq), dtype=np.float64)
    mrr_h2 = np.zeros((n_h2, nq), dtype=np.float64)
    top1_h2 = np.zeros((n_h2, nq), dtype=np.float64)

    # --- PASS 2: H2 over the single fixed (pool, λ) universe per query ---
    print("pass 2: H2 grid...", file=sys.stderr)
    t0 = time.time()
    pool_h2, lam_h2 = best_h1["pool"], best_h1["lambda"]
    for qi in range(nq):
        qv = emb[qi]
        qv_list = qv.tolist()
        qn = np.linalg.norm(qv)
        scores = arm.matrix @ (qv / qn) if qn else np.zeros(arm.matrix.shape[0], dtype=np.float32)
        content = fts_by_qid[q_lines[qi]["qid"]]["content"]
        lab = labels[qi]
        mdf = min_df[q_lines[qi]["qid"]]
        uni = build_universe(arm, scores, qv_list, pool_h2, lam_h2, content)
        for ci2, c in enumerate(h2_configs):
            w = c["w_high"] if mdf <= c["D"] else c["w_low"]
            convs, t1 = uni.topn_convs(w, c["k_kw"], c["k_vec"])
            recall_h2[ci2, qi] = recall_at_10(convs, lab)
            mrr_h2[ci2, qi] = mrr_at_10(convs, lab)
            top1_h2[ci2, qi] = t1
        if (qi + 1) % 100 == 0:
            print(f"  {qi + 1}/{nq}", file=sys.stderr)
    print(f"pass 2 done in {time.time() - t0:.0f}s", file=sys.stderr)

    h2_rows = []
    for idx, c in enumerate(h2_configs):
        pc = per_class(recall_h2[idx])
        row = {
            "id": f"whigh{c['w_high']}_wlow{c['w_low']}_D{c['D']}", "config": c, "family": "H2",
            "per_class_recall": pc, "composite": composite(pc),
            "mrr": per_class(mrr_h2[idx]), "avg_top1_cosine": per_class(top1_h2[idx]),
        }
        h2_rows.append(row)
    h2_rows.sort(key=lambda r: -r["composite"])
    for row in h2_rows[:15]:
        idx = next(i for i, c in enumerate(h2_configs)
                   if f"whigh{c['w_high']}_wlow{c['w_low']}_D{c['D']}" == row["id"])
        row["ci_vs_narrow80"] = ci_block(recall_h2[idx] - base_recall)
        row["promote"] = promote_conditions(row["per_class_recall"], row["ci_vs_narrow80"])

    # --- H2 vs H1 (the mixed slice is the only place H2 can earn its keep) ---
    h1_best_row = h1_rows[0]
    h2_best_row = h2_rows[0]
    mixed_h1 = h1_best_row["per_class_recall"]["mixed"]
    mixed_h2 = h2_best_row["per_class_recall"]["mixed"]
    # H2's best on the mixed slice specifically vs H1's best-composite mixed value.
    h2_best_mixed_row = max(h2_rows, key=lambda r: r["per_class_recall"]["mixed"])
    h1_best_mixed_row = max(h1_rows, key=lambda r: r["per_class_recall"]["mixed"])
    h2_vs_h1 = {
        "h1_best_composite": {"id": h1_best_row["id"], "config": h1_best_row["config"],
                              "composite": h1_best_row["composite"], "mixed_recall": mixed_h1},
        "h2_best_composite": {"id": h2_best_row["id"], "config": h2_best_row["config"],
                              "composite": h2_best_row["composite"], "mixed_recall": mixed_h2},
        "composite_delta_h2_minus_h1": h2_best_row["composite"] - h1_best_row["composite"],
        "mixed_slice": {
            "h1_best_mixed": {"id": h1_best_mixed_row["id"], "mixed_recall": h1_best_mixed_row["per_class_recall"]["mixed"]},
            "h2_best_mixed": {"id": h2_best_mixed_row["id"], "mixed_recall": h2_best_mixed_row["per_class_recall"]["mixed"]},
            "mixed_delta_h2_minus_h1": h2_best_mixed_row["per_class_recall"]["mixed"]
            - h1_best_mixed_row["per_class_recall"]["mixed"],
        },
        "verdict_rule": "H2 promotes over H1 only if it beats H1's best composite AND wins the mixed slice by a "
                        "margin justifying runtime DF machinery; ties/unclear -> H1 wins by dissolution.",
    }

    # --- anomaly scan ---
    anomalies = []
    for cls in CLASSES:
        cidx = class_idx[cls]
        if len(cidx) == 0:
            anomalies.append(f"class {cls}: zero queries")
            continue
        zero_all = int(np.sum(recall_h1[:, cidx].max(axis=0) == 0.0))
        if zero_all:
            anomalies.append(f"class {cls}: {zero_all}/{len(cidx)} queries score recall@10=0 under EVERY H1 config")
    base_zero = int(np.sum(base_recall == 0.0))
    anomalies.append(f"narrow80 baseline: {base_zero}/{nq} queries at recall@10=0")

    # --- write outputs ---
    meta = {
        "backend": args.backend,
        "n_queries": nq,
        "class_counts": {c: int(len(class_idx[c])) for c in CLASSES},
        "recall_at_10_definition": "ab_rrf mirror: {conv_id for r in results[:10]} & labels / min(10,|labels|); "
                                   "caps at 10 result slots then projects to distinct conversations",
        "grid": {"w_kw": W_KW, "k_kw": K_KW, "k_vec": K_VEC, "pools": POOLS, "lambdas": LAMBDAS,
                 "narrow_recalls": NARROW_RECALLS, "h2_D": H2_D},
        "n_h1_configs": n_h1, "n_h2_configs": n_h2,
        "baseline": f"narrow recall={NARROW_BASELINE_RECALL}",
        "bootstrap": {"resamples": BOOT, "seed": SEED, "ci": "95% (2.5/97.5 pct) of per-query recall@10 delta"},
        "self_check": sc,
        "wall_seconds": round(time.time() - t_start, 1),
        "note_evaluation": "per-class, never pooled (plan). Metrics: recall@10 gates; MRR secondary; "
                           "avg_top1_cosine reported, NEVER binding (structural inflation under full-set ranking).",
    }
    results = {
        "meta": meta,
        "narrow_baselines": narrow_rows,
        "h1_top": h1_rows[:15],
        "h1_full": [{"id": r["id"], "config": r["config"], "composite": r["composite"],
                     "per_class_recall": r["per_class_recall"]} for r in h1_rows],
        "h2_top": h2_rows[:15],
        "h2_full": [{"id": r["id"], "config": r["config"], "composite": r["composite"],
                     "per_class_recall": r["per_class_recall"]} for r in h2_rows],
        "h2_vs_h1": h2_vs_h1,
        "anomalies": anomalies,
    }
    out_json = RUN_DIR / f"sweep-results-{args.backend}.json"
    out_json.write_text(json.dumps(results, indent=2))
    print(f"wrote {out_json}", file=sys.stderr)

    write_report(RUN_DIR / f"sweep-report-{args.backend}.md", results)
    print(f"wrote {RUN_DIR / f'sweep-report-{args.backend}.md'}", file=sys.stderr)
    print(f"DONE in {meta['wall_seconds']}s", file=sys.stderr)


def _fmt_pc(pc: dict[str, float]) -> str:
    return " ".join(f"{c[:4]}={pc[c]:.3f}" for c in CLASSES)


def write_report(path: Path, R: dict) -> None:
    m = R["meta"]
    L = []
    L.append(f"# Stage-1 sweep report — {m['backend']} arm\n")
    L.append(f"- Queries: {m['n_queries']}  ({', '.join(f'{k}={v}' for k, v in m['class_counts'].items())})")
    L.append(f"- H1 configs: {m['n_h1_configs']}  |  H2 configs: {m['n_h2_configs']}  "
             f"|  baseline: {m['baseline']}")
    L.append(f"- Wall time: {m['wall_seconds']}s  |  self-check: "
             f"{'PASS' if m['self_check']['pass'] else 'FAIL'}")
    L.append(f"- recall@10: {m['recall_at_10_definition']}")
    L.append(f"- Bootstrap: {m['bootstrap']['resamples']} resamples, seed {m['bootstrap']['seed']}, "
             f"{m['bootstrap']['ci']}")
    L.append(f"- Evaluation: {m['note_evaluation']}\n")

    L.append("## Narrow-then-rank baselines (recall knob)\n")
    L.append("| recall | composite | " + " | ".join(CLASSES) + " |")
    L.append("|" + "---|" * (len(CLASSES) + 2))
    for row in R["narrow_baselines"]:
        tag = " (BASELINE)" if row["is_baseline"] else ""
        pc = row["per_class_recall"]
        L.append(f"| {row['recall']}{tag} | {row['composite']:.4f} | "
                 + " | ".join(f"{pc[c]:.3f}" for c in CLASSES) + " |")
    L.append("")

    def rank_table(rows, title, family):
        L.append(f"## {title}\n")
        L.append("| # | id | composite | " + " | ".join(CLASSES) + " |")
        L.append("|" + "---|" * (len(CLASSES) + 3))
        for i, row in enumerate(rows[:5], 1):
            pc = row["per_class_recall"]
            L.append(f"| {i} | `{row['id']}` | {row['composite']:.4f} | "
                     + " | ".join(f"{pc[c]:.3f}" for c in CLASSES) + " |")
        L.append("")

    rank_table(R["h1_top"], "Top-5 H1 (constant w_kw) by composite", "H1")
    rank_table(R["h2_top"], "Top-5 H2 (query-conditional w(q)) by composite", "H2")

    L.append("## Promote-rule check — top H1 configs vs narrow recall=80\n")
    L.append("Rules (pre-committed): identifier/topical/tool recall@10 no worse (bootstrap CI of the "
             "delta includes 0 or favors RRF); paraphrase +20% relative **[margin PENDING re-ratification]**.\n")
    L.append("| id | id/top/tool no-worse | ident CI | tool CI | topical CI | paraphrase rel Δ |")
    L.append("|---|---|---|---|---|---|")
    for row in R["h1_top"][:5]:
        p = row["promote"]
        def ci_s(c):
            ci = p[f"{c}_no_worse"]["ci"]
            return f"[{ci['lo']:+.3f},{ci['hi']:+.3f}]{'✓' if p[f'{c}_no_worse']['met'] else '✗'}"
        rel = p["paraphrase_rel_delta"]
        rel_s = f"{rel * 100:+.1f}%" if rel is not None else "n/a"
        allnw = "YES" if p["identifier_tool_topical_all_no_worse"] else "no"
        L.append(f"| `{row['id']}` | {allnw} | {ci_s('identifier')} | {ci_s('tool')} | "
                 f"{ci_s('topical')} | {rel_s} |")
    L.append("\n> Paraphrase: the +20% relative margin is **flagged pending re-ratification** (plan promote "
             "rule 2). Reported as data, not a pass/fail verdict.\n")

    hv = R["h2_vs_h1"]
    L.append("## H2 vs H1 — does query-conditional w(q) earn its keep?\n")
    L.append(f"- H1 best (composite): `{hv['h1_best_composite']['id']}` "
             f"composite={hv['h1_best_composite']['composite']:.4f} "
             f"mixed={hv['h1_best_composite']['mixed_recall']:.3f}")
    L.append(f"- H2 best (composite): `{hv['h2_best_composite']['id']}` "
             f"composite={hv['h2_best_composite']['composite']:.4f} "
             f"mixed={hv['h2_best_composite']['mixed_recall']:.3f}")
    L.append(f"- Composite Δ (H2−H1): {hv['composite_delta_h2_minus_h1']:+.4f}")
    ms = hv["mixed_slice"]
    L.append(f"- **Mixed slice** (the only place H2 can win): H1 best-mixed "
             f"`{ms['h1_best_mixed']['id']}`={ms['h1_best_mixed']['mixed_recall']:.3f} vs "
             f"H2 best-mixed `{ms['h2_best_mixed']['id']}`={ms['h2_best_mixed']['mixed_recall']:.3f} "
             f"(Δ {ms['mixed_delta_h2_minus_h1']:+.3f})")
    L.append(f"- Rule: {hv['verdict_rule']}\n")

    L.append("## Anomalies\n")
    for a in R["anomalies"]:
        L.append(f"- {a}")
    L.append("")
    path.write_text("\n".join(L))


if __name__ == "__main__":
    main()
