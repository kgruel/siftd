# Autoresearch Ideas — Current State (CLI sweep)

## Recently completed

### `api/sync.py` — ✅ 100%
- 296/296 covered, miss 0

### `cli/db.py` — ✅ 100%
- 398/398 covered, miss 0

### `cli/search.py` (no-embed benchmark scope) — ✅ 100%
- 400/400 covered, miss 0

### `cli/data.py` — ✅ 100%
- 674/674 covered, miss 0
- Covered copy/doctor/migrate/backfill/ingest branches including painted/json/plain doctor runners and fix registry paths

## Pruned stale ideas
- ❌ "next target: cli/search" (done)
- ❌ "next target: cli/data" (done)
- ❌ "doctor/copy branches likely blocked" (now covered)

## Highest ROI next targets

### 1) `cli/install.py`
- Good deterministic branch density with low runtime overhead vs `test_data.py`
- Likely better efficiency slope than current heavy data benchmark

### 2) `cli/meta.py`
- Medium branch pockets, mostly deterministic CLI output paths

### 3) `cli/query.py`
- Still meaningful miss pockets but some branches depend on broader fixtures

### 4) `cli/peek.py`
- Useful, but more IO/stream behavior; likely lower immediate ROI than install/meta

## De-prioritized for now
- `serve/*`, `embeddings/*` marker/runtime-heavy paths
- `adapters/template.py` example code
- brittle terminal-only defensive branches unless they block practical coverage milestones
