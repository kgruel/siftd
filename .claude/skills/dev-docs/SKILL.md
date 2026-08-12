---
name: dev-docs
description: How siftd's developer docs work — per-folder READMEs with generated spans, docs/reference, the drift gate, and where a new fact belongs. Use when ./dev docs --check fails, when adding or renaming modules/tests/scripts/adapters/doctor checks, when adding a managed README or section kind, or when deciding where to document something.
---

# siftd dev-docs system

One idea: hand-authored doc shells with the spans that are *copies of code
facts* machine-generated between markers, gated so drift fails the build.
Design: `docs/dev/design/dev-docs-system-2026-07-14.md`.

## Where a fact belongs

| Load time | Home |
|---|---|
| Every session (agents) | root `CLAUDE.md` — keep it tight |
| Entering a folder | that folder's `README.md` authored preamble |
| Derivable from code | a generated span (never hand-written twice) |
| Narrative / mental model | `docs/concepts/`, `docs/guides/` |
| Full API/CLI/schema/config reference | `docs/reference/` (whole-file generated) |
| Trigger-moment procedure | `.claude/skills/` |
| Design decisions | `docs/dev/` (gitignored; `git add -f` for ratified designs) |

## The contract

- Managed READMEs are listed in `MANIFEST` in `scripts/gen_docs.py` —
  explicit ownership, no recursive discovery. Every `src/siftd/` subpackage
  plus `src/siftd/`, `tests/`, `scripts/`.
- Generated spans sit between `<!-- gen:begin <id> -->` and `<!-- gen:end -->`.
  **Never hand-edit inside markers** — `./dev docs` rewrites them. Authored
  prose outside markers is never touched by the generator.
- Section kinds: `modules` (docstring table), `tests` (per-dir rollup +
  per-file counts), `scripts` (`# DESC:` table), `adapters` (registry-derived),
  `doctor-checks` (registry-derived), `files` (non-Python inventories).
- `./dev docs` regenerates everything; `./dev docs --check` is strict (a
  skipped target — e.g. api.md without optional extras — is a failure, not
  a silent keep).

## Gate topology

1. Tracked pre-commit hook (`.githooks/pre-commit`, wired by `./dev setup`):
   lint, then regenerate; only `docs/reference/` (fully generated) is
   auto-staged. Managed READMEs mix authored prose with generated spans, so
   they are never auto-staged — stage their regenerated spans yourself.
2. `./dev check` runs `./dev docs --check` as its **last** step.
3. CI has a dedicated `docs` job (all extras installed so nothing skips).

If `--check` fails, the docs are already regenerated — it regenerates before it
diffs, so re-running `./dev docs` changes nothing. Review the diff and `git add`
it. If it fails because a docstring is missing or wrong, fix the source — never
the table.

## Extending

- **New subpackage** → add a `MANIFEST` entry, bootstrap the README with a
  real authored preamble + the marker pair, run `./dev docs`.
- **New section kind** → renderer function in `scripts/gen_docs.py` +
  idempotency/preservation coverage in `tests/test_gen_docs_readmes.py`.
  Only add a kind for facts that are genuine copies of code; editorial
  judgement (purpose columns, register, warnings) stays authored.
- **Preamble register**: written for someone about to work *inside* the
  folder — boundary, local invariants, pointers. Don't restate the generated
  tables or root CLAUDE.md.
