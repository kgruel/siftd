#!/usr/bin/env python3
"""Run queries against embeddings DBs and compare results."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

# bench/ is not a package — add src/ to path so siftd imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.storage.embeddings import search_similar  # noqa: E402


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


def get_chunk_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    return count


def get_tokenizer(backend):
    """Get the tokenizer from the fastembed backend for token counting."""
    tokenizer = backend._embedder.model.tokenizer
    tokenizer.no_truncation()
    return tokenizer


def count_tokens(tokenizer, text: str) -> int:
    """Count tokens in text using the model's tokenizer."""
    return len(tokenizer.encode(text).ids)


def get_chunk_token_stats(db_path: Path, tokenizer) -> dict:
    """Compute token count statistics for all chunks in a DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT text FROM chunks")
    token_counts = [count_tokens(tokenizer, row["text"]) for row in cur]
    conn.close()

    if not token_counts:
        return {"min": 0, "max": 0, "mean": 0, "median": 0, "p95": 0}

    sorted_counts = sorted(token_counts)
    p95_idx = int(len(sorted_counts) * 0.95)
    return {
        "min": sorted_counts[0],
        "max": sorted_counts[-1],
        "mean": round(mean(sorted_counts), 1),
        "median": round(median(sorted_counts), 1),
        "p95": sorted_counts[min(p95_idx, len(sorted_counts) - 1)],
    }


def compute_diversity_metrics(query_results: list[dict]) -> dict:
    """Compute diversity metrics for a single query's top-k results.

    Measures:
    - conversation_redundancy: fraction of top-10 from same conversation as rank-1
    - unique_workspace_count: distinct workspaces in top-10
    """
    if not query_results:
        return {}

    top10 = query_results[:10]

    rank1_conv = top10[0]["conversation_id"]
    same_conv_count = sum(1 for r in top10 if r["conversation_id"] == rank1_conv)
    conversation_redundancy = same_conv_count / len(top10)

    workspaces = {r.get("workspace_path") or "(none)" for r in top10}
    unique_workspace_count = len(workspaces)

    return {
        "conversation_redundancy": round(conversation_redundancy, 4),
        "unique_workspace_count": unique_workspace_count,
    }


def run_bench(
    embed_db_paths: list[Path],
    main_db_path: Path,
    backend,
    tokenizer,
    *,
    hybrid: bool = False,
    recall_limit: int = 80,
    rerank_mode: str = "relevance",
    lambda_: float = 0.7,
) -> dict:
    """Run all queries against all embed DBs, return full results."""
    bench_dir = Path(__file__).parent
    query_data = load_queries(bench_dir)

    use_mmr = rerank_mode == "mmr"
    if use_mmr:
        from siftd.search import mmr_rerank

    main_db = sqlite3.connect(main_db_path)
    main_db.row_factory = sqlite3.Row

    all_results = {}
    recall_meta = {}
    diversity_metrics = {}

    for db_path in embed_db_paths:
        db_label = str(db_path)
        print(f"Running: {db_label}", file=sys.stderr)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        db_results = {}
        db_recall_meta = {}
        db_diversity = {}
        for group in query_data["groups"]:
            for query_text in group["queries"]:
                conversation_ids = None
                if hybrid:
                    from siftd.storage.fts import fts5_recall_conversations
                    fts5_ids, fts5_mode = fts5_recall_conversations(main_db, query_text, limit=recall_limit)
                    db_recall_meta[query_text] = {
                        "fts5_conversations": len(fts5_ids),
                        "fts5_mode": fts5_mode,
                    }
                    if fts5_ids:
                        conversation_ids = fts5_ids

                query_embedding = backend.embed_one(query_text)

                # MMR needs more candidates to rerank from
                search_limit = 100 if use_mmr else 10
                results = search_similar(
                    conn, query_embedding, limit=search_limit,
                    conversation_ids=conversation_ids,
                    include_embeddings=use_mmr,
                )

                results = enrich_results(results, main_db)
                for r in results:
                    r["token_count"] = count_tokens(tokenizer, r["text"])

                if use_mmr:
                    reranked = mmr_rerank(
                        results,
                        query_embedding,
                        lambda_=lambda_,
                        limit=10,
                    )
                    db_diversity[query_text] = compute_diversity_metrics(reranked)
                    db_results[query_text] = reranked
                else:
                    top10 = results[:10]
                    db_diversity[query_text] = compute_diversity_metrics(top10)
                    db_results[query_text] = top10

        conn.close()
        all_results[db_label] = db_results
        recall_meta[db_label] = db_recall_meta
        diversity_metrics[db_label] = db_diversity

    main_db.close()
    return {
        "groups": query_data["groups"],
        "results": all_results,
        "recall_meta": recall_meta,
        "diversity_metrics": diversity_metrics,
    }


def build_output(data: dict, meta: dict) -> dict:
    """Build the JSON output from raw run data."""
    groups = data["groups"]
    results = data["results"]
    db_labels = list(results.keys())

    # Build summary.by_db
    by_db = {}
    for label in db_labels:
        all_scores = []
        top1_scores = []
        top5_scores = []
        for query_text, query_results in results[label].items():
            for r in query_results:
                all_scores.append(r["score"])
            if query_results:
                top1_scores.append(query_results[0]["score"])
                top5_avg = sum(r["score"] for r in query_results[:5]) / min(5, len(query_results))
                top5_scores.append(top5_avg)

        avg = sum(all_scores) / len(all_scores) if all_scores else 0
        variance = (sum((s - avg) ** 2 for s in all_scores) / len(all_scores)) if all_scores else 0
        avg_top1 = sum(top1_scores) / len(top1_scores) if top1_scores else 0
        avg_top5 = sum(top5_scores) / len(top5_scores) if top5_scores else 0
        avg_top10 = sum(
            sum(r["score"] for r in qr) / len(qr)
            for qr in results[label].values() if qr
        ) / max(1, sum(1 for qr in results[label].values() if qr))
        spread = avg_top1 - avg_top10

        by_db[label] = {
            "avg_score": round(avg, 6),
            "variance": round(variance, 8),
            "spread": round(spread, 6),
            "avg_top1": round(avg_top1, 6),
            "avg_top5": round(avg_top5, 6),
        }

    # Aggregate diversity metrics per DB
    div_metrics = data.get("diversity_metrics", {})
    for label in db_labels:
        db_div = div_metrics.get(label, {})
        if db_div:
            redundancies = [m["conversation_redundancy"] for m in db_div.values() if m]
            ws_counts = [m["unique_workspace_count"] for m in db_div.values() if m]
            by_db[label]["avg_conversation_redundancy"] = round(mean(redundancies), 4) if redundancies else None
            by_db[label]["avg_unique_workspace_count"] = round(mean(ws_counts), 1) if ws_counts else None

    recall_meta = data.get("recall_meta", {})

    # Build groups with per-query results
    output_groups = []
    for group in groups:
        group_summary = {}
        for label in db_labels:
            t1_scores = []
            t5_scores = []
            for query_text in group["queries"]:
                qr = results[label].get(query_text, [])
                if qr:
                    t1_scores.append(qr[0]["score"])
                    t5_avg = sum(r["score"] for r in qr[:5]) / min(5, len(qr))
                    t5_scores.append(t5_avg)
            group_summary[label] = {
                "avg_top1": round(sum(t1_scores) / len(t1_scores), 6) if t1_scores else 0,
                "avg_top5": round(sum(t5_scores) / len(t5_scores), 6) if t5_scores else 0,
            }

        output_queries = []
        for query_text in group["queries"]:
            query_results_by_db = {}
            for label in db_labels:
                qr = results[label].get(query_text, [])
                query_results_by_db[label] = [
                    {
                        "score": round(r["score"], 6),
                        "chunk_text": r["text"],
                        "chunk_type": r["chunk_type"],
                        "conversation_id": r["conversation_id"],
                        "token_count": r["token_count"],
                        "started_at": r.get("started_at"),
                    }
                    for r in qr
                ]
            query_entry = {
                "text": query_text,
                "results": query_results_by_db,
            }
            # Add recall metadata if present (hybrid mode)
            for label in db_labels:
                if label in recall_meta and query_text in recall_meta[label]:
                    query_entry.setdefault("recall", {})[label] = recall_meta[label][query_text]
            # Add diversity metrics
            for label in db_labels:
                if label in div_metrics and query_text in div_metrics[label]:
                    query_entry.setdefault("diversity", {})[label] = div_metrics[label][query_text]
            output_queries.append(query_entry)

        output_groups.append({
            "name": group["name"],
            "description": group["description"],
            "summary": group_summary,
            "queries": output_queries,
        })

    return {
        "meta": meta,
        "summary": {"by_db": by_db},
        "groups": output_groups,
    }


def print_results(data: dict) -> None:
    """Print results to stdout."""
    groups = data["groups"]
    results = data["results"]
    db_labels = list(results.keys())

    col_width = max(40, max(len(l) for l in db_labels) + 4)

    for group in groups:
        print(f"\n{'=' * 80}")
        print(f"  {group['name']}: {group['description']}")
        print(f"{'=' * 80}")

        group_scores = {label: {"top1": [], "top5": []} for label in db_labels}

        for query_text in group["queries"]:
            print(f"\n  Q: {query_text}")
            print(f"  {'-' * 76}")

            header = "  " + "".join(label.ljust(col_width) for label in db_labels)
            print(header)

            for rank in range(5):
                parts = []
                for label in db_labels:
                    query_results = results[label].get(query_text, [])
                    if rank < len(query_results):
                        r = query_results[rank]
                        score_str = f"{r['score']:.4f}"
                        snippet = r["text"][:35].replace("\n", " ")
                        parts.append(f"  {score_str} {snippet}".ljust(col_width))
                    else:
                        parts.append(" " * col_width)
                print("  " + "".join(parts))

            for label in db_labels:
                query_results = results[label].get(query_text, [])
                if query_results:
                    group_scores[label]["top1"].append(query_results[0]["score"])
                    top5_avg = sum(r["score"] for r in query_results[:5]) / min(5, len(query_results))
                    group_scores[label]["top5"].append(top5_avg)

        print(f"\n  Group Summary: {group['name']}")
        print(f"  {'DB':<{col_width - 2}} {'Avg Top-1':<12} {'Avg Top-5':<12}")
        for label in db_labels:
            t1 = group_scores[label]["top1"]
            t5 = group_scores[label]["top5"]
            avg_t1 = sum(t1) / len(t1) if t1 else 0
            avg_t5 = sum(t5) / len(t5) if t5 else 0
            print(f"  {label:<{col_width - 2}} {avg_t1:<12.4f} {avg_t5:<12.4f}")

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

    # Diversity metrics
    div_data = data.get("diversity_metrics", {})
    if div_data:
        print(f"\n  {'DIVERSITY'}")
        print(f"  {'-' * 76}")
        print(f"  {'DB':<{col_width - 2}} {'Conv Redund':<13} {'Uniq WS':<10}")

        for label in db_labels:
            db_div = div_data.get(label, {})
            if db_div:
                redundancies = [m["conversation_redundancy"] for m in db_div.values() if m]
                ws_counts = [m["unique_workspace_count"] for m in db_div.values() if m]
                avg_red = mean(redundancies) if redundancies else 0
                avg_ws = mean(ws_counts) if ws_counts else 0
                print(f"  {label:<{col_width - 2}} {avg_red:<13.4f} {avg_ws:<10.1f}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Run bench queries against embeddings DBs")
    parser.add_argument("embed_dbs", nargs="+", type=Path, help="Path(s) to embeddings DB files")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local/share/siftd/siftd.db",
        help="Path to main siftd.db (default: ~/.local/share/siftd/siftd.db)",
    )
    parser.add_argument(
        "--strategy",
        type=Path,
        default=None,
        help="Path to a strategy JSON file (provides label, goal, params)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Label for this run (used in output filename). Required if --strategy not given.",
    )
    parser.add_argument(
        "--goal",
        default=None,
        help="Free text describing what this run tests",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="key=value",
        help="Strategy parameter (repeatable), e.g. --param min_chars=100",
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="Use FTS5 recall before embeddings rerank",
    )
    parser.add_argument(
        "--recall",
        type=int,
        default=80,
        metavar="N",
        help="FTS5 conversation recall limit (default: 80)",
    )
    parser.add_argument(
        "--rerank",
        choices=["mmr", "relevance"],
        default="relevance",
        help="Reranking strategy: mmr (diversity) or relevance (default: relevance)",
    )
    parser.add_argument(
        "--lambda",
        type=float,
        default=0.7,
        dest="lambda_",
        metavar="FLOAT",
        help="MMR lambda: 1.0=pure relevance, 0.0=pure diversity (default: 0.7)",
    )
    args = parser.parse_args()

    # Load strategy if provided
    strategy = None
    if args.strategy:
        if not args.strategy.exists():
            print(f"Error: strategy file not found: {args.strategy}", file=sys.stderr)
            sys.exit(1)
        with open(args.strategy) as f:
            strategy = json.load(f)

    # Resolve label (explicit --label overrides strategy)
    label = args.label or (strategy["name"] if strategy else None)
    if not label:
        parser.error("--label is required when --strategy is not provided")

    # Resolve goal (explicit --goal overrides strategy)
    goal = args.goal or (strategy.get("goal") if strategy else None)

    # Validate paths
    for p in args.embed_dbs:
        if not p.exists():
            print(f"Error: embeddings DB not found: {p}", file=sys.stderr)
            sys.exit(1)
    if not args.db.exists():
        print(f"Error: main DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    # Resolve params: start from strategy, override with explicit --param
    params = dict(strategy["params"]) if strategy and "params" in strategy else {}
    for p in args.param:
        if "=" not in p:
            print(f"Error: --param must be key=value, got: {p}", file=sys.stderr)
            sys.exit(1)
        key, value = p.split("=", 1)
        params[key] = value

    # Initialize backend
    from siftd.embeddings.fastembed_backend import FastEmbedBackend
    print("Initializing embedding model...", file=sys.stderr)
    backend = FastEmbedBackend()
    tokenizer = get_tokenizer(backend)

    # Run
    data = run_bench(
        args.embed_dbs, args.db, backend, tokenizer,
        hybrid=args.hybrid, recall_limit=args.recall,
        rerank_mode=args.rerank, lambda_=args.lambda_,
    )

    # Build output path
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    run_id = f"{timestamp_str}_{label}"

    runs_dir = Path(__file__).parent / "runs"
    runs_dir.mkdir(exist_ok=True)
    output_path = runs_dir / f"{run_id}.json"

    # Build meta
    bench_dir = Path(__file__).parent
    query_data = load_queries(bench_dir)
    query_count = sum(len(g["queries"]) for g in query_data["groups"])

    total_chunks = {}
    chunk_token_stats = {}
    print("Computing chunk token stats...", file=sys.stderr)
    for db_path in args.embed_dbs:
        total_chunks[str(db_path)] = get_chunk_count(db_path)
        chunk_token_stats[str(db_path)] = get_chunk_token_stats(db_path, tokenizer)

    meta = {
        "id": run_id,
        "timestamp": now.isoformat(),
        "label": label,
        "goal": goal,
        "params": params,
        "embed_dbs": [str(p) for p in args.embed_dbs],
        "main_db": str(args.db),
        "model": {
            "name": backend.model,
            "max_seq_length": 512,
            "dimension": backend.dimension,
        },
        "query_count": query_count,
        "total_chunks": total_chunks,
        "chunk_token_stats": chunk_token_stats,
        "hybrid": args.hybrid,
        "recall_limit": args.recall if args.hybrid else None,
        "rerank": args.rerank,
        "lambda": args.lambda_ if args.rerank == "mmr" else None,
    }

    # Build and write output
    structured = build_output(data, meta)
    with open(output_path, "w") as f:
        json.dump(structured, f, indent=2)

    # Print results to stdout
    print_results(data)

    print(f"\nResults written to: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
