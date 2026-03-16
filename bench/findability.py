#!/usr/bin/env python3
"""Findability benchmark: recall@10 for tool-usage queries.

Generates ground-truth query/conversation pairs from tool_calls table,
then measures recall@10 against an embeddings DB.

Usage:
    python bench/findability.py --embed-db /path/to/embeddings.db
    python bench/findability.py --embed-db /path/to/embeddings.db --main-db /path/to/siftd.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.storage.embeddings import search_similar  # noqa: E402
from siftd.embeddings.fastembed_backend import FastEmbedBackend  # noqa: E402
from siftd.paths import data_dir  # noqa: E402


# Ground-truth query definitions
# Each entry: (query_text, sql, params)
# SQL returns rows with conversation_id column
GROUND_TRUTH_QUERIES = [
    (
        "conversations where files were read",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'file.read'""",
        (),
    ),
    (
        "conversations where shell commands were executed",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'shell.execute'""",
        (),
    ),
    (
        "conversations with tool errors or failures",
        """SELECT DISTINCT conversation_id FROM tool_calls WHERE status = 'error'""",
        (),
    ),
    (
        "conversations editing source files",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'file.edit'""",
        (),
    ),
    (
        "conversations reading pyproject.toml configuration",
        """SELECT DISTINCT conversation_id FROM tool_calls
           WHERE input LIKE '%pyproject.toml%'""",
        (),
    ),
    (
        "conversations running git commands",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id
           WHERE t.name = 'shell.execute' AND tc.input LIKE '%git%'""",
        (),
    ),
    (
        "conversations searching with grep",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'search.grep'""",
        (),
    ),
    (
        "conversations writing new files",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'file.write'""",
        (),
    ),
]


def get_indexed_conversations(embed_conn: sqlite3.Connection) -> set[str]:
    """Get set of conversation IDs present in the embeddings DB."""
    rows = embed_conn.execute("SELECT DISTINCT conversation_id FROM chunks").fetchall()
    return {r[0] for r in rows}


def compute_recall(
    main_conn: sqlite3.Connection,
    embed_conn: sqlite3.Connection,
    backend: FastEmbedBackend,
    *,
    k: int = 10,
    verbose: bool = False,
) -> dict:
    """Compute recall@k for all ground-truth queries.

    Returns dict with per-query and aggregate results.
    """
    indexed_convs = get_indexed_conversations(embed_conn)

    results = []
    for query_text, sql, params in GROUND_TRUTH_QUERIES:
        # Get ground-truth: relevant conversations that are also indexed
        relevant_all = {r[0] for r in main_conn.execute(sql, params).fetchall()}
        relevant = relevant_all & indexed_convs  # only those we can possibly find

        if not relevant:
            if verbose:
                print(f"  SKIP '{query_text}' — no indexed relevant convs")
            continue

        # Embed query and search
        query_embedding = backend.embed_one(query_text)
        search_results = search_similar(embed_conn, query_embedding, limit=k)
        found_convs = {r["conversation_id"] for r in search_results}

        hits = len(relevant & found_convs)
        recall = hits / len(relevant)  # fraction of relevant convs found in top-k

        # Alternative: was ANY relevant conversation in top-k? (binary hit@k)
        hit_at_k = 1 if (relevant & found_convs) else 0

        results.append({
            "query": query_text,
            "relevant_total": len(relevant_all),
            "relevant_indexed": len(relevant),
            "hits_in_top_k": hits,
            "recall_at_k": round(recall, 4),
            "hit_at_k": hit_at_k,
        })

        if verbose:
            status = "✓" if hit_at_k else "✗"
            print(f"  {status} [{hits}/{len(relevant)} relevant] recall={recall:.3f} | {query_text}")

    if not results:
        return {"recall_at_k": 0.0, "hit_at_k": 0.0, "queries": [], "k": k}

    avg_recall = sum(r["recall_at_k"] for r in results) / len(results)
    avg_hit_at_k = sum(r["hit_at_k"] for r in results) / len(results)

    return {
        "k": k,
        "recall_at_k": round(avg_recall, 4),
        "hit_at_k": round(avg_hit_at_k, 4),
        "query_count": len(results),
        "queries": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Findability benchmark: recall@10 for tool-usage queries")
    parser.add_argument("--embed-db", type=Path, required=True, help="Embeddings DB to evaluate")
    parser.add_argument("--main-db", type=Path, default=None, help="Main siftd.db path")
    parser.add_argument("-k", type=int, default=10, help="Recall@k (default: 10)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--metric", action="store_true", help="Output METRIC lines for autoresearch.sh")
    args = parser.parse_args()

    if not args.embed_db.exists():
        print(f"Error: embeddings DB not found: {args.embed_db}", file=sys.stderr)
        sys.exit(1)

    main_db_path = args.main_db or (data_dir() / "siftd.db")
    if not main_db_path.exists():
        print(f"Error: main DB not found: {main_db_path}", file=sys.stderr)
        sys.exit(1)

    main_conn = sqlite3.connect(main_db_path)
    main_conn.row_factory = sqlite3.Row
    embed_conn = sqlite3.connect(args.embed_db)
    embed_conn.row_factory = sqlite3.Row

    print("Initializing embedding model...", file=sys.stderr)
    backend = FastEmbedBackend()

    print(f"\nFindability benchmark (recall@{args.k}):", file=sys.stderr)
    result = compute_recall(main_conn, embed_conn, backend, k=args.k, verbose=True)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  Queries evaluated: {result['query_count']}", file=sys.stderr)
    print(f"  Avg recall@{args.k}:   {result['recall_at_k']:.4f}", file=sys.stderr)
    print(f"  Avg hit@{args.k}:      {result['hit_at_k']:.4f}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    if args.metric:
        print(f"METRIC recall_at_10={result['recall_at_k']}")
        print(f"METRIC hit_at_10={result['hit_at_k']}")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
