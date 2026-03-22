# Autoresearch Ideas — Current State (Sync + CLI DB)

## What landed

### 1) `api/sync.py` is now **100% covered**
- Benchmark: `sync transport coverage efficiency`
- Final: **296/296 covered**, miss **0**, efficiency **0.96**
- Added meaningful transport tests for:
  - SSH push/pull error mapping (disconnect, permission, channel, timeout)
  - HTTP push/pull auth + status/connect errors
  - local pull/push merge + dry-run branches
  - sync_push/sync_pull branch routing and last_push/last_pull updates

### 2) `cli/db.py` is now **100% covered**
- Benchmark: `cli-db coverage efficiency`
- Final: **398/398 covered**, miss **0**, efficiency **0.96**
- Added command-level tests for:
  - `db remote add/list/remove`
  - `db push/pull` success/error/dry-run/zero-conversation/file-not-found
  - `db send/receive` tty/stdin handling, stream path, API error mapping
  - `db info/vacuum/backup/restore/slice/merge` missing-file + runtime branches

## Biggest remaining ROI (next implementation targets)

Current CLI misses (non-serve/non-embeddings test run):
- `src/siftd/cli/data.py` → **182 miss** (73%)
- `src/siftd/cli/search.py` → **153 miss** (62%)
- `src/siftd/cli/install.py` → **155 miss** (49%)
- `src/siftd/cli/meta.py` → **107 miss** (53%)
- `src/siftd/cli/query.py` → **98 miss** (66%)
- `src/siftd/cli/peek.py` → **82 miss** (62%)

### High-value function clusters

#### `cli/data.py`
- `cmd_copy` (63 miss)
- `_doctor_run_painted` (34 miss)
- `cmd_migrate` (14 miss)
- `_doctor_fix` (13 miss)
- `_doctor_run_plain` (12 miss)

#### `cli/search.py`
- `cmd_search` (80 miss)
- `_search_fts_only` (18 miss)
- `_search_build_index` (17 miss)
- `_enrich_context` (17 miss)

## Implementation improvements needed for next gains

1. **Split `cli/data.py` into smaller command modules**
   - Move doctor subcommand rendering into dedicated helpers (`doctor_plain`, `doctor_json`, `doctor_painted`)
   - Extract `cmd_copy` flow (validate/filter/copy/report) into pure helpers
   - This will let tests assert behavior without full command orchestration in each case.

2. **Refactor `cli/search.py` command body**
   - `cmd_search` is still too monolithic; extract decision tree into testable helper funcs:
     - delegation decision
     - empty-result formatting
     - enrichment pipeline selection
   - Keep parser wiring thin; move logic into pure functions with injected dependencies.

3. **Add shared CLI command test helpers**
   - Create helpers for:
     - fake stdin/stdout objects (send/receive-like paths)
     - patchable result factories for sync/search command return objects
     - reusable remote-config fixtures
   - Avoid repeated boilerplate in each `tests/cli/test_*.py` file.

4. **Use full-suite filter without stale exclusions when possible**
   - `test_doctor` now passes; keep only genuinely needed exclusions (`test_basics`, `test_follow_session`, and environment-gated markers).

## De-prioritized / not worth chasing now
- `serve/*` and `embeddings/*` (marker-gated and environment-dependent)
- `adapters/template.py` (example template)
- terminal/UI-only defensive branches that require brittle TTY internals
