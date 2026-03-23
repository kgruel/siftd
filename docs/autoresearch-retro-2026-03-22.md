# Autoresearch Retrospective — 2026-03-22

## Scope covered

- `siftd.cli.search` (no-embed lane) → 100%
- `siftd.cli.data` → 100%
- `siftd.cli.install` → 100%

## What worked well

1. **Stairstep discipline**
   - Keeping every real coverage increase prevented local over-optimization.
   - Efficiency cleanups were deferred until after branch saturation.

2. **Lane pivots at saturation**
   - Fast switching of `autoresearch.sh` and `autoresearch.checks.sh` after hitting 100% avoided diminishing returns.

3. **Targeted branch tests over broad integration rewrites**
   - Direct helper/edge-path tests yielded high line gains with low production risk.

4. **Full checks always on**
   - Caught lint/import drift and prevented benchmark-only regressions.

## Pain points

1. **Heavy target timing loops**
   - Large test files (`test_data.py`) can push benchmark duration into timeout territory.

2. **Pre-existing lint debt blocking baseline**
   - Checks failures on unrelated lint can delay baseline establishment.

3. **Monkeypatch isolation complexity**
   - Deep branch tests required careful restoration of globals/modules (`sys.stdout`, stubs, copy helpers).

## Operational playbook updates

1. **When starting a new lane**
   - Update benchmark + checks target files first.
   - Run a checks-passing baseline before step-ups.

2. **Timeout hygiene**
   - Use shorter timing loops for heavy files (e.g., 3 runs) or higher run timeout.

3. **Coverage strategy**
   - Prioritize deterministic CLI control-flow branches.
   - Avoid overfitting by retaining full-suite coverage measurement.

4. **Backlog maintenance**
   - Prune stale ideas immediately after saturation.
   - Record next-ranked targets explicitly.

## Next suggested lanes (post-retro)

1. `siftd.cli.meta`
2. `siftd.cli.query`
3. `siftd.cli.peek`

---

## 200-run snapshot (202 runs as of now)

### Headline numbers

- **Total runs:** 202
- **Keep:** 173
- **Discard:** 16
- **Crash:** 7
- **Checks failed:** 6
- **Config pivots:** 64

### Biggest wins

- **Ingest performance:** ~121s ➜ ~41.9s (major throughput gain early).
- **Serve arc:** completed and saturated
  - `serve/client.py` 81/81
  - `serve/delegation.py` 130/130
  - `serve/auth.py` 84/84
  - `serve/routes.py` 185/185
  - `serve/html_routes.py` 288/288
  - `serve/app.py` 20/20
- **Broad module saturation:** ~50 modules now listed as complete in `autoresearch.ideas.md`.

### Methodology that held up

- Stairstep keep/discard rule worked: accept coverage ratchets, then step down LOC/time at equal coverage.
- Full-suite coverage measurement prevented narrow benchmark gaming.
- Deterministic filters and checks kept progress reliable despite flaky integration pockets.

### Main pain points

- Intermittent serve integration 500s in long full-suite coverage/order runs (currently excluded in `-k` filter).
- Occasional benchmark-script drift after pivots (fixed by explicit re-baselines).
- Long heavy-lane timing loops where noise/timeout risk rises.

### Current phase

- We are **post-serve**, back to **small module sweeps + efficiency cleanup**.
- Recent closures: `adapters/gemini_cli.py` and `backfill.py` to 100%.
- Active tiny-miss lane: `content/filters.py` (67/69 at latest baseline).
