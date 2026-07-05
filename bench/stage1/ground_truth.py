#!/usr/bin/env python3
"""Stage-1 programmatic ground-truth miners (bench plan 2026-07-05).

Three classes come straight from the snapshot, labels exact by construction:

- identifier: low-DF tokens (fts5vocab) + regex-mined code identifiers whose
  labels are literal-substring-verified. Persona: the stack-trace chaser.
- tool: file basenames touched in 2..12 conversations (ab_rrf pattern, scaled).
  Persona: "where did I edit X".
- topical: conversation tags with 3..50 members. Persona: retrospective writer.

Paraphrase + mixed classes are agent-generated separately (generate_paraphrases).

Usage:
    UV_NO_SYNC=1 uv run --no-sync python bench/stage1/ground_truth.py
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

RUN_DIR = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
SNAPSHOT = RUN_DIR / "siftd-snapshot.db"

rng = random.Random(20260705)  # deterministic mining


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"  {len(rows):4d} -> {path.name}", file=sys.stderr)


def fts_label_conversations(conn: sqlite3.Connection, match: str) -> set[str]:
    """Conversations matching an FTS query (labels for vocab-mined tokens)."""
    rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM content_fts WHERE content_fts MATCH ?",
        (match,),
    ).fetchall()
    return {r[0] for r in rows}


# --- identifier class -----------------------------------------------------

HEX_RE = re.compile(r"^[0-9a-f]{12,}$")
ULID_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{20,26}$")
MIXED_RE = re.compile(r"^(?=.*[a-z])(?=.*[0-9])[a-z0-9]{8,}$")
CODE_ID_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+){1,4}\b")


def term_shape(term: str) -> str | None:
    if HEX_RE.match(term):
        return "hex"
    if ULID_RE.match(term):
        return "ulid"
    if MIXED_RE.match(term):
        return "mixed"
    return None


def mine_identifier_vocab(conn: sqlite3.Connection, per_bucket: int = 8) -> list[dict]:
    """Low-DF vocab tokens, stratified by DF band x shape. Labels = FTS-exact."""
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.v USING fts5vocab(main, 'content_fts', 'row')")
    rows = conn.execute("SELECT term, doc FROM temp.v WHERE doc BETWEEN 2 AND 8 AND length(term) >= 8").fetchall()
    buckets: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for term, doc in rows:
        shape = term_shape(term)
        if shape is None:
            continue
        band = "2" if doc == 2 else ("3-4" if doc <= 4 else "5-8")
        buckets.setdefault((band, shape), []).append((term, doc))

    out: list[dict] = []
    for (band, shape), cands in sorted(buckets.items()):
        rng.shuffle(cands)
        for term, doc in cands[:per_bucket]:
            labels = fts_label_conversations(conn, f'"{term}"')
            if not 2 <= len(labels) <= 8:
                continue
            out.append(
                {
                    "class": "identifier",
                    "query": term,
                    "labels": sorted(labels),
                    "meta": {"miner": "vocab", "shape": shape, "df_band": band},
                }
            )
    return out


def mine_identifier_code(conn: sqlite3.Connection, sample_rows: int = 4000, max_out: int = 60) -> list[dict]:
    """snake_case identifiers regex-mined from sampled texts.

    The porter/unicode61 tokenizer splits on '_', so these never appear in the
    vocab as single tokens. Candidates are narrowed by an FTS phrase match, then
    labels are LITERAL-substring verified — a phrase hit on the prose "hybrid
    search" must not label a query for `hybrid_search`.
    """
    max_rowid = conn.execute("SELECT max(rowid) FROM content_fts").fetchone()[0]
    seen: dict[str, int] = {}
    for _ in range(sample_rows):
        rid = rng.randint(1, max_rowid)
        row = conn.execute("SELECT text_content FROM content_fts WHERE rowid = ?", (rid,)).fetchone()
        if row is None or row[0] is None:
            continue
        for m in CODE_ID_RE.finditer(row[0][:20000]):
            ident = m.group(0)
            if 10 <= len(ident) <= 40:
                seen[ident] = seen.get(ident, 0) + 1

    candidates = sorted(seen, key=lambda k: seen[k])  # rare-in-sample first
    rng.shuffle(candidates)
    out: list[dict] = []
    used: set[str] = set()
    for ident in candidates:
        if len(out) >= max_out:
            break
        root = ident.lower().split("_")[0]
        if root in used:  # avoid near-duplicate families (foo_bar, foo_baz)
            continue
        phrase = '"' + " ".join(ident.split("_")) + '"'
        try:
            hits = conn.execute(
                "SELECT conversation_id, text_content FROM content_fts WHERE content_fts MATCH ? LIMIT 400",
                (phrase,),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        if len(hits) >= 400:  # too common to label exactly
            continue
        labels = {cid for cid, text in hits if ident in (text or "")}
        if 2 <= len(labels) <= 10:
            used.add(root)
            out.append(
                {
                    "class": "identifier",
                    "query": ident,
                    "labels": sorted(labels),
                    "meta": {"miner": "code", "shape": "snake_case"},
                }
            )
    return out


# --- tool-findability class -----------------------------------------------


def mine_tool(conn: sqlite3.Connection, max_out: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT etc.input, e.conversation_id
        FROM events e JOIN event_tool_call etc ON etc.event_id = e.id
        LEFT JOIN tools t ON t.id = etc.tool_id
        WHERE e.kind='tool_call' AND (t.category='file' OR t.name LIKE 'file.%')
        """
    ).fetchall()
    by_base: dict[str, set[str]] = {}
    for inp, cid in rows:
        try:
            path = (json.loads(inp) or {}).get("file_path") or ""
        except (ValueError, TypeError):
            path = ""
        base = os.path.basename(path)
        if base and "." in base:
            by_base.setdefault(base, set()).add(cid)

    candidates = [(b, cids) for b, cids in by_base.items() if 2 <= len(cids) <= 12]
    rng.shuffle(candidates)
    # Stratify: half small-DF (2-4), half larger (5-12).
    small = [c for c in candidates if len(c[1]) <= 4][: max_out // 2]
    large = [c for c in candidates if len(c[1]) > 4][: max_out - len(small)]
    out = []
    for base, cids in small + large:
        out.append(
            {
                "class": "tool",
                "query": f"the conversation where {base} was edited",
                "labels": sorted(cids),
                "meta": {"basename": base, "df": len(cids)},
            }
        )
    return out


# --- topical class ----------------------------------------------------------


def tag_to_query(name: str) -> str:
    phrase = name.split(":", 1)[-1].replace("-", " ")
    return phrase


def mine_topical(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.name, GROUP_CONCAT(ta.target_id)
        FROM tag_assignments ta JOIN tags t ON t.id = ta.tag_id
        WHERE ta.target_kind = 'conversation'
        GROUP BY t.id HAVING COUNT(*) BETWEEN 3 AND 50
        """
    ).fetchall()
    return [
        {
            "class": "topical",
            "query": tag_to_query(name),
            "labels": sorted(set(ids.split(","))),
            "meta": {"tag": name},
        }
        for name, ids in rows
    ]


def main() -> None:
    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)

    print("mining identifier (vocab)...", file=sys.stderr)
    ident = mine_identifier_vocab(conn)
    print("mining identifier (code)...", file=sys.stderr)
    ident += mine_identifier_code(conn)
    write_jsonl(RUN_DIR / "gt-identifier.jsonl", ident)

    print("mining tool-findability...", file=sys.stderr)
    write_jsonl(RUN_DIR / "gt-tool.jsonl", mine_tool(conn))

    print("mining topical...", file=sys.stderr)
    write_jsonl(RUN_DIR / "gt-topical.jsonl", mine_topical(conn))
    conn.close()


if __name__ == "__main__":
    main()
