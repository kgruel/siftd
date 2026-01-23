#!/usr/bin/env python3
"""Benchmark runner — compare semantic search quality across embeddings DBs."""

import argparse
import json
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def decode_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_similar(conn: sqlite3.Connection, query_embedding: list[float], limit: int = 10) -> list[dict]:
    cur = conn.execute("SELECT id, conversation_id, chunk_type, text, embedding FROM chunks")
    results = []
    for row in cur:
        stored = decode_embedding(row["embedding"])
        score = cosine_similarity(query_embedding, stored)
        results.append({
            "chunk_id": row["id"],
            "conversation_id": row["conversation_id"],
            "chunk_type": row["chunk_type"],
            "text": row["text"],
            "score": score,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def enrich_results(results: list[dict], main_db: sqlite3.Connection) -> list[dict]:
    """Add workspace path and date from main DB."""
    for r in results:
        cur = main_db.execute(
            """SELECT c.started_at, w.path as workspace_path
               FROM conversations c
               LEFT JOIN workspaces w ON c.workspace_id = w.id
               WHERE c.id = ?""",
            (r["conversation_id"],),
        )
        row = cur.fetchone()
        if row:
            r["started_at"] = row["started_at"]
            r["workspace_path"] = row["workspace_path"]
        else:
            r["started_at"] = None
            r["workspace_path"] = None
    return results


def load_queries(bench_dir: Path) -> dict:
    with open(bench_dir / "queries.json") as f:
        return json.load(f)


def run_benchmark(embed_db_paths: list[Path], main_db_path: Path) -> dict:
    """Run all queries against all embed DBs, return full results."""
    from embeddings.fastembed_backend import FastEmbedBackend

    print("Initializing embedding model...", file=sys.stderr)
    backend = FastEmbedBackend()

    bench_dir = Path(__file__).parent
    query_data = load_queries(bench_dir)

    main_db = sqlite3.connect(main_db_path)
    main_db.row_factory = sqlite3.Row

    all_results = {}

    for db_path in embed_db_paths:
        db_label = str(db_path)
        print(f"Benchmarking: {db_label}", file=sys.stderr)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        db_results = {}
        for group in query_data["groups"]:
            for query_text in group["queries"]:
                query_embedding = backend.embed_one(query_text)
                results = search_similar(conn, query_embedding, limit=10)
                results = enrich_results(results, main_db)
                # Store with snippet
                for r in results:
                    r["text_snippet"] = r["text"][:150]
                db_results[query_text] = results

        conn.close()
        all_results[db_label] = db_results

    main_db.close()
    return {"groups": query_data["groups"], "results": all_results}


def print_comparison(data: dict) -> None:
    """Print comparison report to stdout."""
    groups = data["groups"]
    results = data["results"]
    db_labels = list(results.keys())

    col_width = max(40, max(len(l) for l in db_labels) + 4)

    # Per-query sections
    for group in groups:
        print(f"\n{'=' * 80}")
        print(f"  {group['name']}: {group['description']}")
        print(f"{'=' * 80}")

        group_scores = {label: {"top1": [], "top5": []} for label in db_labels}

        for query_text in group["queries"]:
            print(f"\n  Q: {query_text}")
            print(f"  {'-' * 76}")

            # Header
            header = "  " + "".join(label.ljust(col_width) for label in db_labels)
            print(header)

            # Show top-5 side by side
            for rank in range(5):
                parts = []
                for label in db_labels:
                    query_results = results[label].get(query_text, [])
                    if rank < len(query_results):
                        r = query_results[rank]
                        score_str = f"{r['score']:.4f}"
                        snippet = r["text_snippet"][:35].replace("\n", " ")
                        parts.append(f"  {score_str} {snippet}".ljust(col_width))
                    else:
                        parts.append(" " * col_width)
                print("  " + "".join(parts))

            # Collect group scores
            for label in db_labels:
                query_results = results[label].get(query_text, [])
                if query_results:
                    group_scores[label]["top1"].append(query_results[0]["score"])
                    top5_avg = sum(r["score"] for r in query_results[:5]) / min(5, len(query_results))
                    group_scores[label]["top5"].append(top5_avg)

        # Per-group summary
        print(f"\n  Group Summary: {group['name']}")
        print(f"  {'DB':<{col_width - 2}} {'Avg Top-1':<12} {'Avg Top-5':<12}")
        for label in db_labels:
            t1 = group_scores[label]["top1"]
            t5 = group_scores[label]["top5"]
            avg_t1 = sum(t1) / len(t1) if t1 else 0
            avg_t5 = sum(t5) / len(t5) if t5 else 0
            print(f"  {label:<{col_width - 2}} {avg_t1:<12.4f} {avg_t5:<12.4f}")

    # Overall summary
    print(f"\n{'=' * 80}")
    print("  OVERALL SUMMARY")
    print(f"{'=' * 80}")
    print(f"\n  {'DB':<{col_width - 2}} {'Avg Score':<12} {'Variance':<12} {'Spread':<12}")

    for label in db_labels:
        all_scores = []
        top1_scores = []
        top10_scores = []
        for query_text, query_results in results[label].items():
            for r in query_results:
                all_scores.append(r["score"])
            if query_results:
                top1_scores.append(query_results[0]["score"])
                top10_avg = sum(r["score"] for r in query_results) / len(query_results)
                top10_scores.append(top10_avg)

        avg = sum(all_scores) / len(all_scores) if all_scores else 0
        variance = (sum((s - avg) ** 2 for s in all_scores) / len(all_scores)) if all_scores else 0
        avg_top1 = sum(top1_scores) / len(top1_scores) if top1_scores else 0
        avg_top10 = sum(top10_scores) / len(top10_scores) if top10_scores else 0
        spread = avg_top1 - avg_top10

        print(f"  {label:<{col_width - 2}} {avg:<12.4f} {variance:<12.6f} {spread:<12.4f}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Benchmark semantic search across embeddings DBs")
    parser.add_argument("embed_dbs", nargs="+", type=Path, help="Path(s) to embeddings DB files")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local/share/tbd/tbd.db",
        help="Path to main tbd.db (default: ~/.local/share/tbd/tbd.db)",
    )
    args = parser.parse_args()

    # Validate paths
    for p in args.embed_dbs:
        if not p.exists():
            print(f"Error: embeddings DB not found: {p}", file=sys.stderr)
            sys.exit(1)
    if not args.db.exists():
        print(f"Error: main DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    data = run_benchmark(args.embed_dbs, args.db)

    # Dump full results
    bench_dir = Path(__file__).parent
    results_path = bench_dir / "results.json"
    # Serialize without full text (just snippets)
    dump_data = {}
    for label, queries in data["results"].items():
        dump_data[label] = {}
        for query_text, query_results in queries.items():
            dump_data[label][query_text] = [
                {
                    "score": r["score"],
                    "conversation_id": r["conversation_id"],
                    "chunk_type": r["chunk_type"],
                    "text_snippet": r["text_snippet"],
                    "started_at": r.get("started_at"),
                    "workspace_path": r.get("workspace_path"),
                }
                for r in query_results
            ]
    with open(results_path, "w") as f:
        json.dump(dump_data, f, indent=2)
    print(f"Full results written to: {results_path}", file=sys.stderr)

    # Print comparison report
    print_comparison(data)


if __name__ == "__main__":
    main()
