# Dev-docs system: navigable READMEs, drift gate, skills — 2026-07-14

Status: RATIFIED 2026-07-14 (Fable, overnight arc; user review pending)
Advisory: codex gpt-5.6-sol (medium) consulted on the full design; painted's docs
system (tools/docgen.py, docs/dev/plans/2026-06-01-docs-system-design.md) studied
as prior art.

## Problem

siftd has one flat root CLAUDE.md and zero per-folder documentation. A developer
or agent entering `src/siftd/doctor/` or `tests/` learns the local conventions
only by grepping. `docs/reference/` is generated but its cli.md target silently
broke when `--help` moved to the lanes format, and nothing gates docs staleness
(`./dev docs --check` exists but is not in `./dev check`, CI, or pre-commit).
`.claude/skills/` is fully gitignored, so operational skills are single-machine.

## Shape (one idea)

Hand-authored README shells per folder, with the spans that are *copies of code
facts* machine-generated between markers, gated so drift fails the build.
Borrowed from painted: authored prose is never generated; only derivable facts
are. Native to siftd: the generator is a new target in the existing
`scripts/gen_docs.py` (whole-artifact reference generation stays as-is).

## Decisions

1. **`readmes` target in gen_docs.py.** A checked-in manifest (data structure in
   gen_docs.py) maps each managed README to its generated sections. No recursive
   README discovery — explicit ownership only. Sections are bounded by
   `<!-- gen:begin <id> -->` / `<!-- gen:end -->`; the generator rewrites only
   inside markers, is idempotent, and errors on malformed/unknown/nested markers.
   Each generated block carries a one-line provenance caption (source + regen
   command).
2. **Section kinds** (first slice): `modules` (module → first docstring line,
   as a table with relative links), `tests` (per-directory rollup + per-file
   table with test counts and docstring lines), `scripts` (`# DESC:` table).
   Registry-derived kinds (`adapters`, `doctor-checks`) where a real registry is
   authoritative — never "first class docstring" heuristics for doctor checks;
   read the registry.
3. **Coverage**: all 14 subpackages under src/siftd/ + src/siftd/README.md
   (the 16 loose "core" modules) + tests/README.md + scripts/README.md. Tiny
   packages get a one-paragraph preamble + small table; consistency beats
   curation here (user preference). tests/README.md leads with an authored
   responsibility map (area → purpose → lane/marker → command), then generated
   rollups and per-file tables.
4. **Test counts stay in and are gated.** Advisor recommended excluding volatile
   counts; user's founding example was exactly this table. Drift is confined to
   files you touched and the fix is one command (`./dev docs`). Revisit if the
   friction annoys in practice. (Flagged for morning review.)
5. **No CLAUDE.md symlinks** (divergence from painted). Auto-loaded context
   should be constraints, not inventories; a 200-row table charging every
   session is the wrong invariant. Root CLAUDE.md gains a "folder guides"
   ladder pointing at the READMEs for on-demand reading. Local real CLAUDE.md
   files can be added later where repeated agent mistakes demonstrate need.
6. **cli.md repair via parser introspection.** Build the parser
   (`_build_parser()`), enumerate public commands from `_LANES`/subparser
   actions after plumbing-hiding, capture each `--help` as today. Never parse
   rendered lanes text. A test asserts every public lane command has a section.
7. **Check mode is strict.** `./dev docs --check` must fail if a target skips
   (e.g. api.md import failure currently degrades to "keep committed file" —
   a false green under --check). Wire docs --check into `./dev check` (last
   step, cheapest blast radius) AND as an explicit ci.yml step/job with full
   extras (base lane may legitimately lack optional deps).
8. **Skills tracked** via narrow .gitignore exceptions (`!.claude/skills/**`,
   `!.claude/commands/**`; settings and the rest stay ignored). Track existing
   `release` (fixing its false "pre-commit regenerates docs" claim) and
   `subtask`. New: `dev-docs` skill (the docs-system contract: where facts
   live, marker rules, when to run ./dev docs, manifest location). Commands:
   only where they orchestrate (candidate: `/new-adapter` scaffold); zero is an
   acceptable answer.
9. **Out of scope**: the shelved MkDocs site rework (docs/rework worktree,
   site-IA axis — resume/supersede is a separate decision); painted's
   fragment-injection machinery (no ≥2-site duplicated facts in siftd yet);
   a dev plugin.

## Tests

- Marker engine: idempotency (second run = no diff), authored-prose
  preservation, unknown/malformed marker → error, manifest paths exist.
- cli.md: every public command gets a section.
- Strict check mode: a skipping target fails.

## Residue sweep

- release skill claim fixed; CLAUDE.md structure tree updated to mention folder
  guides; gen_docs.py docstring updated with the new target; docs.sh usage text.
