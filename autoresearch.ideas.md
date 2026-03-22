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

### `cli/peek.py` — ✅ 100%
- 214/214 covered, miss 0

### `cli/tool_search.py` — ✅ 100%
- 157/157 covered, miss 0

## Pruned stale ideas
- ❌ "next target: cli/search" (done)
- ❌ "next target: cli/data" (done)
- ❌ "next target: cli/install" (done)
- ❌ "next target: cli/meta" (done)
- ❌ "next target: cli/query" (done)
- ❌ "next target: cli/peek" (done)
- ❌ "next target: cli/tool_search" (done)

## Highest ROI next targets

### 1) `cli/upgrade.py`
- Small deterministic CLI surface; good candidate for quick saturation

### 2) `cli/export.py`
- Likely next practical CLI branch target after upgrade

## De-prioritized for now
- `serve/*`, `embeddings/*` marker/runtime-heavy paths
- `adapters/template.py` example code
- brittle terminal-only defensive branches unless they block practical milestones
