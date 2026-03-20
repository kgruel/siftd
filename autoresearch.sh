#!/usr/bin/env bash
set -euo pipefail

# Single-process benchmark for siftd query speed.
# Covers all major query patterns to avoid over-optimizing for one case.

.venv/bin/python - <<'BENCH'
import time

# --- import_ms ---
t_imp0 = time.perf_counter()
from siftd.api import list_conversations
from siftd.output.format_registry import select_format
from siftd.output.painted_bridge import emit_output
t_imp1 = time.perf_counter()
import_ms = round((t_imp1 - t_imp0) * 1000, 1)

from datetime import datetime, timedelta, timezone

def bench(label, **kwargs):
    t0 = time.perf_counter()
    result = list_conversations(**kwargs)
    t1 = time.perf_counter()
    ms = round((t1 - t0) * 1000, 1)
    return ms, result

# Query variants (order matters: first is cold SQLite cache)
sql_ms, convs        = bench("default",       limit=10)
sql50_ms, _          = bench("limit50",        limit=50)
since7d_ms, _        = bench("since7d",        limit=10, since=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
workspace_ms, _      = bench("workspace",      limit=10, workspace="siftd")
model_ms, _          = bench("model",          limit=10, model="claude-opus")
tag_ms, _            = bench("tag",            limit=10, tags=["co-creation"])
oldest_ms, _         = bench("oldest",         limit=10, oldest_first=True)
nolimit_ms, _        = bench("nolimit",        limit=0,  since=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())

assert len(convs) == 10, f"Expected 10 results, got {len(convs)}"

# --- render_ms ---
t_rnd0 = time.perf_counter()
fmt = select_format(json_mode=False, is_tty=True)
from painted import Fidelity
output = fmt.render_list(convs, Fidelity())
t_rnd1 = time.perf_counter()
render_ms = round((t_rnd1 - t_rnd0) * 1000, 1)

total_ms = round(import_ms + sql_ms + render_ms, 1)

print(f"METRIC total_ms={total_ms}")
print(f"METRIC import_ms={import_ms}")
print(f"METRIC sql_ms={sql_ms}")
print(f"METRIC render_ms={render_ms}")
print(f"METRIC sql50_ms={sql50_ms}")
print(f"METRIC since7d_ms={since7d_ms}")
print(f"METRIC workspace_ms={workspace_ms}")
print(f"METRIC model_ms={model_ms}")
print(f"METRIC tag_ms={tag_ms}")
print(f"METRIC oldest_ms={oldest_ms}")
print(f"METRIC nolimit_ms={nolimit_ms}")
BENCH
