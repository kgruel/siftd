# Autoresearch Ideas — Current State (CLI sweep)

## Recently completed

### `api/sync.py` — ✅ 100%
- 296/296 covered, miss 0
- Transport branches covered: SSH/HTTP/local, auth/errors/timeouts, dry-run/update logic

### `cli/db.py` — ✅ 100%
- 398/398 covered, miss 0
- Covered remote add/list/remove, push/pull, send/receive, merge/slice, and key error paths

### `cli/search.py` (no-embed benchmark scope) — ✅ 100%
- 400/400 covered, miss 0
- Added robust no-embed tests for:
  - mode selection and validation
  - FTS-only error/empty/non-empty output paths
  - cmd_search branch behavior (threshold/first/thread/conversations/refs/by-time)
  - delegation/formatter and build-index error branches

## Pruned stale ideas
- ❌ "next target: cli/search" (done)
- ❌ "sync transport untestable" (done)
- ❌ "need structural changes before search" (handled enough for no-embed branch testing)

## Highest ROI next targets

### 1) `cli/data.py` (still largest CLI gap)
- Current miss remains high (copy + doctor subpaths dominate)
- Biggest clusters:
  - `cmd_copy`
  - doctor renderers (`_doctor_run_plain`, `_doctor_run_json`, `_doctor_run_painted`)
  - migrate/backfill edge handlers
- Suggested implementation prep:
  - extract copy flow helper(s)
  - split doctor output modes into tighter helpers with dependency injection

### 2) `cli/install.py` + `cli/meta.py`
- Medium-sized miss pockets with many deterministic branches
- Good candidates for quick branch-coverage gains once data.py is underway

### 3) `cli/query.py` + `cli/peek.py`
- Useful but less ROI than data/install/meta in raw miss reduction

## De-prioritized for now
- `serve/*`, `embeddings/*` (marker/runtime heavy)
- `adapters/template.py` (example code)
- terminal-UI-only defensive branches with brittle TTY behavior
