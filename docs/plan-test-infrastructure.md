# Plan: Test Infrastructure for CLI Coverage

## Problem

The project has 79% overall coverage (14,667 stmts, 3,048 miss). The miss is
concentrated in two areas:

| Layer | Miss | % of total miss | Current coverage |
|-------|------|-----------------|------------------|
| **CLI modules** | 1,376 | **45%** | 62% average |
| Excluded (serve/embed/doctor) | 972 | 32% | behind markers |
| Everything else | 700 | 23% | 90%+ average |

The CLI layer is the coverage gap. Six modules sit below 60%: `cli_data` (56%),
`cli_db` (59%), `cli_search` (62%), `cli_peek` (62%), `cli_install` (49%),
`cli_meta` (53%). These contain user-facing error messages, output formatting,
and argument validation that can silently break during refactors.

The infrastructure to test them already exists — `main()` accepts argv,
`test_db` creates a real SQLite DB, `capsys` captures output. The pattern works
well in `test_cli_tags.py` (84% coverage with 300 LOC of tests). It just hasn't
been applied consistently.

## What's blocking further progress

### 1. CLI modules are flat files, not a package

16 files at `src/siftd/cli_*.py` totaling 235KB. No shared namespace, no
`__init__.py` to organize imports. Each is a grab bag of:

- `cmd_*()` handler functions (argparse → DB → print)
- `_helper()` pure functions (formatting, aggregation, validation)
- `build_*_parser()` argparse wiring
- Renderer classes (`_IngestTextRenderer`, `_IngestJsonRenderer`)

The helpers are testable without a DB. The handlers need one. But since
everything is in one flat file, coverage tools can't tell what's tested vs what's
unreachable dead code.

**Proposed change**: Move CLI to a package.

```
src/siftd/cli/
├── __init__.py          # main(), build_parser() — the dispatcher
├── _common.py           # resolve_db, fidelity_from_args, etc.
├── _filters.py          # FilterArgs, add_filter_args, extract_filter_args
├── data.py              # ingest, backfill, doctor commands
├── db.py                # db stats, db vacuum, db path, etc.
├── export.py
├── install.py
├── meta.py              # about, stats
├── peek.py
├── query.py
├── search.py
├── serve.py
├── sessions.py
├── tags.py
├── tool_search.py
└── upgrade.py
```

This is a rename, not a rewrite. Every `cli_foo.py` becomes `cli/foo.py`. The
public API (`from siftd.cli import main`) doesn't change. Internal imports shift
from `from siftd.cli_common import resolve_db` to `from siftd.cli._common
import resolve_db`.

**Why it matters for testing**: A package lets test files mirror the structure.
`tests/cli/test_data.py` tests `siftd.cli.data`. Coverage reports group by
package. Helpers can be extracted to `_common.py` and shared fixtures live in
`tests/cli/conftest.py`.

### 2. No shared CLI test fixtures

Every CLI test file re-discovers how to get a conversation ID:

```python
conn = open_database(test_db)
conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
conn.close()
```

This pattern appears 50+ times across test files. A `cli_db` fixture that
returns both the path and a known conversation ID would eliminate this
boilerplate:

```python
# tests/cli/conftest.py
@pytest.fixture
def cli_db(test_db):
    """test_db plus extracted IDs for direct reference."""
    conn = open_database(test_db)
    row = conn.execute(
        "SELECT id, external_id FROM conversations ORDER BY started_at LIMIT 1"
    ).fetchone()
    conn.close()
    return SimpleNamespace(
        path=test_db,
        conv_id=row["id"],
        external_id=row["external_id"],
        args=["--db", str(test_db)],
    )
```

Then tests become:

```python
def test_tag_apply(cli_db, capsys):
    assert main([*cli_db.args, "tag", cli_db.conv_id, "foo"]) == 0
    assert "Applied tag" in capsys.readouterr().out
```

### 3. Peek test files overlap

There are 5 files testing `siftd.peek`:

| File | LOC | Focus |
|------|-----|-------|
| `test_peek.py` | 960 | Integration: full adapter→scan→read flow |
| `test_peek_follow.py` | 515 | Follow + parse_record + tool hints |
| `test_scanner.py` | 177 | Scanner internals (unit) |
| `test_reader.py` | 252 | Reader internals (unit) |
| `test_follow.py` | 163 | Follow internals (unit) |

Combined coverage on `siftd.peek`: **96%** (28 miss out of 503 stmts).
Individually they're complementary — the old files test integration, the new
files test edge cases. But there's significant overlap in what they exercise
(both sets test `parse_record`, `read_session_detail`, etc.).

**Proposed change**: Consolidate into 3 files.

```
tests/
├── test_peek.py          # Integration tests (from old test_peek.py, trimmed)
├── test_peek_follow.py   # Follow-specific (threaded tests, tool hints)
├── test_peek_units.py    # Merged scanner+reader+follow unit tests
```

The old `test_scanner.py`, `test_reader.py`, `test_follow.py` merge into
`test_peek_units.py`. The old `test_peek.py` drops any tests that are now
redundant with the unit file. Goal: same 96% coverage, fewer total LOC, no
duplicate assertions.

### 4. `from_query_params` is dead code

`cli_filters.py` defines `from_query_params` as a nested function inside
`add_filter_args`. It has a `@classmethod` decorator but is never attached to
`FilterArgs` and is never called anywhere. The serve routes build filter params
differently. This should be either:

- Deleted (if truly unused), or
- Moved to `FilterArgs.from_query_params()` as a proper classmethod and used by
  serve routes

### 5. `test_stats_cache` dominates test runtime

One test (`test_ingest_creates_cache`) takes 73 seconds — longer than the rest
of the suite combined. It runs a full `main(["ingest"])` to verify cache
creation. Options:

- **Mark it `@pytest.mark.slow`** and exclude from default runs
- **Mock the ingest** and test only the cache read/write path (the 7 other tests
  in the file already do this and run in <1s)
- **Move it to an integration marker** alongside embeddings/serve tests

## Execution order

### Phase 1: Quick wins (no restructuring)

1. **Delete `from_query_params` dead code** from `cli_filters.py`
2. **Mark `test_ingest_creates_cache` as slow** or refactor to mock ingest
3. **Extract `_aggregate_conversations` and `_compute_thread_tiers`** from
   `cli_search.py` into unit tests — these are pure functions, 6+6 miss lines,
   no DB needed

### Phase 2: CLI package migration

1. Create `src/siftd/cli/` package, move files with `git mv`
2. Update all internal imports (mechanical — `cli_foo` → `cli.foo`)
3. Keep `from siftd.cli import main` working (re-export from `__init__.py`)
4. Create `tests/cli/conftest.py` with `cli_db` fixture
5. Move existing `test_cli_*.py` into `tests/cli/`

### Phase 3: CLI coverage push

With the package structure and shared fixtures in place, write tests for:

| Module | Current | Target | Miss to cover | Strategy |
|--------|---------|--------|---------------|----------|
| `cli/data.py` | 56% | 80% | ~160 lines | doctor subcommands, ingest error paths |
| `cli/db.py` | 59% | 80% | ~85 lines | db stats, db vacuum, db export |
| `cli/query.py` | 66% | 85% | ~55 lines | detail view, SQL mode, filters |
| `cli/search.py` | 62% | 75% | ~50 lines | pure helpers + FTS-only path |
| `cli/meta.py` | 53% | 75% | ~50 lines | about, stats rendering |
| `cli/install.py` | 49% | 65% | ~50 lines | error paths, already-installed |

Conservative targets. The remaining miss after this will be serve delegation
paths, embeddings integration, and platform-specific error handling.

### Phase 4: Peek consolidation

1. Merge `test_scanner.py` + `test_reader.py` + `test_follow.py` into
   `test_peek_units.py`
2. Audit `test_peek.py` for tests now redundant with unit file
3. Remove duplicates, verify coverage unchanged

## What NOT to do

- **Don't test SSH/HTTP transport in `api/sync.py`** — 167 miss lines that
  require a real SSH server or extensive mocking. Not worth the maintenance cost.
- **Don't chase `serve/` or `embeddings/` coverage** — these are behind test
  markers for good reason (require running servers or large model downloads).
- **Don't test `adapters/template.py`** — it's sample code (0% coverage, 86
  lines), not production code.
- **Don't test `doctor/view.py`** — 158 miss, 0% coverage, but it's a
  terminal-specific rendering module that depends on `painted` runtime state.

## Expected outcome

| Metric | Before | After Phase 3 |
|--------|--------|---------------|
| Overall coverage | 79% | ~83% |
| CLI coverage | 62% | ~77% |
| Miss lines | 3,048 | ~2,600 |
| Test count | 1,720 | ~1,850 |
| Test runtime | ~103s | ~40s (with slow marker) |
