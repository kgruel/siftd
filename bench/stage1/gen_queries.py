#!/usr/bin/env python3
"""Stage-1 query generation: the deterministic half of paraphrase/mixed ground truth.

The paraphrase and mixed classes are agent-generated (a model reads a source chunk
and writes a natural query). This module is the deterministic scaffolding around
that step:

- ``sample`` draws source chunks from the snapshot for the agent to paraphrase.
- ``filter`` applies the rejection rules that keep the two classes honest — a
  paraphrase that still shares a rare token with its source lets FTS take credit
  for what should be a semantic-recall win.

Both halves must tokenize identically to the corpus FTS index. content_fts uses
``porter unicode61 remove_diacritics 1``; we replicate it in an in-memory temp
FTS5 table and read a text's stemmed terms back through fts5vocab, so
``searching`` and ``searches`` both collapse to the same ``search`` stem the main
index stored. DF per term is then a dict lookup against the main index's
fts5vocab (``temp.v``) — the exact machinery ground_truth.py mines identifiers
with.

DF bands (derived from ground_truth.py + the corpus, documented so the thresholds
are auditable rather than magic):

- **low-DF: doc <= 8.** This is ground_truth.mine_identifier_vocab's exact
  identifier ceiling (``doc BETWEEN 2 AND 8``). doc is event-frequency over the
  245k content_fts rows; doc<=8 is the rare tail FTS nails by rarity (77.8% of the
  67.8k-term vocab). A query sharing a doc<=8 token with its source chunk is
  trivially FTS-retrievable, so it is rejected from BOTH classes.
- **mid-DF: 9 <= doc <= 50.** Lower bound sits one above the low-DF ceiling; the
  upper bound is the corpus p90 of vocab doc-frequency (doc<=50 == 90.0% of terms)
  — which also coincides with the topical miner's 3..50 member ceiling. These are
  the tokens the "half-rememberer" persona plausibly keeps: distinctive enough to
  matter, common enough that FTS alone will not resolve them.
- doc > 50 is high-DF (top decile, stopword-ish): allowed to co-occur but it earns
  no credit toward the mixed "keep 1-2" requirement.

Rules (from the bench plan's ground-truth table):

- **paraphrase:** reject if the query shares ANY low-DF (doc<=8) token with its
  source chunk.
- **mixed:** reject if the query shares any low-DF token; otherwise require the
  query to keep EXACTLY 1-2 mid-DF (9<=doc<=50) tokens that appear in the source
  chunk. Zero mid-DF overlap or three-plus both reject.

Usage:
    UV_NO_SYNC=1 uv run --no-sync python bench/stage1/gen_queries.py \\
        sample --n 300 [--seed S] [--out FILE]
    UV_NO_SYNC=1 uv run --no-sync python bench/stage1/gen_queries.py \\
        filter --class paraphrase|mixed --candidates FILE --out FILE --rejects FILE
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from ground_truth import SNAPSHOT  # noqa: E402  (reuse the snapshot path from the sibling miner)

from siftd.embeddings.chunker import (  # noqa: E402
    extract_exchange_window_chunks,
    extract_tool_summary_chunks,
)

# --- DF bands (see module docstring for provenance) ------------------------
LOW_DF_MAX = 8  # ground_truth.mine_identifier_vocab identifier ceiling
MID_DF_MIN = 9
MID_DF_MAX = 50  # corpus p90 of vocab doc-frequency; topical miner member ceiling

# --- substantive-chunk band ------------------------------------------------
MIN_CHARS = 200
MAX_CHARS = 4000

DEFAULT_SEED = 20260705  # matches ground_truth.rng


# --- FTS-consistent tokenization + DF --------------------------------------


class FtsLexicon:
    """Tokenize arbitrary text exactly like content_fts, and look up term DF.

    An in-memory temp FTS5 table with content_fts's tokenizer turns any text into
    the same stemmed terms the main index stored; ``temp.v`` (main index vocab)
    provides doc-frequency, identical to ground_truth.py's ``temp.v``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.tok "
            "USING fts5(x, tokenize='porter unicode61 remove_diacritics 1')"
        )
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.tokv USING fts5vocab(temp, 'tok', 'row')")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.v USING fts5vocab(main, 'content_fts', 'row')")
        self.df: dict[str, int] = {t: d for t, d in conn.execute("SELECT term, doc FROM temp.v")}

    def terms(self, text: str) -> set[str]:
        self.conn.execute("DELETE FROM temp.tok")
        self.conn.execute("INSERT INTO temp.tok(x) VALUES(?)", (text,))
        return {t for (t,) in self.conn.execute("SELECT term FROM temp.tokv")}


# --- sample subcommand -----------------------------------------------------


def _conv_thirds(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """Split conversations into 3 equal-count age bands by started_at.

    Returns third -> [cid...] in started_at order. Equal thirds, not proportional,
    so the sample is not recency-biased.
    """
    cids = [r[0] for r in conn.execute("SELECT id FROM conversations ORDER BY started_at")]
    n = len(cids)
    b1, b2 = n // 3, 2 * n // 3
    by_third: dict[int, list[str]] = {0: [], 1: [], 2: []}
    for i, cid in enumerate(cids):
        t = 0 if i < b1 else (1 if i < b2 else 2)
        by_third[t].append(cid)
    return by_third


def _conv_chunks(conn: sqlite3.Connection, cid: str) -> list[dict]:
    """Chunk one conversation exactly as build_index.py does (exchange then tool)."""
    chunks = extract_exchange_window_chunks(conn, conversation_id=cid)
    chunks.extend(extract_tool_summary_chunks(conn, conversation_ids={cid}))
    return chunks


def _substantive(text: str) -> bool:
    return MIN_CHARS <= len(text) <= MAX_CHARS


def cmd_sample(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)

    by_third = _conv_thirds(conn)
    for t in by_third:
        rng.shuffle(by_third[t])

    n = args.n
    # Equal thirds; spread the remainder across the earliest bands.
    per_third = [n // 3 + (1 if i < n % 3 else 0) for i in range(3)]
    oversample = 3  # gather a surplus so chunk_type proportions can be honoured
    max_convs_per_third = 500  # bound the on-the-fly chunking work

    # pool[third][chunk_type] = list of chunk records (with meta attached)
    pool: dict[int, dict[str, list[dict]]] = {0: defaultdict(list), 1: defaultdict(list), 2: defaultdict(list)}
    convs_scanned = {0: 0, 1: 0, 2: 0}

    for t in (0, 1, 2):
        target = per_third[t] * oversample
        for cid in by_third[t]:
            if sum(len(v) for v in pool[t].values()) >= target:
                break
            if convs_scanned[t] >= max_convs_per_third:
                break
            convs_scanned[t] += 1
            chunks = _conv_chunks(conn, cid)
            n_chunks = len(chunks)
            for idx, c in enumerate(chunks):
                text = c["text"]
                if not _substantive(text):
                    continue
                pool[t][c["chunk_type"]].append(
                    {
                        "conversation_id": cid,
                        "chunk_type": c["chunk_type"],
                        "text": text,
                        "meta": {
                            "chunk_index": idx,
                            "n_chunks": n_chunks,
                            "source_ids": (c.get("source_ids") or [])[:3],
                            "age_third": t,
                            "char_len": len(text),
                        },
                    }
                )

    # Proportional-by-chunk_type selection within each equal-size third.
    selected: list[dict] = []
    sel_counts: dict[tuple[int, str], int] = {}
    for t in (0, 1, 2):
        buckets = pool[t]
        total = sum(len(v) for v in buckets.values())
        if total == 0:
            continue
        want = per_third[t]
        # Largest-remainder apportionment across chunk_types by observed share.
        raw = {ct: want * len(v) / total for ct, v in buckets.items()}
        alloc = {ct: int(x) for ct, x in raw.items()}
        short = want - sum(alloc.values())
        for ct, _ in sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:short]:
            alloc[ct] += 1
        for ct, k in alloc.items():
            bucket = buckets[ct]
            take = min(k, len(bucket))
            picks = rng.sample(bucket, take) if take < len(bucket) else list(bucket)
            selected.extend(picks)
            sel_counts[(t, ct)] = take

    # Deterministic emission order.
    selected.sort(key=lambda r: (r["meta"]["age_third"], r["chunk_type"], r["conversation_id"], r["meta"]["chunk_index"]))

    out = Path(args.out) if args.out else None
    lines = "".join(json.dumps(r) + "\n" for r in selected)
    if out:
        out.write_text(lines)
    else:
        sys.stdout.write(lines)

    # Stratification report.
    print(f"sampled {len(selected)} chunks (target {n}, seed {args.seed})", file=sys.stderr)
    print(f"  convs scanned per third: {convs_scanned}", file=sys.stderr)
    for t in (0, 1, 2):
        parts = ", ".join(f"{ct}={sel_counts.get((t, ct), 0)}" for ct in sorted(pool[t]))
        pool_parts = ", ".join(f"{ct}={len(v)}" for ct, v in sorted(pool[t].items()))
        print(f"  third {t}: selected {parts}  (pool: {pool_parts})", file=sys.stderr)


# --- filter subcommand -----------------------------------------------------


def _classify_shared(lex: FtsLexicon, query: str, chunk_text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Return (low_df_shared, mid_df_shared) as {term: df} maps."""
    shared = lex.terms(query) & lex.terms(chunk_text)
    low: dict[str, int] = {}
    mid: dict[str, int] = {}
    for term in shared:
        df = lex.df.get(term, 0)
        if 1 <= df <= LOW_DF_MAX:
            low[term] = df
        elif MID_DF_MIN <= df <= MID_DF_MAX:
            mid[term] = df
    return low, mid


def _judge(cls: str, low: dict[str, int], mid: dict[str, int]) -> str | None:
    """Return a reject_reason string, or None if the candidate is accepted."""
    if low:
        return "shared_low_df_token:" + ",".join(f"{k}({v})" for k, v in sorted(low.items()))
    if cls == "paraphrase":
        return None
    # mixed: must keep exactly 1-2 mid-DF anchors.
    if len(mid) == 0:
        return "no_mid_df_overlap"
    if len(mid) > 2:
        return "too_many_mid_df_overlap:" + ",".join(f"{k}({v})" for k, v in sorted(mid.items()))
    return None


def cmd_filter(args: argparse.Namespace) -> None:
    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    lex = FtsLexicon(conn)

    candidates = [
        json.loads(line) for line in Path(args.candidates).read_text().splitlines() if line.strip()
    ]

    accepted: list[dict] = []
    rejected: list[dict] = []
    for cand in candidates:
        query = cand["query"]
        cid = cand["conversation_id"]
        chunk_text = cand["chunk_text"]
        low, mid = _classify_shared(lex, query, chunk_text)
        reason = _judge(args.cls, low, mid)

        passthrough = {k: v for k, v in cand.items() if k not in ("query", "conversation_id", "chunk_text")}
        if reason is None:
            accepted.append(
                {
                    "class": args.cls,
                    "query": query,
                    "labels": [cid],
                    "meta": {
                        "miner": f"agent-{args.cls}",
                        "mid_df_tokens": mid,
                        "n_shared": len(low) + len(mid),
                        **passthrough,
                    },
                }
            )
        else:
            rejected.append({**cand, "reject_reason": reason})

    Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in accepted))
    if args.rejects:
        Path(args.rejects).write_text("".join(json.dumps(r) + "\n" for r in rejected))

    by_reason: dict[str, int] = defaultdict(int)
    for r in rejected:
        by_reason[r["reject_reason"].split(":", 1)[0]] += 1
    print(
        f"[{args.cls}] {len(accepted)} accepted / {len(rejected)} rejected "
        f"of {len(candidates)} candidates -> {args.out}",
        file=sys.stderr,
    )
    for reason, count in sorted(by_reason.items()):
        print(f"  reject {reason}: {count}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sample", help="draw source chunks for agent paraphrasing")
    s.add_argument("--n", type=int, default=300, help="target chunk count (default 300)")
    s.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"rng seed (default {DEFAULT_SEED})")
    s.add_argument("--out", default=None, help="output JSONL path (default stdout)")
    s.set_defaults(func=cmd_sample)

    f = sub.add_parser("filter", help="apply paraphrase/mixed rejection rules")
    f.add_argument("--class", dest="cls", required=True, choices=("paraphrase", "mixed"))
    f.add_argument("--candidates", required=True, help="candidate JSONL: {query, conversation_id, chunk_text, ...}")
    f.add_argument("--out", required=True, help="accepted ground-truth JSONL")
    f.add_argument("--rejects", default=None, help="rejected records with reject_reason")
    f.set_defaults(func=cmd_filter)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
