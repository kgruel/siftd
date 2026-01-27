#!/usr/bin/env python3
"""Benchmark runner — compare semantic search quality across embeddings DBs."""

import argparse
import json
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

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


def search_similar(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    limit: int = 10,
    conversation_ids: set[str] | None = None,
    role_source_ids: set[str] | None = None,
) -> list[dict]:
    if conversation_ids is not None:
        placeholders = ",".join("?" for _ in conversation_ids)
        sql = f"SELECT id, conversation_id, chunk_type, text, embedding, source_ids FROM chunks WHERE conversation_id IN ({placeholders})"
        cur = conn.execute(sql, list(conversation_ids))
    else:
        cur = conn.execute("SELECT id, conversation_id, chunk_type, text, embedding, source_ids FROM chunks")
    results = []
    for row in cur:
        source_ids_val = json.loads(row["source_ids"]) if row["source_ids"] else []

        # Role filter: skip chunks that don't overlap with allowed source IDs
        if role_source_ids is not None:
            if not source_ids_val or not set(source_ids_val) & role_source_ids:
                continue

        stored = decode_embedding(row["embedding"])
        score = cosine_similarity(query_embedding, stored)
        results.append({
            "chunk_id": row["id"],
            "conversation_id": row["conversation_id"],
            "chunk_type": row["chunk_type"],
            "text": row["text"],
            "score": score,
            "source_ids": source_ids_val,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def resolve_role_source_ids(main_db: sqlite3.Connection, role: str) -> set[str]:
    """Resolve source IDs for a given role from main DB.

    'user': prompt IDs (prompts are user messages).
    'assistant': prompt IDs that have responses.
    """
    if role == "user":
        rows = main_db.execute("SELECT id FROM prompts").fetchall()
    else:
        rows = main_db.execute("SELECT DISTINCT prompt_id AS id FROM responses").fetchall()
    return {row["id"] for row in rows}


def compute_first_mention(results: list[dict], main_db: sqlite3.Connection, threshold: float = 0.65) -> dict | None:
    """Find the chronologically earliest result above the relevance threshold."""
    above = [r for r in results if r["score"] >= threshold]
    if not above:
        return None

    conv_ids = list({r["conversation_id"] for r in above})
    placeholders = ",".join("?" * len(conv_ids))
    rows = main_db.execute(
        f"SELECT id, started_at FROM conversations WHERE id IN ({placeholders})",
        conv_ids,
    ).fetchall()
    conv_times = {row["id"]: row["started_at"] or "" for row in rows}

    above.sort(key=lambda r: (conv_times.get(r["conversation_id"], ""), r["chunk_id"]))
    first = above[0]
    return {
        "conversation_id": first["conversation_id"],
        "score": first["score"],
        "started_at": conv_times.get(first["conversation_id"]),
        "snippet": first["text"][:200],
    }


def compute_conversation_scores(results: list[dict]) -> list[dict]:
    """Aggregate chunk scores per conversation."""
    by_conv: dict[str, list[dict]] = {}
    for r in results:
        by_conv.setdefault(r["conversation_id"], []).append(r)

    conv_scores = []
    for conv_id, chunks in by_conv.items():
        scores = [c["score"] for c in chunks]
        max_score = max(scores)
        mean_score = sum(scores) / len(scores)
        conv_scores.append({
            "conversation_id": conv_id,
            "max_score": max_score,
            "mean_score": mean_score,
            "chunk_count": len(chunks),
        })

    conv_scores.sort(key=lambda x: x["max_score"], reverse=True)
    return conv_scores


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


def run_benchmark(
    embed_db_paths: list[Path],
    main_db_path: Path,
    backend,
    tokenizer,
    *,
    hybrid: bool = False,
    recall_limit: int = 80,
    role: str | None = None,
    rerank_mode: str = "relevance",
    lambda_: float = 0.7,
) -> dict:
    """Run all queries against all embed DBs, return full results."""
    bench_dir = Path(__file__).parent
    query_data = load_queries(bench_dir)

    use_mmr = rerank_mode == "mmr"
    if use_mmr:
        from strata.search import mmr_rerank

    main_db = sqlite3.connect(main_db_path)
    main_db.row_factory = sqlite3.Row

    # Resolve role filter once
    role_source_ids = None
    if role:
        role_source_ids = resolve_role_source_ids(main_db, role)
        if not role_source_ids:
            print(f"Warning: no {role} source IDs found", file=sys.stderr)

    all_results = {}
    recall_meta = {}  # per-query FTS5 recall metadata
    first_mentions = {}  # per-query first-mention data
    conversation_agg = {}  # per-query conversation-level aggregation
    diversity_metrics = {}  # per-query diversity metrics

    for db_path in embed_db_paths:
        db_label = str(db_path)
        print(f"Benchmarking: {db_label}", file=sys.stderr)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        db_results = {}
        db_recall_meta = {}
        db_first_mentions = {}
        db_conversation_agg = {}
        db_diversity = {}
        for group in query_data["groups"]:
            for query_text in group["queries"]:
                conversation_ids = None
                if hybrid:
                    from strata.storage.sqlite import fts5_recall_conversations
                    fts5_ids, fts5_mode = fts5_recall_conversations(main_db, query_text, limit=recall_limit)
                    db_recall_meta[query_text] = {
                        "fts5_conversations": len(fts5_ids),
                        "fts5_mode": fts5_mode,
                    }
                    if fts5_ids:
                        conversation_ids = fts5_ids

                query_embedding = backend.embed_one(query_text)
                # Wider search for first-mention and conversation aggregation
                results = search_similar(
                    conn, query_embedding, limit=100,
                    conversation_ids=conversation_ids,
                    role_source_ids=role_source_ids,
                )

                # Attach decoded embeddings for MMR and diversity pairwise metrics
                if use_mmr:
                    for r in results:
                        r["embedding"] = decode_embedding(
                            conn.execute(
                                "SELECT embedding FROM chunks WHERE id = ?",
                                (r["chunk_id"],),
                            ).fetchone()["embedding"]
                        )

                results = enrich_results(results, main_db)
                for r in results:
                    r["token_count"] = count_tokens(tokenizer, r["text"])

                # Compute first-mention
                fm = compute_first_mention(results, main_db)
                if fm:
                    db_first_mentions[query_text] = fm

                # Compute conversation-level aggregation
                conv_agg = compute_conversation_scores(results)
                db_conversation_agg[query_text] = conv_agg[:10]  # top 10 conversations

                # Apply MMR reranking if requested
                if use_mmr:
                    reranked = mmr_rerank(
                        results,
                        query_embedding,
                        lambda_=lambda_,
                        limit=10,
                    )
                    # Compute diversity on reranked results
                    db_diversity[query_text] = compute_diversity_metrics(reranked)
                    db_results[query_text] = reranked
                else:
                    # Trim to top 10 for standard results
                    top10 = results[:10]
                    db_diversity[query_text] = compute_diversity_metrics(top10)
                    db_results[query_text] = top10

        conn.close()
        all_results[db_label] = db_results
        recall_meta[db_label] = db_recall_meta
        first_mentions[db_label] = db_first_mentions
        conversation_agg[db_label] = db_conversation_agg
        diversity_metrics[db_label] = db_diversity

    main_db.close()
    return {
        "groups": query_data["groups"],
        "results": all_results,
        "recall_meta": recall_meta,
        "first_mentions": first_mentions,
        "conversation_agg": conversation_agg,
        "diversity_metrics": diversity_metrics,
    }


def compute_presentation_metrics(query_results: list[dict]) -> dict:
    """Compute metrics relevant to presentation/narrative features.

    From a single query's top-k results, measures:
    - unique_conversations: how many distinct conversations in top-k
    - temporal_span_days: time range from earliest to latest result
    - chrono_top1_score: score of the chronologically earliest result
    - chrono_degradation: top1 score minus chrono_top1 score
    - clusters_above_mean: conversations with max score > mean score
    """
    if not query_results:
        return {}

    # Conversation diversity
    conv_ids = [r["conversation_id"] for r in query_results]
    unique_convs = len(set(conv_ids))

    # Temporal spread
    timestamps = [r["started_at"] for r in query_results if r.get("started_at")]
    temporal_span_days = 0.0
    if len(timestamps) >= 2:
        sorted_ts = sorted(timestamps)
        try:
            earliest = datetime.fromisoformat(sorted_ts[0].replace("Z", "+00:00"))
            latest = datetime.fromisoformat(sorted_ts[-1].replace("Z", "+00:00"))
            temporal_span_days = (latest - earliest).total_seconds() / 86400
        except (ValueError, TypeError):
            temporal_span_days = 0.0

    # Chrono degradation: what score do you get if you pick the earliest result?
    top1_score = query_results[0]["score"]
    chrono_top1_score = top1_score
    if timestamps:
        chrono_sorted = sorted(
            [r for r in query_results if r.get("started_at")],
            key=lambda r: r["started_at"],
        )
        if chrono_sorted:
            chrono_top1_score = chrono_sorted[0]["score"]
    chrono_degradation = top1_score - chrono_top1_score

    # Cluster density: conversations with max score above mean
    all_scores = [r["score"] for r in query_results]
    mean_score = sum(all_scores) / len(all_scores)
    conv_max_scores = {}
    for r in query_results:
        cid = r["conversation_id"]
        if cid not in conv_max_scores or r["score"] > conv_max_scores[cid]:
            conv_max_scores[cid] = r["score"]
    clusters_above_mean = sum(1 for s in conv_max_scores.values() if s > mean_score)

    return {
        "unique_conversations": unique_convs,
        "temporal_span_days": round(temporal_span_days, 1),
        "chrono_top1_score": round(chrono_top1_score, 6),
        "chrono_degradation": round(chrono_degradation, 6),
        "clusters_above_mean": clusters_above_mean,
    }


def compute_diversity_metrics(query_results: list[dict]) -> dict:
    """Compute diversity metrics for a single query's top-k results.

    Measures:
    - conversation_redundancy: fraction of top-10 from same conversation as rank-1
    - unique_workspace_count: distinct workspaces in top-10
    - pairwise_similarity_mean: mean cosine sim between all top-10 pairs (requires embeddings)
    """
    if not query_results:
        return {}

    top10 = query_results[:10]

    # Conversation redundancy: fraction from same conv as rank-1
    rank1_conv = top10[0]["conversation_id"]
    same_conv_count = sum(1 for r in top10 if r["conversation_id"] == rank1_conv)
    conversation_redundancy = same_conv_count / len(top10)

    # Unique workspace count
    workspaces = {r.get("workspace_path") or "(none)" for r in top10}
    unique_workspace_count = len(workspaces)

    # Pairwise similarity (only if embeddings are present)
    pairwise_similarity_mean = None
    if top10 and "embedding" in top10[0]:
        sims = []
        for i in range(len(top10)):
            for j in range(i + 1, len(top10)):
                sims.append(cosine_similarity(top10[i]["embedding"], top10[j]["embedding"]))
        if sims:
            pairwise_similarity_mean = round(sum(sims) / len(sims), 6)

    result = {
        "conversation_redundancy": round(conversation_redundancy, 4),
        "unique_workspace_count": unique_workspace_count,
    }
    if pairwise_similarity_mean is not None:
        result["pairwise_similarity_mean"] = pairwise_similarity_mean
    return result


def compute_cross_query_overlap(results_a: list[dict], results_b: list[dict]) -> float:
    """Jaccard similarity of conversation IDs between two result sets (top-10)."""
    convs_a = {r["conversation_id"] for r in results_a[:10]}
    convs_b = {r["conversation_id"] for r in results_b[:10]}
    if not convs_a and not convs_b:
        return 0.0
    intersection = convs_a & convs_b
    union = convs_a | convs_b
    return len(intersection) / len(union) if union else 0.0


def build_structured_output(data: dict, meta: dict) -> dict:
    """Build the structured JSON output from raw benchmark data."""
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

        # Presentation metrics aggregated across all queries
        pres_metrics = []
        for query_text, query_results in results[label].items():
            if query_results:
                pres_metrics.append(compute_presentation_metrics(query_results))
        avg_unique_convs = mean([m["unique_conversations"] for m in pres_metrics]) if pres_metrics else 0
        avg_temporal_span = mean([m["temporal_span_days"] for m in pres_metrics]) if pres_metrics else 0
        avg_chrono_deg = mean([m["chrono_degradation"] for m in pres_metrics]) if pres_metrics else 0
        avg_clusters = mean([m["clusters_above_mean"] for m in pres_metrics]) if pres_metrics else 0

        by_db[label] = {
            "avg_score": round(avg, 6),
            "variance": round(variance, 8),
            "spread": round(spread, 6),
            "avg_top1": round(avg_top1, 6),
            "avg_top5": round(avg_top5, 6),
            "avg_unique_conversations": round(avg_unique_convs, 1),
            "avg_temporal_span_days": round(avg_temporal_span, 1),
            "avg_chrono_degradation": round(avg_chrono_deg, 6),
            "avg_clusters_above_mean": round(avg_clusters, 1),
        }

    # Aggregate diversity metrics per DB
    div_metrics = data.get("diversity_metrics", {})
    for label in db_labels:
        db_div = div_metrics.get(label, {})
        if db_div:
            redundancies = [m["conversation_redundancy"] for m in db_div.values() if m]
            ws_counts = [m["unique_workspace_count"] for m in db_div.values() if m]
            pairwise = [m["pairwise_similarity_mean"] for m in db_div.values() if m and "pairwise_similarity_mean" in m]
            by_db[label]["avg_conversation_redundancy"] = round(mean(redundancies), 4) if redundancies else None
            by_db[label]["avg_unique_workspace_count"] = round(mean(ws_counts), 1) if ws_counts else None
            by_db[label]["avg_pairwise_similarity"] = round(mean(pairwise), 6) if pairwise else None

    # Compute cross-query overlap for broad-then-narrow group
    cross_query_overlaps = {}
    for label in db_labels:
        label_results = results.get(label, {})
        for group in groups:
            if group["name"] == "broad-then-narrow":
                queries = group["queries"]
                # Compare consecutive pairs (broad, narrow)
                overlaps = []
                for i in range(0, len(queries) - 1, 2):
                    a = label_results.get(queries[i], [])
                    b = label_results.get(queries[i + 1], [])
                    if a and b:
                        overlaps.append(compute_cross_query_overlap(a, b))
                if overlaps:
                    cross_query_overlaps.setdefault(label, round(mean(overlaps), 4))
    for label in db_labels:
        if label in cross_query_overlaps:
            by_db[label]["avg_cross_query_overlap"] = cross_query_overlaps[label]

    recall_meta = data.get("recall_meta", {})
    first_mentions = data.get("first_mentions", {})
    conversation_agg = data.get("conversation_agg", {})

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
                    for i, r in enumerate(qr)
                ]
            # Compute presentation metrics per DB
            presentation_by_db = {}
            for label in db_labels:
                qr = results[label].get(query_text, [])
                if qr:
                    presentation_by_db[label] = compute_presentation_metrics(qr)
            query_entry = {
                "text": query_text,
                "results": query_results_by_db,
                "presentation": presentation_by_db,
            }
            # Add recall metadata if present (hybrid mode)
            for label in db_labels:
                if label in recall_meta and query_text in recall_meta[label]:
                    query_entry.setdefault("recall", {})[label] = recall_meta[label][query_text]
            # Add first-mention data
            for label in db_labels:
                if label in first_mentions and query_text in first_mentions[label]:
                    fm = first_mentions[label][query_text]
                    query_entry.setdefault("first_mention", {})[label] = {
                        "conversation_id": fm["conversation_id"],
                        "score": round(fm["score"], 6),
                        "started_at": fm["started_at"],
                        "snippet": fm["snippet"],
                    }
            # Add conversation-level aggregation
            for label in db_labels:
                if label in conversation_agg and query_text in conversation_agg[label]:
                    convs = conversation_agg[label][query_text]
                    query_entry.setdefault("conversations", {})[label] = [
                        {
                            "conversation_id": c["conversation_id"],
                            "max_score": round(c["max_score"], 6),
                            "mean_score": round(c["mean_score"], 6),
                            "chunk_count": c["chunk_count"],
                        }
                        for c in convs
                    ]
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


def print_comparison(data: dict) -> None:
    """Print comparison report to stdout."""
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

    # Presentation metrics
    print(f"\n  {'PRESENTATION METRICS'}")
    print(f"  {'-' * 76}")
    print(f"  {'DB':<{col_width - 2}} {'Uniq Convs':<12} {'Span (days)':<13} {'Chrono Deg':<12} {'Clusters':<10}")

    for label in db_labels:
        pres_metrics = []
        for query_text, query_results in results[label].items():
            if query_results:
                pres_metrics.append(compute_presentation_metrics(query_results))
        if pres_metrics:
            avg_uc = mean([m["unique_conversations"] for m in pres_metrics])
            avg_ts = mean([m["temporal_span_days"] for m in pres_metrics])
            avg_cd = mean([m["chrono_degradation"] for m in pres_metrics])
            avg_cl = mean([m["clusters_above_mean"] for m in pres_metrics])
            print(f"  {label:<{col_width - 2}} {avg_uc:<12.1f} {avg_ts:<13.1f} {avg_cd:<12.4f} {avg_cl:<10.1f}")

    # Diversity metrics
    div_data = data.get("diversity_metrics", {})
    if div_data:
        print(f"\n  {'DIVERSITY METRICS'}")
        print(f"  {'-' * 76}")
        print(f"  {'DB':<{col_width - 2}} {'Conv Redund':<13} {'Uniq WS':<10} {'Pairwise Sim':<14}")

        for label in db_labels:
            db_div = div_data.get(label, {})
            if db_div:
                redundancies = [m["conversation_redundancy"] for m in db_div.values() if m]
                ws_counts = [m["unique_workspace_count"] for m in db_div.values() if m]
                pairwise = [m["pairwise_similarity_mean"] for m in db_div.values() if m and "pairwise_similarity_mean" in m]
                avg_red = mean(redundancies) if redundancies else 0
                avg_ws = mean(ws_counts) if ws_counts else 0
                avg_pw = mean(pairwise) if pairwise else 0
                pw_str = f"{avg_pw:<14.4f}" if pairwise else "N/A"
                print(f"  {label:<{col_width - 2}} {avg_red:<13.4f} {avg_ws:<10.1f} {pw_str}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Benchmark semantic search across embeddings DBs")
    parser.add_argument("embed_dbs", nargs="+", type=Path, help="Path(s) to embeddings DB files")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local/share/strata/strata.db",
        help="Path to main strata.db (default: ~/.local/share/strata/strata.db)",
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
        "--role",
        choices=["user", "assistant"],
        default=None,
        help="Filter chunks by source role (user prompts or assistant responses)",
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
    from strata.embeddings.fastembed_backend import FastEmbedBackend
    print("Initializing embedding model...", file=sys.stderr)
    backend = FastEmbedBackend()
    tokenizer = get_tokenizer(backend)

    # Run benchmark
    data = run_benchmark(
        args.embed_dbs, args.db, backend, tokenizer,
        hybrid=args.hybrid, recall_limit=args.recall, role=args.role,
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
        "role_filter": args.role,
        "rerank": args.rerank,
        "lambda": args.lambda_ if args.rerank == "mmr" else None,
    }

    # Build and write structured output
    structured = build_structured_output(data, meta)
    with open(output_path, "w") as f:
        json.dump(structured, f, indent=2)

    # Print comparison report to stdout
    print_comparison(data)

    # Print output path
    print(f"\nResults written to: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
