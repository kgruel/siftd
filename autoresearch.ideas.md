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
- `output/terminal_fmt.py` (125/125) — step-down complete (efficiency returned to 0.04)
- `output/painted_bridge.py` (372/374) — near-saturated; remaining 2 lines appear structurally unreachable (`if not parts` after unconditional header append).

## Pruned stale ideas

- Removed stale `cli/tags.py`, `cli/sessions.py`, `output/format_registry.py`, and `output/terminal_fmt.py` targets after saturation.
- Demoted `output/painted_bridge.py` from active target to near-saturated notes.
- Removed completed CLI sweep leftovers; keep only unsaturated post-CLI lanes.

## Highest ROI next targets

### 1) `output/narrative.py`
- Large deterministic branch surface remains (especially `HtmlEmitter`/`MarkdownEmitter` presentation paths).
- Focused lane: `tests/test_output_narrative.py`.

### 2) other `output/*` formatter edge branches (if still not saturated)
### 3) targeted `api/*` deterministic leaf modules

## De-prioritized

- `serve/*`, `embeddings/*` (marker/runtime heavy)
- `adapters/template.py` (example code)
- brittle terminal/TTY-only defensive branches unless they block practical milestones
