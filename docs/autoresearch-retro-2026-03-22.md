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
