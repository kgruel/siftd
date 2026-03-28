# fix-serialization-cycle Plan

## Objective

Break the `api.stats <-> serialization.stats` cycle by restoring one-way direction:

- `serialization -> api` is allowed
- `api -> serialization` is not allowed

Constraints honored:
- no CLI/serve behavior changes
- no stats-cache mechanism redesign
- keep anti-drift serializer contract checks

## Current state (verified)

- `src/siftd/api/stats.py` imports `siftd.serialization.stats.serialize_stats` inside `_stats_to_dict()` and uses it in `write_stats_cache()`.
- `src/siftd/serialization/stats.py` type-check imports `DatabaseStats` from `siftd.api.stats`.
- Cycle in dependency audit: `api.stats <-> serialization.stats`.
- `src/siftd/serialization/conversations.py` type-check imports API conversation types; no reverse `api -> serialization` import there.
- Repo scan found only one `api -> serialization` import today: `src/siftd/api/stats.py`.

## Placement decision for `serialize_stats` / `_stats_to_dict`

### Recommended (minimal + coherent)

Make `api.stats` the owner of the stats cache payload mapping and make `serialization.stats.serialize_stats()` delegate to it.

Why:
- `write_stats_cache()` already lives in `api.stats`; keeping its serializer local removes the inversion with minimal churn.
- preserves one-way dependency (`serialization -> api`) and removes forbidden direction.
- avoids dual implementations drifting.

### Option considered: move cache-write responsibility to callers

Not recommended for this task.

Why:
- touches CLI + serve call sites and broadens scope.
- effectively restructures cache-write flow, which is explicitly out of scope.

## Minimal change set

1. `src/siftd/api/stats.py`
- Replace `_stats_to_dict()` implementation so it no longer imports `siftd.serialization.stats`.
- Keep output shape identical to current `serialize_stats` contract.

2. `src/siftd/serialization/stats.py`
- Keep public `serialize_stats()` entry point.
- Delegate to API-owned mapper (single source of truth), preserving existing import path for output/serve/tests.
- Keep `DatabaseStats` typing contract in place.

3. Tests (anti-drift preserved)
- Keep `tests/test_stats_cache.py::test_serialize_stats_contract_matches_dataclasses` as the contract guard.
- Add/adjust a parity assertion that `serialize_stats(stats)` and `_stats_to_dict(stats)` are identical.
- Keep existing cache round-trip tests unchanged (behavioral guard).

4. Architecture guardrail
- Add a hard-rule test in `tests/architecture/test_hard_rules.py` to forbid `siftd.serialization` imports from `src/siftd/api/**` (with existing suppression pattern if ever needed).
- Optional follow-up ratchet: remove `"serialization"` from `ALLOWED_DEPS["api"]` in `tests/architecture/test_imports.py` once clean.

## What to do about other `api -> serialization` imports

- None found outside `api/stats.py`.
- `serialization/conversations.py -> api.conversations` is acceptable one-way; no immediate action required.

## Validation plan

1. Focused tests:
- `pytest tests/test_stats_cache.py tests/test_serve_fmt.py tests/test_narrative.py tests/architecture/test_hard_rules.py tests/architecture/test_imports.py`

2. Full gate:
- `./dev check`

## Expected outcome

- Cycle removed (`api.stats` no longer imports serialization).
- Serialization contract stability remains enforced by dataclass anti-drift tests.
- New architecture test prevents regressions of `api -> serialization` direction inversion.
