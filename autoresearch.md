# Autoresearch: Adapter Layer Test Coverage Efficiency

## Objective
Optimize the **test coverage efficiency** of the `src/siftd/adapters/` package. The metric
rewards writing concise, fast tests that cover more source lines — encouraging clean
integration tests over bloated or trivial ones.

The adapter layer (1,721 stmts after excluding __init__.py and template.py) sits at ~16%
coverage with ~704 LOC of tests (`tests/test_adapters.py`). The goal is to lower the
composite metric by: increasing covered source lines, reducing test LOC bloat, and keeping
test execution fast.

## Metrics
- **Primary**: `efficiency` (lower is better) = `test_LOC × test_time_s / covered_lines`
- **Secondary**:
  - `coverage_pct` — percentage of adapter lines covered (higher = better, watch for progress)
  - `test_time_s` — seconds to run adapter tests (lower = better, watch for regression)
  - `test_loc` — lines of test code (lower per coverage = better)
  - `covered_lines` — absolute count of covered source lines (higher = better)

## How to Run
`./autoresearch.sh` — outputs `METRIC name=number` lines.

## Scope
Coverage is measured over these adapter files (excluding __init__.py and template.py):

- `src/siftd/adapters/_jsonl.py` (21 stmts) — JSONL helper utilities
- `src/siftd/adapters/aider.py` (158 stmts) — Aider log parser
- `src/siftd/adapters/claude_code.py` (149 stmts) — Claude Code JSONL parser
- `src/siftd/adapters/codex_cli.py` (194 stmts) — Codex CLI session parser
- `src/siftd/adapters/copilot_cli.py` (132 stmts) — Copilot CLI parser
- `src/siftd/adapters/gemini_cli.py` (158 stmts) — Gemini CLI parser
- `src/siftd/adapters/opencode.py` (174 stmts) — OpenCode SQLite parser
- `src/siftd/adapters/pi_agent.py` (154 stmts) — Pi Agent JSONL parser
- `src/siftd/adapters/registry.py` (35 stmts) — Adapter discovery and loading
- `src/siftd/adapters/sdk.py` (325 stmts) — Shared SDK utilities for adapters
- `src/siftd/adapters/validation.py` (26 stmts) — Adapter validation helpers
- `src/siftd/adapters/vscode.py` (195 stmts) — VS Code chat history parser

Total: ~1,721 statements

## Files in Scope (may modify)
- `tests/test_adapters.py` — adapter tests (extend and optimize)
- `tests/conftest.py` — shared fixtures (may add adapter-specific fixtures)

## Off Limits (must NOT modify)
- All source files under `src/siftd/` — we're testing, not changing the implementation
- Other test files — don't break existing tests
- No new external dependencies

## Constraints
- All existing tests must still pass (`./dev test`)
- Tests must have meaningful assertions (no `assert True` padding)
- Each test function must contain at least one `assert` statement
- Tests should exercise real behavior through the public API, not mock internals
- Coverage is measured with `--include` to target only adapter files (excluding __init__.py
  and template.py)

## Key Patterns from Existing Tests
- Adapter tests create fixture data inline (JSON strings, fake file structures)
- `can_handle()` tests verify path matching
- `parse()` tests verify full conversation extraction
- SDK tests use `tmp_path` for file-based fixtures
- Each adapter class gets its own `Test*Adapter` class

## What's Been Tried
(none yet — restarting fresh after refactoring adapter code for testability)
