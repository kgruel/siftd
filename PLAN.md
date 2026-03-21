# 0.6.0: Output System + htmx Web UI

Worktree: `~/Code/siftd-htmx-ui` (branch `feat/htmx-ui`)
Main repo: `~/Code/siftd` (branch `main`)

## What's done

### Initial commit (85e8854)

- **html_fmt.py** — Fourth OutputFormat (peer to terminal/markdown/json). Returns HTML fragments.
- **HtmlEmitter** in narrative.py — Renders via walk_narrative(), same path as markdown/json.
- **html_routes.py** — Page shell (`/ui`), fragment endpoints (`/ui/query`, `/ui/search`).
- **Fidelity controls** — `?tools=true`, `?thinking=true`, `?brief=true`, `?full=true` query params.
- **tool_presenters.py** — Format-neutral extraction layer. 7 tool-specific extractors + generic fallback.
- **Architecture tests** — Route boundary enforcement: output can't hardcode routes; JSON/HTML routes can't cross-reference.

### Phase 1: tool presenter unification (a9de653)

- **Tool name aliases** — `bash`, `read`, `edit`, `write`, `grep`, `glob`, `todo` in `_EXTRACTORS`.
- **painted_bridge consumes ToolPresentation** — 7 `_render_*_lines` + duplicated JSON parsing → single `_presentation_to_lines`. −350 lines.
- **CSS extracted to static file** — `serve/static/siftd.css` via Litestar `StaticFilesConfig`.

### Phase 2: unified narrative walker (a9de653)

- **PaintedEmitter** implements NarrativeEmitter protocol. `render_narrative_block()` delegates to `walk_narrative()`. Single source of truth for fidelity gating.

### Phase 3: deep links + search (a9de653)

- **hx-push-url** on detail links → bookmarkable `/ui?id=...` URLs. Shell handles `?id=`, `?q=`, `?follow=`.
- **Live search** — `hybrid_search` (semantic+FTS5) when embeddings available, FTS5 fallback.

### Phase 4: filters + follow mode (a9de653, 2ece282)

- **Filter dropdowns** — workspace/model/tag `<select>` from `/ui/meta`, since/before date inputs.
- **Resizable panes** — draggable divider between list and detail (JS, 15%-85% clamp).
- **Follow mode** — "Live" nav link → `/ui/peek` lists active sessions, `/ui/follow` polls every 2s.
- **Search modes** — Chunks/Conversations toggle tabs, `aggregate_by_conversation()` API.
- **Stats dashboard** — `/ui/stats` with summary cards, by-model token breakdown, by-workspace cost breakdown, top tools.

### Phase 5: visual polish — "The Instrument" (current)

- **Palette** — warm obsidian substrate replacing GitHub-dark. Amber thread for metrics ("sifting for gold").
- **Typography** — IBM Plex Sans (structural) + IBM Plex Mono (data) from Google Fonts. Two-layer hierarchy.
- **DomainStyles** — CSS classes map 1:1 to terminal theme vocabulary. `.metric` gets amber, `.identifier` gets accent blue.
- **Tool cards** — full bordered cards with recessed backgrounds, replacing left-border-only. Diff lines teal/red.
- **Thinking blocks** — bordered cards with collapsible summary, italic content.
- **Stats dashboard** — CSS grid, amber stat values, engraved labels (uppercase, letter-spaced).
- **Nav** — instrument-panel treatment: recessed search input, animated link underlines, "Compact" density toggle.
- **Filters** — responsive wrapping, smaller footprint, text-overflow ellipsis.
- **Detail view** — breadcrumb nav (workspace > date > ID), middot-separated metadata, role labels (USER/ASSISTANT), accent left-border on prompts, 80ch reading measure.
- **Interaction** — htmx swap animations (fade+slide), loading bar on detail pane, pulsing live dot on follow mode, thin scrollbars, focus rings.
- **Empty states** — centered layout with icon and contextual hints.
- **Responsive** — below 768px: stacked panes, full-width search, hidden divider.

### Architecture (a9de653, 74d5cd6)

- **html_routes API-layer discipline** — imports only from `api`/`output`, not `storage`/`peek`/`search` directly.
- **Arch test** — `test_html_routes_use_api_layer` enforces this.
- **`list_workspaces`** gains `db_path` kwarg (matching `list_tags` pattern).
- **Stats API** — `get_usage_summary`, `get_usage_by_model`, `get_usage_by_workspace`, `get_cost_coverage` — proper aggregate SQL over full DB.

### Cost coverage (merged to main)

Investigation + fix landed on `main` (subtask `fix/cost-coverage-v2`):
- **Root cause**: pricing JOIN on NULL `provider_id` produced 0.0 instead of NULL; pricing table had only 10 manually-seeded rows.
- **Fix**: pricing JOIN now routes through harness source as fallback. NULL-safe cost expression (missing pricing → NULL, not 0.0). Bundled pricing seed for 10 models. Doctor check for coverage rate.
- **Result**: estimated 50% → 75% cost coverage on next re-ingest.

## Dissolved

- **Lazy turn loading** — no measurable latency problem. Revisit if conversations with 100+ turns cause visible lag.
- **Per-turn fidelity** — requires turn-level endpoint for a problem that doesn't exist yet.
- **Side-by-side comparison** — wait for Operation IR to land before building more views.

## Blocked: waiting for Operation IR

The Operation IR pattern formalizes what html_routes already does ad-hoc:

```
normalize(input) → Operation(fn, params, fidelity) → dispatch → render
```

Both CLI and serve routes will declare Operations instead of manually building the normalize→call→render pipeline. Landing this in the main repo first, then pulling into this branch to refactor routes and extend functionality.

**What changes when Operations land:**
- `html_routes.py` endpoints shrink to normalization + `dispatch(op, format)`
- New endpoints become trivial (just an Operation declaration)
- CLI and HTTP share the same dispatch path — feature parity by construction

**What to do after Operations land:**
- Refactor existing routes to Operation declarations
- Add remaining views: export, tagging UI, advanced search (thread mode)

## Next: needs design discussion

### Syntax highlighting + diff rendering

Tool cards show raw code in `<pre>` blocks. Two related improvements:
- **Syntax highlighting** in file reads and tool output (lightweight CDN highlighter)
- **Proper diff rendering** in edit tool cards (unified diff with line numbers, potentially side-by-side)

These affect the HtmlEmitter and tool_presenters interface — need to decide whether highlighting is CSS-only (class-based tokens from server) or client-side JS (Prism/highlight.js). Diff rendering may need changes to ToolPresentation's `removed`/`added` fields.

### Keyboard navigation

`j`/`k` list navigation, `Enter` to open detail. Pure JS, no route changes. Independent of Operations.

## Running it

```bash
cd ~/Code/siftd-htmx-ui
./dev setup           # if venv not ready
./dev check           # lint + test
.venv/bin/siftd serve --no-auth --port 8485  # browse http://localhost:8485/ui
```
