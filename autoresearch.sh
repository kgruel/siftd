#!/usr/bin/env bash
set -euo pipefail

# Single-process benchmark: avoids subprocess/cache noise entirely.
# Measures the same code path as `siftd query`, minus Python boot (~30ms constant).

.venv/bin/python - <<'BENCH'
import time

# --- import_ms: full module tree (same as CLI entrypoint) ---
t_imp0 = time.perf_counter()
from siftd.api import list_conversations
from siftd.output.format_registry import select_format
from siftd.output.painted_bridge import emit_output
t_imp1 = time.perf_counter()
import_ms = round((t_imp1 - t_imp0) * 1000, 1)

# --- sql_ms: list_conversations (cold SQLite internal cache) ---
t_sql0 = time.perf_counter()
conversations = list_conversations(limit=10)
t_sql1 = time.perf_counter()
sql_ms = round((t_sql1 - t_sql0) * 1000, 1)

# --- render_ms: format output (same as CLI TTY path) ---
t_rnd0 = time.perf_counter()
fmt = select_format(json_mode=False, is_tty=True)
from painted import Fidelity
output = fmt.render_list(conversations, Fidelity())
t_rnd1 = time.perf_counter()
render_ms = round((t_rnd1 - t_rnd0) * 1000, 1)

total_ms = round(import_ms + sql_ms + render_ms, 1)

assert len(conversations) == 10, f"Expected 10 results, got {len(conversations)}"

print(f"METRIC total_ms={total_ms}")
print(f"METRIC import_ms={import_ms}")
print(f"METRIC sql_ms={sql_ms}")
print(f"METRIC render_ms={render_ms}")
BENCH
