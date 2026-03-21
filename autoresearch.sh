#!/bin/bash
set -euo pipefail

# Benchmark a full fresh ingest into a temp database
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

.venv/bin/python -c "
import time, json
from pathlib import Path
from siftd.storage.sqlite import open_database
from siftd.adapters.registry import load_all_adapters
from siftd.ingestion import ingest_all

db_path = Path('$TMPDIR/bench.db')
conn = open_database(db_path)

plugins = load_all_adapters()
adapters = [p.module for p in plugins]

start = time.perf_counter()
stats = ingest_all(conn, adapters)
elapsed = time.perf_counter() - start

conn.close()

print(f'METRIC ingest_s={elapsed:.3f}')
print(f'METRIC conversations={stats.conversations}')
"
