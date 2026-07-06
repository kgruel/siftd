"""One-off: annotate sampled chunks with mid-DF anchor surface words for the mixed class.

For each chunk, find actual words in the text whose porter stem lands in the
mid-DF band (9 <= doc <= 50). The generation agent is told to keep 1-2 of these
verbatim; gen_queries.py filter still validates post-hoc, so this is an
efficiency aid, not a trust source.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from gen_queries import MID_DF_MAX, MID_DF_MIN, FtsLexicon  # noqa: E402
from ground_truth import SNAPSHOT  # noqa: E402

RUN = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
lex = FtsLexicon(conn)

stem_cache: dict[str, str | None] = {}


def stem_of(word: str) -> str | None:
    if word not in stem_cache:
        ts = lex.terms(word)
        stem_cache[word] = next(iter(ts)) if len(ts) == 1 else None
    return stem_cache[word]


records = [json.loads(x) for x in (RUN / "sample-chunks.jsonl").read_text().splitlines()]
n_with = 0
for r in records:
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", r["text"])}
    anchors = []
    for w in sorted(words):
        s = stem_of(w)
        if s is None:
            continue
        df = lex.df.get(s, 0)
        if MID_DF_MIN <= df <= MID_DF_MAX:
            anchors.append((df, w))
    anchors.sort()  # lower df = more distinctive, listed first
    r["meta"]["anchor_words"] = [w for _, w in anchors[:6]]
    if anchors:
        n_with += 1

(RUN / "sample-chunks-anchored.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
print(f"{n_with}/{len(records)} chunks have >=1 mid-DF anchor word", file=sys.stderr)
