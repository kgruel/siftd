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

### `cli/meta.py` — ✅ 100%
- 229/229 covered, miss 0

### `cli/query.py` — ✅ 100%
- 287/287 covered, miss 0

## Pruned stale ideas
- ❌ "next target: cli/search" (done)
- ❌ "next target: cli/data" (done)
- ❌ "next target: cli/install" (done)
- ❌ "next target: cli/meta" (done)
- ❌ "next target: cli/query" (done)

## Highest ROI next targets

### 1) `cli/peek.py`
- Remaining high-value CLI surface not yet saturated in this phase
- Expect stream/IO branch complexity; likely needs careful fixture design

### 2) `cli/tool_search.py`
- Candidate if peek branch setup is too costly for near-term loop velocity

## De-prioritized for now
- `serve/*`, `embeddings/*` marker/runtime-heavy paths
- `adapters/template.py` example code
- brittle terminal-only defensive branches unless they block practical milestones
