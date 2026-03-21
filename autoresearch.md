# Autoresearch: Storage Layer Test Coverage Efficiency

## Objective
Optimize the **test coverage efficiency** of the `src/siftd/storage/` package. The metric
rewards writing concise, fast tests that cover more source lines — encouraging clean
integration tests over bloated or trivial ones.

The storage layer (1,204 stmts after excluding embeddings/migrations) sits at ~19% coverage
with ~715 LOC of tests (`tests/test_blobs.py`). The goal is to lower the composite metric
by: increasing covered source lines, reducing test LOC bloat, and keeping test execution fast.

## Metrics
- **Primary**: `efficiency` (lower is better) = `test_LOC × test_time_s / covered_lines`
- **Secondary**:
  - `coverage_pct` — percentage of storage lines covered (higher = better, watch for progress)
  - `test_time_s` — seconds to run storage tests (lower = better, watch for regression)
  - `test_loc` — lines of test code (lower per coverage = better)
  - `covered_lines` — absolute count of covered source lines (higher = better)

## How to Run
`./autoresearch.sh` — outputs `METRIC name=number` lines.

## Scope
Coverage is measured over these storage files (excluding embeddings/migrations which can't
be tested without extra infra):

- `src/siftd/storage/blobs.py` (28 stmts) — content-addressable blob storage, ref counting
- `src/siftd/storage/conversation_stats.py` (24 stmts) — materialized stats rebuild
- `src/siftd/storage/filters.py` (69 stmts) — WhereBuilder for dynamic SQL conditions
- `src/siftd/storage/fts.py` (65 stmts) — FTS5 full-text search operations
- `src/siftd/storage/queries.py` (185 stmts) — read queries (exchanges, stats, tags)
- `src/siftd/storage/sessions.py` (92 stmts) — live session tracking, pending tags
- `src/siftd/storage/sql_helpers.py` (40 stmts) — SQL utility functions
- `src/siftd/storage/sqlite.py` (463 stmts) — connection, migrations, vocabulary, inserts, store_conversation
- `src/siftd/storage/tags.py` (133 stmts) — tag CRUD, shell command tagging, derivative detection
- `src/siftd/storage/tool_search.py` (102 stmts) — tool search projection and FTS index

Total: ~1,204 statements (excluding `__init__.py`, `embeddings.py`, `migrate_blobs.py`, `migrate_workspaces.py`)

## Files in Scope (may modify)
- `tests/test_storage.py` — **NEW** focused storage layer tests (create and extend)
- `tests/test_blobs.py` — existing blob storage tests (may refactor for efficiency)
- `tests/conftest.py` — shared fixtures (may add storage-specific fixtures)

## Off Limits (must NOT modify)
- All source files under `src/siftd/` — we're testing, not changing the implementation
- `src/siftd/storage/schema.sql` — database schema
- Other test files — don't break existing tests
- No new external dependencies

## Constraints
- All existing tests must still pass (`./dev test`)
- Tests must have meaningful assertions (no `assert True` padding)
- Each test function must contain at least one `assert` statement
- Tests should exercise real behavior through the public API, not mock internals
- Coverage is measured with `--source=src/siftd/storage` and `--include` to exclude
  embeddings/migration files

## Key Patterns from Existing Tests
- `open_database(tmp_path / "test.db")` creates a fresh DB with full schema
- `conftest.py` has `make_db()` and `make_conversation()` helpers
- Functions use `conn: sqlite3.Connection` with `commit=False` default
- Tests close connections explicitly
- The `test_db` fixture provides 2 conversations with prompts/responses

## What's Been Tried
(none yet — starting fresh)
