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
- **Lazy turn loading** — deferred (no measurable latency to solve).

### Phase 4: filters + follow mode (a9de653, 2ece282)

- **Filter dropdowns** — workspace/model/tag `<select>` from `/ui/meta`, since/before date inputs.
- **Resizable panes** — draggable divider between list and detail (JS, 15%-85% clamp).
- **Follow mode** — "Live" nav link → `/ui/peek` lists active sessions, `/ui/follow` polls every 2s.
- **Search modes** — Chunks/Conversations toggle tabs, `aggregate_by_conversation()` API.
- **Stats dashboard** — `/ui/stats` with summary cards, by-model token breakdown, by-workspace cost breakdown, top tools.
- **Per-turn fidelity** — deferred (same reason as lazy turns: needs turn-level endpoint).

### Architecture (a9de653, 74d5cd6)

- **html_routes API-layer discipline** — imports only from `api`/`output`, not `storage`/`peek`/`search` directly.
- **Arch test** — `test_html_routes_use_api_layer` enforces this.
- **`list_workspaces`** gains `db_path` kwarg (matching `list_tags` pattern).
- **Stats API** — `get_usage_summary`, `get_usage_by_model`, `get_usage_by_workspace`, `get_cost_coverage` — proper aggregate SQL over full DB.

## Dissolved

- **Lazy turn loading** — no measurable latency problem. Revisit if conversations with 100+ turns cause visible lag.
- **Per-turn fidelity** — requires turn-level endpoint for a problem that doesn't exist yet.
- **Side-by-side comparison** — wait for Operation IR to land before building more views.

## Blocked: waiting for Operation IR

The Operation IR pattern (design discussion: session b5398cce, 2026-03-21) formalizes what html_routes already does ad-hoc:

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
- CSS/visual polish (pure presentation, independent of dispatch)

## Issues for Subtasks

### Cost coverage (24%)

`conversation_stats.cost` is only populated for 2,507/10,300 conversations (24%). Values look like placeholder calculations, not real API costs. The stats dashboard shows "$161 total" which is obviously wrong.

**Investigation needed:**
- How does ingest populate `conversation_stats.cost`? Which adapters contribute cost data?
- Are there conversations with token data but no cost? (Yes — 76% of them.)
- Can cost be back-calculated from token counts + model pricing tables?
- Should `siftd doctor` flag low cost coverage as a data quality issue?

### Stats query accuracy

The by-model cost attribution was removed because cost lives at the conversation level and can't be reliably split across models in multi-model conversations. If cost calculation improves, revisit whether per-model cost is feasible.

## Running it

```bash
cd ~/Code/siftd-htmx-ui
./dev setup           # if venv not ready
./dev check           # lint + test
.venv/bin/siftd serve --no-auth --port 8485  # browse http://localhost:8485/ui
```
