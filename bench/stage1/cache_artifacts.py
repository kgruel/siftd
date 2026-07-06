#!/usr/bin/env python3
"""Cache per-query search artifacts for the stage-1 sweep + fidelity gate.

For every ground-truth query (all 5 classes, 632 total), fetch ONCE against an arm
and store to ``RUN/artifacts-<backend>/``:

  * query_emb.npy    — the query embedding (arm backend, "query" intent), one row per
                       query, aligned to queries.jsonl. This is the only PAID call, so
                       it is cached first and reruns are free.
  * queries.jsonl    — qid/class/query/labels/meta, row-aligned to query_emb.npy.
  * fts.jsonl        — per query: the FTS5 *recall* list (conversation-level, AND->OR
                       fallback, the narrow-then-rank input) AND the FTS5 *content*
                       list (event-level bm25, the RRF keyword input). Both cached
                       deep (limit 500) so any swept pool depth is a prefix.
  * vector_top1000   — per query: the top-1000 global cosine (chunk_id, score). A
                       convenience/validation artifact; the sweep + gate recompute
                       cosine over the in-memory matrix from query_emb.npy as needed,
                       and MMR reads chunk embeddings straight from that matrix by
                       chunk_id -> row index (offline_lib.ArmData) — so no per-chunk
                       embedding is cached separately.

Design choice (documented per the task): the expensive, network-bound step is the
query embedding; the FTS lists are cheap SQL and the vector matvec is milliseconds
over the loaded matrix. So the cache stores query embeddings durably and recomputes
FTS + vector every run (they are deterministic and fast) — the "resumable" property
that matters (never re-pay Voyage) is guaranteed by the query_emb.npy skip.

Usage (from worktree root; sources the key without printing it):
    set -a; source ~/.config/siftd/bench-keys.env; set +a
    export SIFTD_BENCH_EMBED_KEY="$SIFTD_BENCH_KEY_VOYAGER"
    env -u VIRTUAL_ENV UV_NO_SYNC=1 uv run --no-sync python bench/stage1/cache_artifacts.py --backend voyage
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from offline_lib import ArmData, load_queries  # noqa: E402

from siftd.storage.fts import (  # noqa: E402
    fts5_recall_details,
    search_content,
)

RUN_DIR = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
SNAPSHOT = RUN_DIR / "siftd-snapshot.db"
FTS_DEPTH = 500  # cache the FTS recall + content lists this deep (>> any swept pool)
VECTOR_DEPTH = 1000  # cache the global cosine ranking this deep


def make_backend(name: str):
    """Construct the arm's embedding backend (mirrors build_index.make_backend, remote
    arms only — the cache is for a remote arm's query embeddings)."""
    if name == "local":
        from siftd.embeddings.fastembed_backend import FastEmbedBackend

        return FastEmbedBackend()
    from siftd.embeddings.presets import get_preset
    from siftd.embeddings.remote import RemoteBackend

    preset = get_preset(name)
    if preset is None:
        sys.exit(f"unknown backend {name!r}")
    api_key = os.environ.get("SIFTD_BENCH_EMBED_KEY", "")
    if not api_key:
        sys.exit("remote backend needs SIFTD_BENCH_EMBED_KEY in the environment")
    return RemoteBackend(
        preset_name=preset.name,
        base_url=preset.base_url,
        model=preset.default_model,
        intent_style=preset.intent_style,
        max_batch=preset.max_batch,
        api_key=api_key,
        dimension=preset.default_dimensions,
    )


def load_cached_emb(queries: list[dict], out_dir: Path) -> np.ndarray | None:
    """Return the cached query embeddings if present and aligned to ``queries``, else
    None. Checked BEFORE building the backend so a rerun never needs the API key."""
    emb_path = out_dir / "query_emb.npy"
    q_path = out_dir / "queries.jsonl"
    if not (emb_path.exists() and q_path.exists()):
        return None
    cached_qids = [json.loads(line)["qid"] for line in q_path.read_text().splitlines() if line.strip()]
    arr = np.load(emb_path)
    if cached_qids == [q["qid"] for q in queries] and arr.shape[0] == len(queries):
        print(f"reusing cached query embeddings ({arr.shape[0]} x {arr.shape[1]})", file=sys.stderr)
        return arr.astype(np.float32)
    print("query set changed; re-embedding", file=sys.stderr)
    return None


def embed_queries(backend, queries: list[dict], out_dir: Path) -> np.ndarray:
    """Embed all queries with the arm backend at "query" intent, batched, and cache to
    query_emb.npy + queries.jsonl (never re-pay: callers check load_cached_emb first)."""
    emb_path = out_dir / "query_emb.npy"
    q_path = out_dir / "queries.jsonl"
    texts = [q["query"] for q in queries]
    t0 = time.time()
    # "query" intent, batched by the backend's max_batch (Voyage: 128 -> ~5 requests).
    vecs = backend._embed(texts, intent="query")  # noqa: SLF001 — bench harness, batched query intent
    if len(vecs) != len(texts):
        raise RuntimeError(f"embed count mismatch: {len(vecs)} != {len(texts)}")
    arr = np.asarray(vecs, dtype=np.float32)
    print(f"embedded {len(texts)} queries -> {arr.shape} in {time.time() - t0:.1f}s", file=sys.stderr)

    np.save(emb_path, arr)
    with q_path.open("w") as f:
        for q in queries:
            f.write(json.dumps(q) + "\n")
    return arr


def cache_fts(conn: sqlite3.Connection, queries: list[dict], out_dir: Path) -> None:
    """Cache the FTS5 recall list (narrow input) + content list (RRF input) per query."""
    path = out_dir / "fts.jsonl"
    t0 = time.time()
    with path.open("w") as f:
        for i, q in enumerate(queries):
            recall = fts5_recall_details(conn, q["query"], limit=FTS_DEPTH)
            content = search_content(conn, q["query"], limit=FTS_DEPTH)
            row = {
                "qid": q["qid"],
                "recall": {"conversation_ids": recall.conversation_ids, "mode": recall.mode},
                "content": [
                    {"c": h["conversation_id"], "e": h["event_id"], "k": h["kind"], "rank": h["rank"]}
                    for h in content
                ],
            }
            f.write(json.dumps(row) + "\n")
            if (i + 1) % 100 == 0:
                print(f"  fts {i + 1}/{len(queries)}", file=sys.stderr)
    print(f"cached fts.jsonl in {time.time() - t0:.1f}s", file=sys.stderr)


def cache_vector(arm: ArmData, emb: np.ndarray, queries: list[dict], out_dir: Path) -> None:
    """Cache the top-VECTOR_DEPTH global cosine ranking (chunk_id, score) per query.

    Computed the same way search_similar ranks the full set (normalized matrix @
    normalized query, argsort top-k) so the artifact is a genuine validation of the
    engine's vector stage, not an independent metric."""
    path = out_dir / "vector_top1000.jsonl"
    t0 = time.time()
    with path.open("w") as f:
        for i, q in enumerate(queries):
            qv = emb[i]
            qn = np.linalg.norm(qv)
            scores = arm.matrix @ (qv / qn) if qn else np.zeros(arm.matrix.shape[0], dtype=np.float32)
            if scores.shape[0] <= VECTOR_DEPTH:
                order = np.argsort(-scores)
            else:
                part = np.argpartition(-scores, VECTOR_DEPTH)[:VECTOR_DEPTH]
                order = part[np.argsort(-scores[part])]
            top = [[arm.chunk_ids[int(j)], float(scores[int(j)])] for j in order]
            f.write(json.dumps({"qid": q["qid"], "top": top}) + "\n")
            if (i + 1) % 100 == 0:
                print(f"  vector {i + 1}/{len(queries)}", file=sys.stderr)
    print(f"cached vector_top1000.jsonl in {time.time() - t0:.1f}s", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="voyage", help="arm backend (a preset name, e.g. voyage)")
    args = ap.parse_args()

    embed_db = RUN_DIR / f"embed-{args.backend}.db"
    if not embed_db.exists():
        sys.exit(f"arm DB not found: {embed_db}")
    out_dir = RUN_DIR / f"artifacts-{args.backend}"
    out_dir.mkdir(exist_ok=True)

    queries = load_queries(RUN_DIR)
    print(f"queries: {len(queries)}", file=sys.stderr)

    # Build the (paid) backend only if the embeddings are not already cached.
    emb = load_cached_emb(queries, out_dir)
    if emb is None:
        backend = make_backend(args.backend)
        print(f"backend: {backend.name} {backend.model} dim={backend.dimension}", file=sys.stderr)
        emb = embed_queries(backend, queries, out_dir)

    print("loading arm matrix...", file=sys.stderr)
    arm = ArmData.load(embed_db)
    print(f"arm: {len(arm.chunk_ids)} chunks x {arm.dim} dim", file=sys.stderr)
    if arm.dim != emb.shape[1]:
        sys.exit(f"dim mismatch: arm {arm.dim} != query emb {emb.shape[1]}")

    # Arm identity for the manifest (from index_meta, so a cached rerun needs no backend).
    ident = dict(sqlite3.connect(
        f"file:{embed_db.as_posix()}?mode=ro&immutable=1", uri=True
    ).execute("SELECT key, value FROM index_meta").fetchall())

    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cache_fts(conn, queries, out_dir)
    finally:
        conn.close()

    cache_vector(arm, emb, queries, out_dir)

    manifest = {
        "backend": ident.get("backend"),
        "model": ident.get("model"),
        "dimension": int(ident["dimension"]) if ident.get("dimension") else None,
        "n_queries": len(queries),
        "n_chunks": len(arm.chunk_ids),
        "fts_depth": FTS_DEPTH,
        "vector_depth": VECTOR_DEPTH,
        "arm_db": embed_db.name,
        "snapshot": SNAPSHOT.name,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"done -> {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
