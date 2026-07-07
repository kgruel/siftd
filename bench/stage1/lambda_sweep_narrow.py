#!/usr/bin/env python3
"""Narrow-path MMR-lambda sweep (post-dedup-rollup question, 2026-07-07).

The stage-1/2 sweep (sweep.py) only ever ran the narrow-then-rank baseline at
DEFAULT_LAMBDA=0.7 (offline_lib.replica_narrow's default) -- LAMBDAS=["off",0.5,0.7,1.0]
in sweep.py is the RRF/H1 fusion grid's post-fusion MMR knob, never applied to narrow.
This script fills that gap: sweep lambda_ on replica_narrow itself, recall=80 (ship
value), at conversation-unit (narrow's MMR already dedups its output at recall@10, so
unit=conversation == unit=slot-deduped here, same as sweep.py's comment establishes).

Usage:
    env -u VIRTUAL_ENV UV_NO_SYNC=1 uv run --no-sync python bench/stage1/lambda_sweep_narrow.py --backend voyage
    env -u VIRTUAL_ENV UV_NO_SYNC=1 uv run --no-sync python bench/stage1/lambda_sweep_narrow.py --backend local
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

from offline_lib import ArmData, replica_narrow  # noqa: E402

RUN_DIR = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
N = 10
RECALL = 80  # ship value (narrow's candidate-width knob, held fixed)
SEED = 20260705
BOOT = 1000
CLASSES = ["identifier", "tool", "topical", "paraphrase", "mixed"]
LAMBDAS = [0.5, 0.7, 0.85, 0.95, 1.0]  # 0.7 = shipped/DEFAULT_LAMBDA


def recall_at_10(convs_in_order: list[str], labels: set[str]) -> float:
    got = set(convs_in_order[:N])
    denom = min(N, len(labels)) or 1
    return len(got & labels) / denom


def mrr_at_10(convs_in_order: list[str], labels: set[str]) -> float:
    for i, c in enumerate(convs_in_order[:N]):
        if c in labels:
            return 1.0 / (i + 1)
    return 0.0


def load_artifacts(backend: str):
    art = RUN_DIR / f"artifacts-{backend}"
    embed_db = RUN_DIR / f"embed-{backend}.db"
    q_lines = [json.loads(x) for x in (art / "queries.jsonl").read_text().splitlines() if x.strip()]
    emb = np.load(art / "query_emb.npy").astype(np.float32)
    fts_by_qid: dict[str, dict] = {}
    for line in (art / "fts.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            fts_by_qid[row["qid"]] = row
    print("loading arm matrix...", file=sys.stderr)
    arm = ArmData.load(embed_db)
    arm.event_map()
    return q_lines, emb, fts_by_qid, arm


def make_resamples(class_idx: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)
    out: dict[str, np.ndarray] = {}
    for cls in CLASSES:
        n = len(class_idx[cls])
        out[cls] = rng.integers(0, n, size=(BOOT, n)) if n else np.zeros((BOOT, 0), dtype=int)
    return out


def class_ci(delta: np.ndarray, cls_positions: np.ndarray, resample: np.ndarray) -> tuple[float, float, float]:
    d = delta[cls_positions]
    if d.size == 0:
        return 0.0, 0.0, 0.0
    boot = d[resample].mean(axis=1)
    return float(d.mean()), float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="voyage")
    args = ap.parse_args()

    q_lines, emb, fts_by_qid, arm = load_artifacts(args.backend)
    nq = len(q_lines)
    labels = [set(q["labels"]) for q in q_lines]
    classes = [q["class"] for q in q_lines]
    class_idx = {cls: np.array([i for i in range(nq) if classes[i] == cls], dtype=int) for cls in CLASSES}
    print(f"n={nq} classes=" + ", ".join(f"{c}={len(class_idx[c])}" for c in CLASSES), file=sys.stderr)

    recall_by_lam = {lam: np.zeros(nq, dtype=np.float64) for lam in LAMBDAS}
    mrr_by_lam = {lam: np.zeros(nq, dtype=np.float64) for lam in LAMBDAS}

    t0 = time.time()
    for qi in range(nq):
        qv_list = emb[qi].tolist()
        fts = fts_by_qid[q_lines[qi]["qid"]]
        recall_ids = fts["recall"]["conversation_ids"]
        lab = labels[qi]
        for lam in LAMBDAS:
            res = replica_narrow(arm, qv_list, recall_ids, n=N, recall=RECALL, lambda_=lam)
            convs = list(dict.fromkeys(x["conversation_id"] for x in res))  # dedup rollup (== narrow's MMR output)
            recall_by_lam[lam][qi] = recall_at_10(convs, lab)
            mrr_by_lam[lam][qi] = mrr_at_10(convs, lab)
        if (qi + 1) % 50 == 0:
            rate = (qi + 1) / (time.time() - t0)
            print(f"  {qi + 1}/{nq} ({rate:.1f} q/s)", file=sys.stderr)
    print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)

    def per_class(arr: np.ndarray) -> dict[str, float]:
        return {cls: float(arr[class_idx[cls]].mean()) if len(class_idx[cls]) else 0.0 for cls in CLASSES}

    def composite(pc: dict[str, float]) -> float:
        return float(np.mean([pc[c] for c in CLASSES]))

    base = recall_by_lam[0.7]
    resamples = make_resamples(class_idx)

    rows = []
    for lam in LAMBDAS:
        pc = per_class(recall_by_lam[lam])
        pc_mrr = per_class(mrr_by_lam[lam])
        delta = recall_by_lam[lam] - base
        ci = {}
        for cls in CLASSES:
            md, lo, hi = class_ci(delta, class_idx[cls], resamples[cls])
            ci[cls] = {"mean_delta": md, "lo": lo, "hi": hi}
        rows.append({
            "lambda": lam, "composite": composite(pc), "per_class_recall": pc,
            "mrr": pc_mrr, "ci_vs_l0.7": ci,
        })

    out = {
        "backend": args.backend, "n_queries": nq, "recall_knob": RECALL, "n": N,
        "class_counts": {c: int(len(class_idx[c])) for c in CLASSES},
        "lambdas": LAMBDAS, "baseline_lambda": 0.7,
        "bootstrap": {"resamples": BOOT, "seed": SEED},
        "rows": rows,
    }
    out_path = RUN_DIR / f"lambda-sweep-narrow-{args.backend}.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"wrote {out_path}", file=sys.stderr)

    print(f"\n=== narrow lambda sweep, {args.backend} ===")
    print(f"{'lambda':>8} {'composite':>10} " + " ".join(f"{c[:4]:>8}" for c in CLASSES))
    for row in rows:
        pc = row["per_class_recall"]
        tag = " (shipped)" if row["lambda"] == 0.7 else ""
        print(f"{row['lambda']:>8} {row['composite']:>10.4f} " + " ".join(f"{pc[c]:>8.3f}" for c in CLASSES) + tag)
    print("\nparaphrase CI vs lambda=0.7:")
    for row in rows:
        ci = row["ci_vs_l0.7"]["paraphrase"]
        print(f"  lambda={row['lambda']}: delta={ci['mean_delta']:+.4f} CI=[{ci['lo']:+.4f},{ci['hi']:+.4f}]")


if __name__ == "__main__":
    main()
