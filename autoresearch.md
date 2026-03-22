# Autoresearch: Output Layer Test Coverage

## Objective
Optimize **test coverage** of the `src/siftd/output/` package using a composite metric
that naturally balances coverage improvement against test efficiency — no manual zone
rules needed.

## Metrics
- **Primary**: `score` (lower is better) = `miss_lines + (test_LOC × test_time_s / covered_lines)`
  - First term (`miss_lines`) dominates during coverage push — each uncovered line adds 1.0
  - Second term (efficiency) takes over near 100% — rewards concise, fast tests
  - Natural phase transition: no edge zone rules needed
- **Secondary** (for monitoring):
  - `miss` — number of uncovered source lines
  - `coverage_pct` — percentage of output lines covered
  - `test_time_s` — seconds to run output tests
  - `test_loc` — lines of test code
  - `covered_lines` — absolute count of covered source lines
  - `efficiency` — raw LOC×time/covered (the old metric, for reference)

## Keep/Discard Rules
Simple: score improved → keep. Score worse → discard. No zones.

The composite metric inherently handles the coverage/efficiency tradeoff:
- Adding 20 LOC to cover 10 new miss lines: miss drops by 10, efficiency rises ~0.1 → net improvement
- Adding 20 LOC that covers nothing: miss unchanged, efficiency rises → net worse
- Compressing 50 LOC at 100% coverage: miss stays 0, efficiency drops → net improvement

## How to Run
`./autoresearch.sh` — runs median-of-5 timing, outputs `METRIC name=number` lines.

## Scope
Coverage is measured over output source files (excluding empty modules):

- `src/siftd/output/common.py` (115 stmts) — Format helpers, table formatting, refs display
- `src/siftd/output/format_registry.py` (52 stmts) — Format discovery and selection
- `src/siftd/output/json_fmt.py` (64 stmts) — JSON output format
- `src/siftd/output/markdown_fmt.py` (171 stmts) — Markdown output format
- `src/siftd/output/narrative.py` (46 stmts) — Markdown narrative emitter
- `src/siftd/output/painted_bridge.py` (599 stmts) — Tool presenters, narrative rendering
- `src/siftd/output/terminal_fmt.py` (121 stmts) — Terminal output format
- `src/siftd/output/theme.py` (39 stmts) — Domain styles
- `src/siftd/output/validation.py` (13 stmts) — Formatter validation

Total: ~1,220 statements

## Test Structure
```
tests/
├── test_output_common.py    — common.py helpers (fmt_timestamp, etc.)
├── test_output_formats.py   — render_list across all formats, format_table, fidelity
```

## Files in Scope (may modify)
- `tests/test_output_common.py` — extend with more common.py coverage
- `tests/test_output_formats.py` — extend with render_detail, render_search, narrative
- New test files under `tests/` for output — may create per-module test files

## Off Limits (must NOT modify)
- All source files under `src/siftd/` — we're testing, not changing the implementation
- Other test files not related to output — don't break existing tests
- No new external dependencies

## Constraints
- All existing tests must still pass (`./dev test`)
- Tests must have meaningful assertions (no `assert True` padding)
- Each test function must contain at least one `assert` statement
- Tests should exercise real behavior through the public API, not mock internals
