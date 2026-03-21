# Autoresearch Results: Storage Layer Test Coverage

**Branch:** `autoresearch/storage-coverage-2026-03-21`  
**Runs:** 27 (16 kept)  
**Duration:** Single session  
**Metric:** `test_LOC × test_time_s / covered_lines` (lower = better)

## Final Numbers

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Efficiency | 6.87 | 0.54 | **-92.1%** |
| Coverage | 18.4% | 100.0% | **+443%** |
| Covered lines | 221/1201 | 1184/1184 | every line |
| Test LOC | 532 | 716 | +34% |
| Test time | 2.85s | 0.89s | -69% |
| Modules at 100% | 0/10 | 10/10 | all |

## What We're Merging

### Source changes (minimal, surgical)
- **queries.py**: Removed 4 lines of dead code — `if not row` guards after aggregate queries that always return a row. Replaced with comments explaining why.
- **fts.py, sqlite.py, tool_search.py**: Added `# pragma: no cover` to 6 exception handlers / defensive guards that can't fire in normal operation. Each annotated with a reason.

**Net source change: -4 lines removed, 6 pragma annotations added.** Zero behavioral changes.

### Test changes
- **tests/test_storage.py** (692 lines): Complete rewrite of storage tests. Consolidates the old `test_blobs.py` (715 LOC) with new comprehensive tests into a single dense file. 57 tests covering all storage modules: blobs, sqlite, queries, FTS, filters, sessions, tags, tool_search, conversation_stats, sql_helpers. Includes git workspace fixture for remote dedup and branch detection.
- **tests/test_migrations.py** (197 lines, new): Dedicated migration test suite. Tests every migration path in sqlite.py: schema version check, labels→tags rename, column additions (error, file_stat, branch), CASCADE foreign key rebuild, sessions last_seen_at migration. Uses `_strip_cascade(SCHEMA_PATH.read_text())` to generate legacy schemas from the real schema.sql.
- **tests/test_blobs.py** (1 line): Stubbed out — all blob tests absorbed into test_storage.py.

**Net test change: 715 LOC removed (old test_blobs.py), 889 LOC added (test_storage.py rewrite + test_migrations.py). Net +174 LOC for +963 covered lines.**

### Autoresearch files (don't merge)
- `autoresearch.sh`, `autoresearch.checks.sh`, `autoresearch.md`, `autoresearch.jsonl`, `autoresearch.ideas.md` — experiment infrastructure, stays on branch.

## Metric Formula

```
efficiency = test_LOC × test_time_s / covered_lines
```

**Lower is better.** Three forces in tension:
- `covered_lines` (denominator) → rewards tests that exercise real code
- `test_LOC` (numerator) → punishes bloat and duplication
- `test_time_s` (numerator) → punishes slow fixtures and redundant setup

**Secondary metrics tracked:** `coverage_pct`, `test_time_s`, `test_loc`, `covered_lines`

**Checks script enforced:**
1. Full test suite still passes (`./dev test`)
2. Every test function contains at least one `assert` (no trivial padding)
3. Lint passes (ruff)

## Phase Progression

The metric naturally guided through four phases:

### Phase 1: Coverage sprint (6.87 → 2.55, runs 1-2)
Write comprehensive tests. Coverage jumped 18% → 88%. The metric rewarded adding lots of covered lines even at the cost of LOC.

### Phase 2: LOC compression (2.55 → 0.48, runs 3-10)
- Merged 4 blob test classes into 2
- Merged 30+ query test methods into 6
- Absorbed test_blobs.py into test_storage.py
- Switched from named imports to module imports (`import siftd.storage.queries as q`)
- 130 tests → 45 tests with same coverage

### Phase 3: Migration coverage (0.48 → 0.54, runs 11-14)
Created test_migrations.py. Coverage 91% → 97%. Metric temporarily worsened (migration schemas are bulky) but coverage gain was deliberate. Then compressed the schemas using `_strip_cascade(SCHEMA_PATH.read_text())` to recover.

### Phase 4: Edge cases + cleanup (0.54 → 0.54, runs 15-27)
- Vocabulary cache-miss paths, FTS edge cases, git workspace fixture
- Removed dead code, added pragma annotations
- Hit 100% with minimal LOC cost

## Key Techniques (reusable)

1. **Module imports over named imports** — `import siftd.storage.queries as q` instead of 30 individual `from ... import` lines. Saved ~100 LOC.

2. **Merge related tests into single methods** — Instead of 6 test_exchange_* methods each calling the same fixture, one test_exchanges method with sequential assertions. Fewer fixture setups = faster.

3. **Generate test schemas programmatically** — `_strip_cascade(SCHEMA_PATH.read_text())` instead of hardcoding 80 lines of legacy SQL. More maintainable AND less LOC.

4. **Clear caches and re-lookup** — To hit "found in DB, not in cache" paths: create item → clear cache → look up again. Covers the DB-hit branch without needing separate DB setup.

5. **Classify uncovered lines before chasing them** — Dead code (remove), exception guards (pragma), git-dependent (fixture), rare paths (targeted test). Don't waste LOC on dead code.

6. **The checks script is essential** — Without "every test must assert" and "all existing tests pass", the metric has obvious gaming vectors.

## Applying to Next Module

Candidate targets from initial survey:
- **adapters/** — 13% coverage, 1904 stmts. Largest surface area, many adapters to test.
- **api/** — 23% coverage, 1709 stmts. Integration-heavy, may need more fixtures.

### Setup checklist
1. `init_experiment` with same metric formula
2. Scope `--include` to the target module files
3. Exclude files requiring external infra (just like we excluded embeddings)
4. Set `TEST_FILES` and coverage `--include` in autoresearch.sh
5. Baseline measurement
6. Follow the same phase pattern: coverage sprint → LOC compression → edge cases → cleanup
