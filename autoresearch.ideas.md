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

### `cli/install.py` — ✅ 100%
- 303/303 covered, miss 0

## Pruned stale ideas
- ❌ "next target: cli/search" (done)
- ❌ "next target: cli/data" (done)
- ❌ "next target: cli/install" (done)

## Highest ROI next targets

### 1) `cli/meta.py`
- Deterministic output/control-flow branches (status/config/adapters/workspaces/path)
- Should be lighter/faster benchmark lane than `cli/data.py`

### 2) `cli/query.py`
- Good remaining miss pockets; may require richer fixtures

### 3) `cli/peek.py`
- Valuable coverage but more stream/IO behavior and timing variance

## De-prioritized for now
- `serve/*`, `embeddings/*` marker/runtime-heavy paths
- `adapters/template.py` example code
- brittle terminal-only defensive branches unless they block practical milestones
