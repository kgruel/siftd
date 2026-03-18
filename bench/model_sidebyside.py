#!/usr/bin/env python3
"""Side-by-side comparison of two embedding models on the same queries.

Compares rank agreement, finds divergent results, and prints qualitative
examples for human review.

Usage:
    python bench/model_sidebyside.py
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np  # noqa: E402

from siftd.paths import data_dir  # noqa: E402


def load_index(db_path):
    """Load embeddings matrix and metadata from an index DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, conversation_id, text, embedding FROM chunks"
    ).fetchall()
    conn.close()

    dim = len(rows[0]["embedding"]) // 4
    blob = b"".join(r["embedding"] for r in rows)
    matrix = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), dim).copy()
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    matrix /= norms

    return {
        "matrix": matrix,
        "conv_ids": [r["conversation_id"] for r in rows],
        "texts": [r["text"] for r in rows],
        "dim": dim,
    }


def search(index, query_vec, k=10):
    """Return top-k results as [(conv_id, score, text), ...]."""
    scores = index["matrix"] @ query_vec
    top_idx = np.argpartition(scores, -k)[-k:]
    top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]

    # Deduplicate by conversation, keep best chunk per conversation
    seen = set()
    results = []
    for i in top_idx:
        cid = index["conv_ids"][i]
        if cid not in seen:
            seen.add(cid)
            results.append((cid, float(scores[i]), index["texts"][i]))
    # May need more if dedup reduced count — scan further
    if len(results) < k:
        all_sorted = np.argsort(scores)[::-1]
        for i in all_sorted:
            cid = index["conv_ids"][i]
            if cid not in seen:
                seen.add(cid)
                results.append((cid, float(scores[i]), index["texts"][i]))
                if len(results) >= k:
                    break

    return results[:k]


def main():
    main_db = data_dir() / "siftd.db"
    bge_db = data_dir() / "embeddings.db"
    arctic_db = Path("/tmp/siftd-validate-arctic-s.db")

    if not all(p.exists() for p in [main_db, bge_db, arctic_db]):
        print("Missing DB files", file=sys.stderr)
        return 1

    # Load both indexes
    print("Loading bge-small index...", file=sys.stderr)
    bge = load_index(bge_db)
    print(f"  {bge['matrix'].shape[0]} chunks, dim={bge['dim']}", file=sys.stderr)

    print("Loading arctic-s index...", file=sys.stderr)
    arctic = load_index(arctic_db)
    print(f"  {arctic['matrix'].shape[0]} chunks, dim={arctic['dim']}", file=sys.stderr)

    # Load conversation metadata
    conn = sqlite3.connect(main_db)
    conn.row_factory = sqlite3.Row
    meta_rows = conn.execute(
        "SELECT c.id, c.started_at, w.path as workspace "
        "FROM conversations c LEFT JOIN workspaces w ON w.id = c.workspace_id"
    ).fetchall()
    conn.close()
    meta = {r["id"]: {"started_at": r["started_at"], "workspace": r["workspace"]} for r in meta_rows}

    # Load queries
    bench_dir = Path(__file__).parent
    with open(bench_dir / "queries.json") as f:
        query_data = json.load(f)

    # Initialize both backends for query embedding
    from siftd.embeddings.fastembed_backend import FastEmbedBackend
    print("\nLoading backends...", file=sys.stderr)
    bge_backend = FastEmbedBackend(model="BAAI/bge-small-en-v1.5")
    arctic_backend = FastEmbedBackend(model="Snowflake/snowflake-arctic-embed-s")

    # Run all queries through both models
    all_queries = [q for g in query_data["groups"] for q in g["queries"]]

    same_top1 = 0
    same_top3 = 0
    bge_wins = 0  # bge top-1 conv not in arctic top-5
    arctic_wins = 0  # arctic top-1 conv not in bge top-5
    divergent_examples = []

    for qi, query_text in enumerate(all_queries):
        bge_vec = np.array(bge_backend.embed_one(query_text), dtype=np.float32)
        bge_norm = np.linalg.norm(bge_vec)
        if bge_norm > 0:
            bge_vec /= bge_norm

        arctic_vec = np.array(arctic_backend.embed_one(query_text), dtype=np.float32)
        arctic_norm = np.linalg.norm(arctic_vec)
        if arctic_norm > 0:
            arctic_vec /= arctic_norm

        bge_results = search(bge, bge_vec, k=5)
        arctic_results = search(arctic, arctic_vec, k=5)

        bge_top1_conv = bge_results[0][0]
        arctic_top1_conv = arctic_results[0][0]
        bge_top3_convs = {r[0] for r in bge_results[:3]}
        arctic_top3_convs = {r[0] for r in arctic_results[:3]}
        bge_top5_convs = {r[0] for r in bge_results[:5]}
        arctic_top5_convs = {r[0] for r in arctic_results[:5]}

        if bge_top1_conv == arctic_top1_conv:
            same_top1 += 1
        if bge_top3_convs == arctic_top3_convs:
            same_top3 += 1

        if bge_top1_conv not in arctic_top5_convs:
            bge_wins += 1
        if arctic_top1_conv not in bge_top5_convs:
            arctic_wins += 1

        # Collect divergent examples (different top-1)
        if bge_top1_conv != arctic_top1_conv:
            divergent_examples.append({
                "query": query_text,
                "bge_top1": {
                    "conv_id": bge_top1_conv[:12],
                    "score": round(bge_results[0][1], 4),
                    "workspace": (meta.get(bge_top1_conv) or {}).get("workspace", "?"),
                    "snippet": bge_results[0][2][:120].replace("\n", " "),
                },
                "arctic_top1": {
                    "conv_id": arctic_top1_conv[:12],
                    "score": round(arctic_results[0][1], 4),
                    "workspace": (meta.get(arctic_top1_conv) or {}).get("workspace", "?"),
                    "snippet": arctic_results[0][2][:120].replace("\n", " "),
                },
                "overlap_top5": len(bge_top5_convs & arctic_top5_convs),
            })

        if (qi + 1) % 10 == 0:
            print(f"  {qi + 1}/{len(all_queries)} queries processed", file=sys.stderr)

    # Summary
    n = len(all_queries)
    print(f"\n{'=' * 64}", file=sys.stderr)
    print(f"  Rank Agreement ({n} queries)", file=sys.stderr)
    print(f"  {'-' * 58}", file=sys.stderr)
    print(f"  Same top-1 conversation:   {same_top1}/{n} ({same_top1/n*100:.0f}%)", file=sys.stderr)
    print(f"  Same top-3 set:            {same_top3}/{n} ({same_top3/n*100:.0f}%)", file=sys.stderr)
    print(f"  bge top-1 NOT in arctic-5: {bge_wins}/{n} ({bge_wins/n*100:.0f}%)", file=sys.stderr)
    print(f"  arctic top-1 NOT in bge-5: {arctic_wins}/{n} ({arctic_wins/n*100:.0f}%)", file=sys.stderr)
    print(f"  Divergent top-1:           {len(divergent_examples)}/{n}", file=sys.stderr)
    print(f"{'=' * 64}\n", file=sys.stderr)

    # Print divergent examples for human review
    print(f"{'=' * 64}")
    print(f"  DIVERGENT TOP-1 RESULTS ({len(divergent_examples)} queries)")
    print(f"{'=' * 64}\n")

    for ex in divergent_examples[:20]:  # cap at 20 for readability
        print(f"  Query: {ex['query']}")
        print(f"  top-5 overlap: {ex['overlap_top5']}/5")
        b = ex["bge_top1"]
        a = ex["arctic_top1"]
        print(f"  bge-small:  [{b['score']:.4f}] {b['conv_id']}  {b['workspace']}")
        print(f"              {b['snippet']}")
        print(f"  arctic-s:   [{a['score']:.4f}] {a['conv_id']}  {a['workspace']}")
        print(f"              {a['snippet']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
