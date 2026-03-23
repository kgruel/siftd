# Autoresearch Ideas — Current State

## Recently completed (100%)

- `api/sync.py` (296/296)
- `cli/db.py` (398/398)
- `cli/search.py` no-embed lane (400/400)
- `cli/data.py` (674/674)
- `cli/install.py` (303/303)
- `cli/meta.py` (229/229)
- `cli/query.py` (287/287)
- `cli/peek.py` (214/214)
- `cli/tool_search.py` (157/157)
- `cli/upgrade.py` (135/135)
- `cli/export.py` (55/55)
- `cli/tags.py` (426/426)
- `cli/sessions.py` (50/50) — step-down complete (efficiency improved to 0.25)
- `output/format_registry.py` (52/52) — step-down complete (efficiency improved to 0.05)

## Pruned stale ideas

- Removed stale `cli/tags.py`, `cli/sessions.py`, and `output/format_registry.py` targets after saturation.
- Removed completed CLI sweep leftovers; keep only unsaturated post-CLI lanes.

## Highest ROI next targets

### 1) `output/terminal_fmt.py`
- Small remaining uncovered branch in `render_detail` raw-turns compatibility path.
- Focused lane: `tests/test_output_terminal_fmt.py`.

### 2) other `output/*` formatter edge branches (if still not saturated)
### 3) targeted `api/*` deterministic leaf modules

## De-prioritized

- `serve/*`, `embeddings/*` (marker/runtime heavy)
- `adapters/template.py` (example code)
- brittle terminal/TTY-only defensive branches unless they block practical milestones
