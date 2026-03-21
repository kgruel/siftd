# RFC: Unified Exception Handling for siftd

## Status: Phases 1-3 Complete

## Problem

siftd had **210 `except` clauses** spread across 48 files with no shared policy (now 195 after Phases 1-3). Each module independently decides:
- Which exceptions to catch (42 distinct exception tuples)
- How to recover (return None, continue, pass, log, re-raise, print)
- Whether to log (only 4 modules use `logging`)

This creates:
1. **Inconsistent resilience** — same operation handled differently in different modules
2. **Silent data loss** — most errors are swallowed with `pass` or `return None`
3. **Untestable handlers** — each try/except needs its own corrupt fixture
4. **No observability** — when siftd skips a file or record, nothing is logged

## Current State (post-safecall Phase 2)

`safecall.py` exists with 6 functions covering file I/O, JSON parsing, and timestamps. 6 adapters migrated. This RFC proposes extending the pattern to the full codebase.

## The 210 Exceptions, Categorized

### Category A: Parse-or-skip (≈85 clauses)
**Pattern:** Try to read/parse data. On failure, skip it and move on.
**Recovery:** `return None`, `return []`, `continue`, `pass`
**Where:** Adapters, peek/reader, config, api/conversations, backfill, git

```python
# Before (repeated 85× with slight variations)
try:
    data = json.loads(raw)
except (json.JSONDecodeError, TypeError):
    return None

# After
data = safecall.parse_json(raw)
```

**safecall functions needed:** `read_text`, `load_json`, `iter_jsonl`, `parse_json`, `parse_json_args`, `epoch_ms_to_iso` — **all exist today.**

### Category B: Try-or-degrade (≈35 clauses)
**Pattern:** Try an enhanced operation. On failure, fall back to basic mode.
**Recovery:** Use a default value, disable a feature, try alternative
**Where:** config (TOML parse), search (embeddings), CLI (optional features)

```python
# Before
try:
    config = tomlkit.loads(path.read_text())
except tomlkit.exceptions.TOMLKitError:
    config = {}

# After — stays as try/except, but with structured logging
config = safecall.load_toml(path)  # or keep inline, it's domain-specific
```

**These mostly stay as-is.** The exception tuples are domain-specific (tomlkit, httpx, ImportError). safecall shouldn't know about TOML or HTTP. But we can add `safecall.try_import()` for the 9× `ImportError` pattern.

### Category C: Fail-loudly (≈40 clauses)
**Pattern:** Catch and re-raise with a better error message, or print to stderr and exit.
**Recovery:** `raise CustomError(...)`, `sys.exit(1)`, `print(f"Error: {e}")`
**Where:** CLI modules, embeddings backends, api/sync

```python
# Before
except ValueError as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
```

**These should NOT use safecall.** They're doing the right thing — failing loudly at the boundary. The CLI is the right place to catch and display errors.

### Category D: Broad `except Exception` (≈36 clauses)
**Pattern:** Catch-all for plugin loading, orchestration loops, subprocess calls.
**Recovery:** Log warning, skip item, continue loop
**Where:** ingestion/orchestration, plugin_discovery, doctor/checks, sync

```python
# Before
try:
    adapter.parse(source)
except Exception as e:
    logger.warning("Adapter %s failed: %s", adapter.name, e)
    continue
```

**These stay as-is.** Broad catches at orchestration boundaries are correct — they prevent one bad adapter/plugin from crashing the whole pipeline. The improvement is adding structured logging.

### Category E: Infrastructure (≈14 clauses)
**Pattern:** SQLite errors, subprocess timeouts, file locks
**Recovery:** Varies — retry, skip, fallback
**Where:** storage/sqlite, storage/fts, cli_db

**These stay as-is.** SQLite error handling is already well-contained in the storage layer.

## Proposed Changes

### Phase 1: ✅ DONE — safecall.py foundation
- `read_text`, `load_json`, `iter_jsonl`, `parse_json`, `parse_json_args`, `epoch_ms_to_iso`
- 100% test coverage, registered in architecture layer

### Phase 2: ✅ DONE — Adapter migration
- 6 adapters migrated, -41 lines, coverage 92.5% → 93.4%

### Phase 3: ✅ DONE — Non-adapter Category A migration
Migrated 15 parse-or-skip handlers across 7 modules:

| Module | Clauses | safecall function | Lines saved |
|--------|---------|-------------------|-------------|
| api/conversations.py | 4 | `parse_json` | ~20 |
| backfill.py | 3 | `parse_json` | ~6 |
| api/file_refs.py | 2 | `parse_json` | ~5 |
| api/sync.py | 2 | `parse_json` | ~4 |
| git.py | 2 | `read_text` | ~2 |
| output/painted_bridge.py | 1 | `parse_json` | ~3 |
| peek/reader.py | 1 | `parse_json` | ~2 |
| **Total** | **15** | | **~31 lines** |

15 remaining JSONDecodeError catches are all correct to keep:
adapter internals (5), fail-loudly re-raises (3), CLI user-facing (4), streaming parsers (1), file loading with mixed errors (2).

### Phase 4: Deferred — Observability upgrade
Only ~10 truly silent broad catches remain (`except Exception: pass`).
Most orchestration catches already log, report findings, or record errors.
Low ROI for a dedicated `swallowed()` abstraction — better to add logging
inline to the 10 silent catches if/when they cause debugging pain.

### Phase 5: Deferred — `try_import` for optional dependencies
The 9 `ImportError` catches have diverse recovery actions (re-raise with
better message, return False, set flag, return []) that don't fit a single
`try_import()` pattern. Not worth abstracting.

## What We're NOT Changing

1. **CLI error display** (Category C) — `print(f"Error: {e}")` at CLI boundary is correct
2. **Broad catches at orchestration boundaries** (Category D) — `except Exception` in loops is correct
3. **SQLite/infrastructure handling** (Category E) — already well-contained
4. **Domain-specific exceptions** — tomlkit, httpx, jwt stay inline

## Migration Order

1. **Phase 3** first — biggest impact, least risk, uses existing safecall functions
2. **Phase 4** second — observability is valuable but doesn't change behavior
3. **Phase 5** last — nice-to-have, low impact

## Results

| Metric | Before | After Phases 1-3 |
|--------|--------|------------------|
| `except` clauses | 210 | 195 |
| Parse-or-skip with inline try/except | ~85 | ~55 |
| Lines of exception handling code | ~600 | ~530 |
| Exception behavior tested centrally | 0% | safecall.py 100% |
| Adapter lines removed | — | 72 (adapters: -41, non-adapters: -31) |

## Appendix: Full Exception Census

```
Category A (parse-or-skip):     ~85  → 30 migrated to safecall, ~55 remaining (correct)
Category B (try-or-degrade):    ~35  → kept inline (domain-specific: TOML, HTTP)
Category C (fail-loudly):       ~40  → kept as-is (CLI boundary, SyncError)
Category D (broad catch):       ~36  → kept, ~10 silent, rest already log/report
Category E (infrastructure):    ~14  → kept as-is (storage layer)
                                ───
                  Original: 210 → Current: 195
```
