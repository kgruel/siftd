---
description: Scaffold a new siftd doctor health check (module, registration, tests, docs)
argument-hint: <check-name> — what it should detect
---

Create a new siftd doctor check: $ARGUMENTS

Follow the full workflow — do not skip steps:

1. Read `src/siftd/doctor/README.md` and two existing checks under
   `src/siftd/doctor/checks/` (one fast, one slow — note the `cost` field and
   how slow-lane checks share discovery via `CheckContext`).
2. Implement the check module under `src/siftd/doctor/checks/`: subclass the
   Check contract (name, description, cost, `run()`), with the docstring on
   the class (it feeds the generated check table). Register it in
   `BUILTIN_CHECKS` (`src/siftd/doctor/checks/__init__.py`).
3. Add tests mirroring an existing check's tests (look for the check's name
   under `tests/`), covering: healthy state, the condition it detects, and —
   if it reads config or the DB — the empty/missing case.
4. Run `./dev docs` (regenerates the doctor-checks table), then `./dev check`.
   Both must be green.
5. Dogfood: `uv run siftd doctor` and confirm the check appears and reports
   sensibly against the live environment.
