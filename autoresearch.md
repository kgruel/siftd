# Autoresearch: Adapter Layer Test Coverage Efficiency

## Objective
Optimize the **test coverage efficiency** of the `src/siftd/adapters/` package. The metric
rewards writing concise, fast tests that cover more source lines — encouraging clean
integration tests over bloated or trivial ones.

Tests are split per-adapter under `tests/adapters/` so each adapter can be independently
optimized without interference. The benchmark measures aggregate efficiency across all files.

## Metrics
- **Primary**: `efficiency` (lower is better) = `test_LOC × test_time_s / covered_lines`
- **Secondary**:
  - `coverage_pct` — percentage of adapter lines covered (higher = better, watch for progress)
  - `test_time_s` — seconds to run adapter tests (lower = better, watch for regression)
  - `test_loc` — lines of test code (lower per coverage = better)
  - `covered_lines` — absolute count of covered source lines (higher = better)

## Keep/Discard Rules
Two zones based on coverage percentage:

- **Normal zone** (<90% coverage): efficiency must improve to keep
- **Edge zone** (≥90% coverage): keep if coverage improved ≥1% even if efficiency
  regressed up to 25%. This avoids penalizing tests that close hard-to-reach
  edge-case gaps — the lines that matter most.
- **Always discard**: efficiency regressed AND coverage didn't improve

## How to Run
`./autoresearch.sh` — runs median-of-5 timing, outputs `METRIC name=number` lines.

## Scope
Coverage is measured over adapter source files (excluding __init__.py and template.py):

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

## Test Structure
```
tests/adapters/
├── test_infra.py        — validation, registry, SDK (shared adapter infra)
├── test_claude_code.py  — Claude Code adapter
├── test_codex_cli.py    — Codex CLI adapter
├── test_gemini_cli.py   — Gemini CLI adapter
├── test_aider.py        — Aider adapter
├── test_vscode.py       — VS Code adapter
├── test_pi_agent.py     — Pi Agent adapter
├── test_opencode.py     — OpenCode adapter
└── test_copilot_cli.py  — Copilot CLI adapter
```

## Files in Scope (may modify)
- `tests/adapters/test_*.py` — per-adapter test files (extend and optimize)
- `tests/conftest.py` — shared fixtures (may add adapter-specific fixtures)

## Off Limits (must NOT modify)
- All source files under `src/siftd/` — we're testing, not changing the implementation
- Other test files outside `tests/adapters/` — don't break existing tests
- No new external dependencies

## Constraints
- All existing tests must still pass (`./dev test`)
- Tests must have meaningful assertions (no `assert True` padding)
- Each test function must contain at least one `assert` statement
- Tests should exercise real behavior through the public API, not mock internals

## What's Been Tried
- LOC compression (merged assertions, removed redundant imports): 567→502 LOC
- Normalizer tests for copilot/pi_agent: +47 coverage at zero time cost
- Aider analytics path + vscode error path tests: +17 coverage
- Median-of-5 timing to stabilize time measurement (~8% variance vs ~40% before)
- Split monolith test_adapters.py → per-adapter files under tests/adapters/
