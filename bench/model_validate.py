#!/usr/bin/env python3
"""Validate a single model against the full production index.

Builds the full embeddings DB, runs the 70-query benchmark, and
reports results with per-group breakdown. Prints progress during build.

Usage:
    python bench/model_validate.py arctic-s
    python bench/model_validate.py arctic-s --max-convs 500
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.paths import data_dir  # noqa: E402
from siftd.storage.embeddings import open_embeddings_db, set_meta, store_chunk  # noqa: E402

MODELS = {
    "bge-small": "BAAI/bge-small-en-v1.5",
    "arctic-s": "Snowflake/snowflake-arctic-embed-s",
    "gte-base": "thenlper/gte-base",
}


def main():
    parser = argparse.ArgumentParser(description="Validate a model on the full index")
    parser.add_argument("model", choices=list(MODELS.keys()))
    parser.add_argument("--max-convs", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    main_db_path = data_dir() / "siftd.db"
    if not main_db_path.exists():
        print(f"Main DB not found: {main_db_path}", file=sys.stderr)
        return 1

    fastembed_name = MODELS[args.model]
    output_path = args.output or Path(f"/tmp/siftd-validate-{args.model}.db")

    # Load queries
    bench_dir = Path(__file__).parent
    with open(bench_dir / "queries.json") as f:
        query_data = json.load(f)
    all_queries = [q for g in query_data["groups"] for q in g["queries"]]
    print(f"Loaded {len(all_queries)} queries\n", file=sys.stderr)

    # Extract chunks
    print("Extracting chunks...", file=sys.stderr)
    main_conn = sqlite3.connect(main_db_path)
    main_conn.row_factory = sqlite3.Row

    from fastembed import TextEmbedding

    from siftd.embeddings.chunker import extract_exchange_window_chunks

    tokenizer = TextEmbedding("BAAI/bge-small-en-v1.5").model.tokenizer

    chunks = extract_exchange_window_chunks(
        main_conn, tokenizer,
        target_tokens=256, max_tokens=512, overlap_tokens=25,
    )
    main_conn.close()

    if args.max_convs:
        conv_ids = sorted({c["conversation_id"] for c in chunks})[:args.max_convs]
        conv_set = set(conv_ids)
        chunks = [c for c in chunks if c["conversation_id"] in conv_set]

    n_convs = len({c["conversation_id"] for c in chunks})
    print(f"  {len(chunks)} chunks from {n_convs} conversations\n", file=sys.stderr)

    # Init model
    print(f"Loading {args.model} ({fastembed_name})...", file=sys.stderr)
    from siftd.embeddings.fastembed_backend import FastEmbedBackend
    backend = FastEmbedBackend(model=fastembed_name)
    print(f"  dim={backend.dimension}\n", file=sys.stderr)

    # Build index with progress
    if output_path.exists():
        output_path.unlink()
    embed_conn = open_embeddings_db(output_path)

    texts = [c["text"] for c in chunks]
    batch_size = 256
    total = len(texts)
    all_embeddings = []

    print(f"Embedding {total} chunks...", file=sys.stderr)
    t0 = time.time()
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        all_embeddings.extend(backend.embed(batch))
        elapsed = time.time() - t0
        done = len(all_embeddings)
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"\r  {done}/{total} ({rate:.1f}/s, ETA {eta:.0f}s)", end="", file=sys.stderr)
    build_time = time.time() - t0
    print(f"\n  Done in {build_time:.0f}s\n", file=sys.stderr)

    # Write to DB
    print("Writing DB...", file=sys.stderr)
    for chunk, embedding in zip(chunks, all_embeddings):
        store_chunk(
            embed_conn,
            conversation_id=chunk["conversation_id"],
            chunk_type=chunk["chunk_type"],
            text=chunk["text"],
            embedding=embedding,
            token_count=chunk.get("token_count", 0),
            source_ids=chunk.get("source_ids"),
        )
    embed_conn.commit()
    set_meta(embed_conn, "backend", backend.name)
    set_meta(embed_conn, "model", backend.model)
    set_meta(embed_conn, "dimension", str(backend.dimension))
    embed_conn.commit()
    embed_conn.close()

    db_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  {db_size_mb:.1f}MB\n", file=sys.stderr)

    # Benchmark
    import numpy as np

    print("Running benchmark...", file=sys.stderr)
    conn = sqlite3.connect(output_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, conversation_id, embedding FROM chunks").fetchall()
    conn.close()

    dim = len(rows[0]["embedding"]) // 4
    blob = b"".join(r["embedding"] for r in rows)
    matrix = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), dim).copy()
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    matrix /= norms
    conv_ids = [r["conversation_id"] for r in rows]

    query_results = []
    for query_text in all_queries:
        qvec = np.array(backend.embed_one(query_text), dtype=np.float32)
        qnorm = np.linalg.norm(qvec)
        if qnorm > 0:
            qvec /= qnorm
        scores = matrix @ qvec
        top_idx = np.argpartition(scores, -10)[-10:]
        top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]

        query_results.append({
            "query": query_text,
            "top1": float(scores[top_idx[0]]),
            "avg_top5": mean(float(scores[i]) for i in top_idx[:5]),
        })

    avg_top1 = mean(r["top1"] for r in query_results)
    avg_top5 = mean(r["avg_top5"] for r in query_results)

    # Per-group breakdown
    qi = 0
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  {'Group':<30} {'avg_top1':>9}", file=sys.stderr)
    print(f"  {'-'*56}", file=sys.stderr)
    group_results = {}
    for group in query_data["groups"]:
        group_qr = query_results[qi : qi + len(group["queries"])]
        qi += len(group["queries"])
        g_avg = mean(r["top1"] for r in group_qr) if group_qr else 0
        group_results[group["name"]] = round(g_avg, 4)
        print(f"  {group['name']:<30} {g_avg:>9.4f}", file=sys.stderr)

    print(f"  {'-'*56}", file=sys.stderr)
    print(f"  {'OVERALL':<30} {avg_top1:>9.4f}", file=sys.stderr)
    print(f"  {'avg_top5':<30} {avg_top5:>9.4f}", file=sys.stderr)
    print(f"  {'build_time':<30} {build_time:>8.0f}s", file=sys.stderr)
    print(f"  {'db_size':<30} {db_size_mb:>7.1f}MB", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "model": args.model,
            "fastembed_name": fastembed_name,
            "dimension": backend.dimension,
            "chunks": len(chunks),
            "conversations": n_convs,
            "build_time_s": round(build_time, 1),
            "db_size_mb": round(db_size_mb, 1),
            "avg_top1": round(avg_top1, 6),
            "avg_top5": round(avg_top5, 6),
            "by_group": group_results,
        }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
