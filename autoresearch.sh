#!/bin/bash
set -euo pipefail

# Pre-check: syntax validation of files in scope
.venv/bin/python3 -c "
import py_compile, sys
files = [
    'src/siftd/search.py',
    'src/siftd/storage/embeddings.py',
    'src/siftd/math.py',
    'src/siftd/storage/fts.py',
    'src/siftd/paths.py',
    'src/siftd/config.py',
]
ok = True
for f in files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        print(f'SYNTAX ERROR: {e}', file=sys.stderr)
        ok = False
if not ok:
    sys.exit(1)
"

# Benchmark: run 50 queries through hybrid_search with MMR
.venv/bin/python3 -c "
import json, sys, time
sys.path.insert(0, 'src')

from pathlib import Path
from siftd.search import hybrid_search
from siftd.paths import db_path, embeddings_db_path

db = db_path()
embed_db = embeddings_db_path()

with open('bench/queries.json') as f:
    data = json.load(f)

queries = [q for g in data['groups'] for q in g['queries']]
assert len(queries) == 50, f'Expected 50 queries, got {len(queries)}'

# Warm up: one query to load model + caches
hybrid_search(queries[0], db_path=db, embed_db_path=embed_db, rerank='mmr')

# Timed run
top1_scores = []
redundancies = []

start = time.perf_counter()
for q in queries:
    results = hybrid_search(q, db_path=db, embed_db_path=embed_db, rerank='mmr')
    if results:
        top1_scores.append(results[0].score)
        # Conversation redundancy: fraction of top-10 from same conv as rank-1
        top10 = results[:10]
        rank1_conv = top10[0].conversation_id
        same = sum(1 for r in top10 if r.conversation_id == rank1_conv)
        redundancies.append(same / len(top10))
end = time.perf_counter()

total_ms = round((end - start) * 1000, 1)
avg_top1 = round(sum(top1_scores) / len(top1_scores), 6) if top1_scores else 0
avg_redundancy = round(sum(redundancies) / len(redundancies), 4) if redundancies else 0

print(f'METRIC total_ms={total_ms}')
print(f'METRIC avg_top1={avg_top1}')
print(f'METRIC avg_redundancy={avg_redundancy}')
"
