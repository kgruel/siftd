#!/usr/bin/env python3
"""View bench run results — stdout summary or HTML report."""

import argparse
import json
import sys
import webbrowser
from html import escape
from pathlib import Path


def load_run(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def score_color_class(score: float) -> str:
    if score >= 0.7:
        return "high"
    elif score >= 0.5:
        return "mid"
    return "low"


def print_stdout(data: dict) -> None:
    meta = data["meta"]
    summary = data["summary"]["by_db"]
    groups = data["groups"]

    # Header
    print(f"Run: {meta['id']}")
    print(f"  Label: {meta['label']}")
    if meta.get("goal"):
        print(f"  Goal: {meta['goal']}")
    model = meta.get("model", {})
    if model:
        print(f"  Model: {model.get('name', '?')} (dim={model.get('dimension')}, max_seq={model.get('max_seq_length')})")
    print(f"  Queries: {meta['query_count']}")
    if meta.get("rerank"):
        rerank_str = meta["rerank"]
        if meta.get("lambda") is not None:
            rerank_str += f" (lambda={meta['lambda']})"
        print(f"  Rerank: {rerank_str}")
    for db_label, count in meta.get("total_chunks", {}).items():
        print(f"  Chunks [{db_label}]: {count}")
        stats = meta.get("chunk_token_stats", {}).get(db_label)
        if stats:
            print(f"    Token stats: min={stats['min']} max={stats['max']} mean={stats['mean']} median={stats['median']} p95={stats['p95']}")
    print()

    # Aggregate metrics
    print("Aggregate Metrics:")
    print(f"  {'DB':<60} {'Avg':<8} {'Var':<10} {'Spread':<8} {'Top1':<8} {'Top5':<8}")
    for label, s in summary.items():
        print(f"  {label:<60} {s['avg_score']:<8.4f} {s['variance']:<10.6f} {s['spread']:<8.4f} {s['avg_top1']:<8.4f} {s['avg_top5']:<8.4f}")

    # Diversity metrics in aggregate
    has_diversity = any(s.get("avg_conversation_redundancy") is not None for s in summary.values())
    if has_diversity:
        print()
        print("Diversity Metrics:")
        print(f"  {'DB':<60} {'Conv Red':<10} {'Uniq WS':<10}")
        for label, s in summary.items():
            red = s.get("avg_conversation_redundancy")
            ws = s.get("avg_unique_workspace_count")
            red_str = f"{red:<10.4f}" if red is not None else "N/A       "
            ws_str = f"{ws:<10.1f}" if ws is not None else "N/A       "
            print(f"  {label:<60} {red_str} {ws_str}")
    print()

    # Per-group breakdown
    for group in groups:
        print(f"Group: {group['name']} — {group['description']}")
        group_summary = group.get("summary", {})
        for label, gs in group_summary.items():
            print(f"  [{label}] Top1={gs['avg_top1']:.4f}  Top5={gs['avg_top5']:.4f}")

        for q in group["queries"]:
            print(f"\n  Q: {q['text']}")
            for label, results in q["results"].items():
                top3 = results[:3]
                scores_str = "  ".join(f"{r['score']:.4f}" for r in top3)
                print(f"    [{label}] {scores_str}")
        print()


def generate_html(data: dict) -> str:
    meta = data["meta"]
    summary = data["summary"]["by_db"]
    groups = data["groups"]
    model = meta.get("model", {})

    db_labels = list(summary.keys())

    # Build HTML
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bench Run: {escape(meta['id'])}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8f9fa; color: #1a1a2e; padding: 2rem; line-height: 1.5; }}
h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
h2 {{ font-size: 1.2rem; margin: 1.5rem 0 0.5rem; border-bottom: 2px solid #dee2e6; padding-bottom: 0.3rem; }}
h3 {{ font-size: 1rem; margin: 1rem 0 0.4rem; color: #495057; }}
.meta {{ background: #fff; padding: 1rem; border-radius: 6px; margin-bottom: 1.5rem; border: 1px solid #dee2e6; }}
.meta dt {{ font-weight: 600; display: inline; }}
.meta dd {{ display: inline; margin-right: 1.5rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0; background: #fff; border-radius: 6px; overflow: hidden; }}
th, td {{ padding: 0.4rem 0.7rem; text-align: left; border-bottom: 1px solid #eee; font-size: 0.85rem; }}
th {{ background: #e9ecef; font-weight: 600; }}
.query-section {{ background: #fff; padding: 1rem; border-radius: 6px; margin: 1rem 0; border: 1px solid #dee2e6; }}
.query-text {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 0.7rem; padding: 0.4rem 0.6rem; background: #e8f4fd; border-radius: 4px; }}
.result-card {{ border: 1px solid #eee; border-radius: 4px; padding: 0.6rem; margin: 0.4rem 0; display: flex; gap: 0.8rem; align-items: flex-start; }}
.score-bar-wrap {{ width: 60px; flex-shrink: 0; }}
.score-val {{ font-weight: 700; font-size: 0.85rem; text-align: center; }}
.score-bar {{ height: 6px; border-radius: 3px; margin-top: 2px; }}
.score-bar.high {{ background: #2ecc71; }}
.score-bar.mid {{ background: #f39c12; }}
.score-bar.low {{ background: #e74c3c; }}
.chunk-content {{ flex: 1; min-width: 0; }}
.chunk-text {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.8rem; white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; background: #f8f9fa; padding: 0.4rem; border-radius: 3px; }}
.badges {{ display: flex; gap: 0.4rem; margin-top: 0.3rem; flex-wrap: wrap; }}
.badge {{ font-size: 0.7rem; padding: 0.1rem 0.4rem; border-radius: 3px; background: #e9ecef; color: #495057; }}
.badge.type-prompt {{ background: #d4edda; color: #155724; }}
.badge.type-response {{ background: #cce5ff; color: #004085; }}
.group-section {{ margin: 1.5rem 0; }}
</style>
</head>
<body>
""")

    # Header
    parts.append(f'<h1>Bench Run: {escape(meta["label"])}</h1>')
    parts.append('<div class="meta"><dl>')
    parts.append(f'<dt>ID:</dt><dd>{escape(meta["id"])}</dd>')
    parts.append(f'<dt>Time:</dt><dd>{escape(meta["timestamp"])}</dd>')
    if meta.get("goal"):
        parts.append(f'<dt>Goal:</dt><dd>{escape(meta["goal"])}</dd>')
    if model:
        parts.append(f'<dt>Model:</dt><dd>{escape(model.get("name", "?"))} (dim={model.get("dimension")}, max_seq={model.get("max_seq_length")})</dd>')
    parts.append(f'<dt>Queries:</dt><dd>{meta["query_count"]}</dd>')
    if meta.get("rerank"):
        rerank_str = meta["rerank"]
        if meta.get("lambda") is not None:
            rerank_str += f" (lambda={meta['lambda']})"
        parts.append(f'<dt>Rerank:</dt><dd>{escape(rerank_str)}</dd>')
    if meta.get("params"):
        params_str = ", ".join(f"{k}={v}" for k, v in meta["params"].items())
        parts.append(f'<dt>Params:</dt><dd>{escape(params_str)}</dd>')
    parts.append('</dl>')

    # Chunk stats in meta
    for db_label, count in meta.get("total_chunks", {}).items():
        stats = meta.get("chunk_token_stats", {}).get(db_label)
        stats_str = ""
        if stats:
            stats_str = f" — tokens: min={stats['min']} max={stats['max']} mean={stats['mean']} med={stats['median']} p95={stats['p95']}"
        parts.append(f'<p style="font-size:0.8rem;color:#666;margin-top:0.3rem;">{escape(db_label)}: {count} chunks{stats_str}</p>')
    parts.append('</div>')

    # Summary table
    parts.append('<h2>Aggregate Summary</h2>')
    parts.append('<table><tr><th>DB</th><th>Avg Score</th><th>Variance</th><th>Spread</th><th>Avg Top-1</th><th>Avg Top-5</th></tr>')
    for label, s in summary.items():
        parts.append(f'<tr><td>{escape(label)}</td><td>{s["avg_score"]:.4f}</td><td>{s["variance"]:.6f}</td><td>{s["spread"]:.4f}</td><td>{s["avg_top1"]:.4f}</td><td>{s["avg_top5"]:.4f}</td></tr>')
    parts.append('</table>')

    # Diversity summary table
    has_diversity = any(s.get("avg_conversation_redundancy") is not None for s in summary.values())
    if has_diversity:
        parts.append('<h2>Diversity Summary</h2>')
        parts.append('<table><tr><th>DB</th><th>Conv Redundancy</th><th>Unique Workspaces</th></tr>')
        for label, s in summary.items():
            red = s.get("avg_conversation_redundancy")
            ws = s.get("avg_unique_workspace_count")
            red_str = f"{red:.4f}" if red is not None else "N/A"
            ws_str = f"{ws:.1f}" if ws is not None else "N/A"
            parts.append(f'<tr><td>{escape(label)}</td><td>{red_str}</td><td>{ws_str}</td></tr>')
        parts.append('</table>')

    # Group breakdown
    parts.append('<h2>Group Breakdown</h2>')
    parts.append('<table><tr><th>Group</th><th>DB</th><th>Avg Top-1</th><th>Avg Top-5</th></tr>')
    for group in groups:
        for label, gs in group.get("summary", {}).items():
            parts.append(f'<tr><td>{escape(group["name"])}</td><td>{escape(label)}</td><td>{gs["avg_top1"]:.4f}</td><td>{gs["avg_top5"]:.4f}</td></tr>')
    parts.append('</table>')

    # Per-query sections
    parts.append('<h2>Query Results</h2>')
    for group in groups:
        parts.append(f'<div class="group-section"><h3>{escape(group["name"])}: {escape(group["description"])}</h3>')
        for q in group["queries"]:
            parts.append('<div class="query-section">')
            parts.append(f'<div class="query-text">{escape(q["text"])}</div>')

            for label, results in q["results"].items():
                if len(db_labels) > 1:
                    parts.append(f'<p style="font-size:0.75rem;color:#666;margin:0.3rem 0;">{escape(label)}</p>')
                for r in results[:5]:
                    score = r["score"]
                    color = score_color_class(score)
                    bar_width = int(score * 100)
                    chunk_text = r.get("chunk_text", "")
                    chunk_type = r.get("chunk_type", "")
                    token_count = r.get("token_count", 0)
                    conv_id = r.get("conversation_id", "")

                    type_class = f"type-{chunk_type}" if chunk_type in ("prompt", "response") else ""

                    parts.append(f'<div class="result-card">')
                    parts.append(f'<div class="score-bar-wrap"><div class="score-val">{score:.4f}</div><div class="score-bar {color}" style="width:{bar_width}%"></div></div>')
                    parts.append(f'<div class="chunk-content"><div class="chunk-text">{escape(chunk_text)}</div>')
                    parts.append(f'<div class="badges"><span class="badge {type_class}">{escape(chunk_type)}</span><span class="badge">{token_count} tok</span><span class="badge">{escape(conv_id[:12])}</span></div>')
                    parts.append('</div></div>')

            parts.append('</div>')
        parts.append('</div>')

    parts.append('</body></html>')
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="View bench run results")
    parser.add_argument("run_file", type=Path, help="Path to run JSON file")
    parser.add_argument("--html", action="store_true", help="Generate HTML and open in browser")
    args = parser.parse_args()

    if not args.run_file.exists():
        print(f"Error: file not found: {args.run_file}", file=sys.stderr)
        sys.exit(1)

    data = load_run(args.run_file)

    if args.html:
        html = generate_html(data)
        run_id = data["meta"]["id"]
        out_path = args.run_file.parent / f"{run_id}.html"
        out_path.write_text(html)
        print(f"Written to: {out_path}", file=sys.stderr)
        webbrowser.open(f"file://{out_path.resolve()}")
    else:
        print_stdout(data)


if __name__ == "__main__":
    main()
