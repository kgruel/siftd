#!/usr/bin/env python3
"""Fidelity gate: offline replica vs LIVE engine, exact top-10 (pre-committed).

The sweep fuses configs offline over cached artifacts; nothing it produces is
trusted until the offline replica reproduces the LIVE engine's top-10 EXACTLY at the
two configs that exist as engine knobs:

  (a) default narrow-then-rank  (SIFTD_HYBRID_STRATEGY unset/"narrow")
  (b) RRF                       (SIFTD_HYBRID_STRATEGY=rrf, k=60)

For a deterministic ~40-query probe (seed 20260705, all 16 topical + 6 each of the
other four classes) this runs, per config:
  * the REAL engine (siftd.api.search.hybrid_search) pointed at the snapshot + the
    arm's embed DB, with a CachedQueryBackend that serves the arm's cached query
    embedding (so the gate validates the ranking/fusion math, not the embedding
    service, and costs nothing) — the same code path ab_rrf.py exercises;
  * the offline replica (offline_lib.replica_narrow / replica_rrf) from cached
    artifacts + the in-memory chunk matrix.
Both are projected to conversation level (the ordered conversation_ids of the top-10
chunks — the granularity recall@10 is scored at) and compared position-by-position.

PYTHONHASHSEED is pinned to 0 (the script re-execs itself if needed): narrow-then-rank
has a latent hash-seed-dependent tie order in the vector candidate set (see
offline_lib determinism note); pinning it makes engine and replica agree and is the
honest fix, not a paper-over.

Exit nonzero if any query mismatches on any config; writes RUN/fidelity-report-<b>.json.

Usage (from worktree root):
    env -u VIRTUAL_ENV UV_NO_SYNC=1 uv run --no-sync python bench/stage1/fidelity_gate.py --backend voyage
"""

from __future__ import annotations

import os
import sys

# Pin set-iteration order BEFORE anything else, re-execing once if needed.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable, *sys.argv])

import argparse  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

# Isolate from production config/data: the engine is handed explicit db paths and an
# injected backend, so config is never consulted — but belt-and-suspenders.
_xdg = tempfile.mkdtemp(prefix="siftd-gate-")
os.environ["XDG_CONFIG_HOME"] = str(Path(_xdg) / "config")
os.environ["XDG_DATA_HOME"] = str(Path(_xdg) / "data")

import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import offline_lib  # noqa: E402
from offline_lib import ArmData, load_queries, replica_narrow, replica_rrf  # noqa: E402

from siftd.api.search import _RRF_K, hybrid_search  # noqa: E402
from siftd.search import MAX_MMR_CANDIDATES  # noqa: E402

RUN_DIR = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
SNAPSHOT = RUN_DIR / "siftd-snapshot.db"
SEED = 20260705
PROBE_PER_CLASS = 6  # for the four non-topical classes; topical is taken in full
N = 10


class CachedQueryBackend:
    """An EmbeddingBackend that serves the arm's cached query embeddings. Carries the
    arm's identity so validate_index_compat passes; embed_query returns the cached
    vector for the query text (identical to what the replica uses)."""

    def __init__(self, name: str, model: str, dimension: int, by_text: dict[str, list[float]]):
        self.name = name
        self.model = model
        self.dimension = dimension
        self._by_text = by_text

    def embed_query(self, text: str) -> list[float]:
        vec = self._by_text.get(text)
        if vec is None:
            raise KeyError(f"no cached embedding for query {text!r}")
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError("gate never embeds documents")


def select_probe(queries: list[dict]) -> list[dict]:
    """Deterministic probe: all topical + PROBE_PER_CLASS each of the others."""
    by_class: dict[str, list[dict]] = {}
    for q in queries:
        by_class.setdefault(q["class"], []).append(q)
    rng = random.Random(SEED)
    probe: list[dict] = []
    probe.extend(by_class.get("topical", []))
    for cls in ("identifier", "tool", "paraphrase", "mixed"):
        pool = sorted(by_class.get(cls, []), key=lambda q: q["qid"])
        k = min(PROBE_PER_CLASS, len(pool))
        probe.extend(rng.sample(pool, k))
    return probe


def conv_seq(chunks_or_results) -> list[str]:
    """Ordered conversation_ids of the top-N (position-sensitive, duplicates kept)."""
    out = []
    for item in chunks_or_results[:N]:
        cid = item.conversation_id if hasattr(item, "conversation_id") else item["conversation_id"]
        out.append(cid)
    return out


def run_config(
    config: str,
    probe: list[dict],
    arm: ArmData,
    emb_by_qid: dict[str, np.ndarray],
    fts_by_qid: dict[str, dict],
    embed_db: Path,
    backend: CachedQueryBackend,
) -> dict:
    """Run one config (narrow|rrf) over the probe; return the report block."""
    os.environ["SIFTD_HYBRID_STRATEGY"] = "rrf" if config == "rrf" else "narrow"
    mismatches: list[dict] = []
    n_match = 0
    for q in probe:
        qid = q["qid"]
        emb = emb_by_qid[qid]
        emb_list = emb.tolist()
        fts = fts_by_qid[qid]

        # --- LIVE engine (real code path) ---
        engine_chunks = hybrid_search(
            q["query"], db_path=SNAPSHOT, embed_db=embed_db, mode="hybrid", n=N,
            exclude_active=False, include_derivative=True, embed_backend=backend,
        )
        engine_seq = conv_seq(engine_chunks)

        # --- offline replica (from cached artifacts) ---
        if config == "narrow":
            rep = replica_narrow(arm, emb_list, fts["recall"]["conversation_ids"])
        else:
            kw = [{"conversation_id": h["c"], "event_id": h["e"]} for h in fts["content"]]
            rep = replica_rrf(arm, emb_list, kw)
        rep_seq = conv_seq(rep)

        if engine_seq == rep_seq:
            n_match += 1
        else:
            mismatches.append({
                "qid": qid,
                "class": q["class"],
                "query": q["query"][:120],
                "engine": engine_seq,
                "replica": rep_seq,
                "engine_chunk_ids": [getattr(c, "chunk_id", None) for c in engine_chunks[:N]],
                "replica_chunk_ids": [r.get("chunk_id") for r in rep[:N]],
            })
    return {
        "pass": not mismatches,
        "n_match": n_match,
        "n_total": len(probe),
        "mismatches": mismatches,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="voyage")
    args = ap.parse_args()

    assert _RRF_K == offline_lib.RRF_K, f"RRF k drift: engine {_RRF_K} != replica {offline_lib.RRF_K}"
    assert MAX_MMR_CANDIDATES == offline_lib.MMR_CAP, (
        f"MMR cap drift: engine {MAX_MMR_CANDIDATES} != replica {offline_lib.MMR_CAP}"
    )

    embed_db = RUN_DIR / f"embed-{args.backend}.db"
    art = RUN_DIR / f"artifacts-{args.backend}"
    if not embed_db.exists():
        sys.exit(f"arm DB not found: {embed_db}")
    if not (art / "query_emb.npy").exists():
        sys.exit(f"artifacts not found: {art} (run cache_artifacts.py first)")

    # Arm identity from meta (for the injected backend).
    econn = sqlite3.connect(f"file:{embed_db.as_posix()}?mode=ro&immutable=1", uri=True)
    meta = dict(econn.execute("SELECT key, value FROM index_meta").fetchall())
    econn.close()

    queries = load_queries(RUN_DIR)
    probe = select_probe(queries)
    print(f"probe: {len(probe)} queries "
          f"({', '.join(sorted({q['class'] for q in probe}))})", file=sys.stderr)

    # Cached query embeddings, keyed by qid and by text.
    cached_qids = [json.loads(line)["qid"] for line in (art / "queries.jsonl").read_text().splitlines() if line.strip()]
    cached_q = {json.loads(line)["qid"]: json.loads(line)
                for line in (art / "queries.jsonl").read_text().splitlines() if line.strip()}
    emb_arr = np.load(art / "query_emb.npy").astype(np.float32)
    emb_by_qid = {qid: emb_arr[i] for i, qid in enumerate(cached_qids)}
    by_text = {cached_q[qid]["query"]: emb_by_qid[qid].tolist() for qid in cached_qids}

    fts_by_qid = {}
    for line in (art / "fts.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            fts_by_qid[row["qid"]] = row

    print("loading arm matrix...", file=sys.stderr)
    arm = ArmData.load(embed_db)

    backend = CachedQueryBackend(
        name=meta["backend"], model=meta["model"], dimension=int(meta["dimension"]), by_text=by_text
    )

    report = {"backend": meta["backend"], "seed": SEED, "probe_size": len(probe), "configs": {}}
    all_pass = True
    for config in ("narrow", "rrf"):
        print(f"\n=== config: {config} ===", file=sys.stderr)
        block = run_config(config, probe, arm, emb_by_qid, fts_by_qid, embed_db, backend)
        report["configs"][config] = block
        status = "PASS" if block["pass"] else "FAIL"
        print(f"  {status}: {block['n_match']}/{block['n_total']} exact", file=sys.stderr)
        for mm in block["mismatches"]:
            print(f"  MISMATCH {mm['qid']} [{mm['class']}] {mm['query']!r}", file=sys.stderr)
            print(f"    engine : {mm['engine']}", file=sys.stderr)
            print(f"    replica: {mm['replica']}", file=sys.stderr)
        all_pass = all_pass and block["pass"]

    out = RUN_DIR / f"fidelity-report-{args.backend}.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport -> {out}", file=sys.stderr)
    print(f"GATE: {'PASS' if all_pass else 'FAIL'}", file=sys.stderr)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
