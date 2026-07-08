# bench — offline search-quality harness

Offline benchmarking for siftd's hybrid search: cached-artifact replicas of the
live engine, a ground-truth query set, and a fidelity gate that proves the
replica reproduces the real engine before any offline number is trusted.

## Standing policy: no search-behavior change ships without a bench re-gate

Any change touching `hybrid_search`, fusion (RRF/narrow), rollup, or the
strategy/preset defaults must pass the offline fidelity gate
(`bench/stage1/fidelity_gate.py`) against the cached artifacts before it ships.
The gate proves the offline replica still reproduces the LIVE engine's top-10
exactly — if the engine changed and the gate fails, either the replica must be
updated to match (and the sweep conclusions re-derived) or the change is a
ranking change and needs justification. **Material ranking changes require a
sweep re-run** (`bench/stage1/sweep.py`), not just the gate: the gate proves
fidelity, the sweep measures quality.

## Running the fidelity gate

The gate needs a run directory of cached artifacts (snapshot DB, per-arm embed
DB, cached query embeddings + FTS results — produced by `build_index.py` and
`cache_artifacts.py`; the current run lives at `bench/runs/stage1-2026-07-05/`,
which is local-only and not tracked in git). From the worktree root:

```console
env -u VIRTUAL_ENV UV_NO_SYNC=1 uv run --no-sync python bench/stage1/fidelity_gate.py --backend voyage
```

`--backend` selects the arm (`voyage`, `fastembed`, …) and expects
`embed-<backend>.db` plus `artifacts-<backend>/` in the run directory. The
script pins `PYTHONHASHSEED=0` (re-execing itself if needed), runs the REAL
engine (`siftd.api.search.hybrid_search`, with a cached-query backend so no
provider is called) and the offline replica over a deterministic ~40-query
probe for both engine configs (`narrow` and `rrf`), compares top-10
conversation rankings position-by-position, writes
`fidelity-report-<backend>.json` into the run directory, and exits nonzero on
any mismatch.

## Design and results records

The bench arc's records live in `docs/dev/` (note: `docs/dev/` is **gitignored**
— these files were force-added with `git add -f`; any new doc added there must
also be `git add -f`'d or it won't be tracked):

- `docs/dev/bench-plan-2026-07-05.md` — the stage-1 plan (arms, query classes,
  metrics, pre-committed decision rules).
- `docs/dev/bench-stage1-results-2026-07-06.md` — stage-1 results (provider
  arms, narrow-vs-RRF, dedup-on-RRF promotion).
- `docs/dev/bench-stage2-chunking-design-2026-07-06.md` — stage-2 typed-chunking
  design and its empirical rejection.
- `docs/dev/bench-lambda-sweep-2026-07-07.md` — MMR lambda sweep on the narrow
  path.
- `docs/dev/bench-topical-regen-2026-07-07.md` — topical query-class
  regeneration notes.
