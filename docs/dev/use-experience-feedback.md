# Strata Use Experience — UX Feedback

This document is product/UX feedback from using `strata` as a CLI tool while preparing `strata` itself for release. It focuses on ergonomics, usefulness, friction points, and “what I reached for” in real usage.

## Summary

`strata` is unusually effective at closing the loop between “I once decided this” and “I need that decision now”. The core UX wins come from:

- A clean mental model (**ingest → store → query**) that maps to the command surface.
- A practical split between **exact-ish retrieval** (`query` / FTS5 / SQL) and **semantic retrieval** (`ask` / embeddings).
- A lightweight synthesis layer (**tags**) that lets you turn raw history into curated memory without adding new schema.
- Progressive disclosure that encourages the right workflows (search → drill down → tag → retrieve).

The highest-impact UX pitfalls I hit were about *operational friction* (read-only DB behavior, embeddings DB sidecar writes) and *sharp edges in query syntax* (FTS5 parsing surprises). Both are fixable with small “make the safe thing easy” changes.

## The mental model that “clicks”

If you hand a new user the tool, the model that works is:

1. **Ingest**: “Collect and normalize everything my tools already logged.”
2. **Query**: “Browse / filter / drill down the normalized log.”
3. **Ask**: “When I don’t remember the exact words, search by meaning.”
4. **Tag**: “Mark what matters so future me (or an agent) can retrieve it instantly.”
5. **Repeat**: “Over time, tags become the durable layer.”

The command layout mostly reinforces this model:

- `strata ingest` establishes the corpus.
- `strata status` gives confidence and a sense of scale.
- `strata query` is the deterministic browser (filters, SQL files, drill-down).
- `strata ask` is the semantic layer (index + search + multiple output modes).
- `strata tag` / `strata tags` are the synthesis loop.
- `strata tools` / `strata doctor` / `strata peek` are “ops visibility” support commands.

## What feels especially good

### 1) `ask` as the default “research” entrypoint

In practice, I reached for `ask` more than anything else. When you’re doing meta-work (review, design archaeology, “why did we do this”), the words you remember rarely match the words you originally used. Semantic retrieval is the right primitive.

The strongest UX features here:

- **Multiple reading modes** (`--thread`, `--context N`, `--full`, `-v`) let you start broad and progressively pay attention.
- **Filters compose** (`-w`, tag filters, date filters, role filters).
- **Results nudge toward preservation** (the “Tip: Tag useful results…” line is small but behavior-shaping).

### 2) Tag system is “lightweight enough to actually use”

Tags feel like the right level of friction:

- No hierarchies, no migrations, no special UI.
- Prefix matching (namespace wildcard via trailing `:`) is a very strong affordance.
- The boolean model (`OR` via repeated `-l`, `AND` via `--all-tags`, `NOT` via `--no-tag`) is powerful without demanding a full query language.

In practice: tags let you turn semantic retrieval into “curated memory” cheaply.

### 3) Tool-call tagging is a big unlock for workflow research

Auto-tagging `shell.execute` calls into `shell:*` categories is a great example of “manual first, automate when patterns emerge”:

- It’s derived, not authoritative (safe to backfill/rebuild).
- It provides immediate value: filtering and workflow analytics.
- It supports later “synthesis layer” ideas without requiring a big upfront taxonomy system.

### 4) SQL query files are the right “escape hatch”

Letting users drop `.sql` files into `~/.config/strata/queries/` and run them is a great design choice:

- It’s “data platform”, not “reporting app”.
- It avoids building a bespoke report language.
- It turns “I keep doing this analysis” into a reusable artifact.

## Friction points I hit (and what to do about them)

### 1) Read-only database behavior (high impact)

**Symptom:** running a read-only command against a DB that is not writable fails with errors like:

- “attempt to write a readonly database”
- “unable to open database file” (often from embeddings DB WAL/SHM behavior)

**Why it matters:** read commands should be safe in restricted environments (CI, containers, sandboxed shells, readonly mounts). This kind of friction is especially harmful because it looks like “the tool is fragile” even when the data is fine.

**Fix direction:** make “read-only open” first-class:

- Reads should not run migrations/ensures that write.
- Embeddings reads should avoid creating WAL/SHM files (SQLite `immutable=1` is useful here).
- If migrations are required, the CLI should give a specific “run X with write access” hint.

### 2) FTS5 query syntax surprises in `query -s` (medium impact)

**Symptom:** queries that look like plain text can be interpreted as FTS syntax. Example pattern: hyphenated strings can be parsed as operators, producing confusing errors.

**Why it matters:** users expect `-s "some words"` to “just search”. If it’s truly FTS5 syntax, users need guardrails:

- Better error messaging (“this is FTS5 syntax; try quoting terms”).
- Potentially a “literal” mode that escapes user text into safe FTS tokens.

### 3) Docs/CLI mismatches (medium impact)

Any mismatch between docs and help output has outsized trust cost. This is easy to miss when evolving quickly.

Practical mitigation:

- Keep `docs/cli.md` auto-generated from the in-repo CLI, not an installed copy.
- Use `doctor` to validate that “user-facing claims” remain true (especially plugin UX).

### 4) Configuration UX: keep it minimal, but frictionless (medium impact)

The current config scope is intentionally small (default `ask` formatter). That’s good.

What improves UX without growing scope:

- A discoverable “starter config” story (e.g., `strata config set ask.formatter thread` + an example snippet in docs).
- A `strata config` output that doesn’t require heavy dependencies and doesn’t break in minimal Python environments.

### 5) “Global flags must come first” is a common CLI footgun (low/medium)

`--db` is a global flag (must appear before subcommands). This is standard argparse behavior but still trips users.

Two options:

- Document it in examples (“global flags go before the command”), and/or
- Accept `--db` after subcommands via a custom argparse pattern (extra complexity).

## What I actually did during the review (a practical workflow)

This is the workflow that emerged naturally:

1. **Scope the corpus to this repo/workspace**
   - `strata query -w <workspace-substring> -n 20 -v`
2. **Find “principle” conversations**
   - `strata tags --prefix principles:`
   - `strata query -l principles:architecture -v`
3. **Use `ask` to pull rationale and evolution**
   - `strata ask -w <workspace-substring> --thread "storage not pluggable"`
   - `strata ask --first "MMR diversity"`
4. **Quantify actual usage**
   - `strata tools --by-workspace`
5. **Run health checks**
   - `strata doctor`
6. **Tag what matters**
   - `strata tag <id> research:<topic>`

This felt like “review with primary sources”: not just code and docs, but the decision record that produced them.

## Suggested UX improvements (future)

These are “nice to have” ideas that follow the existing philosophy and would improve the day-to-day feel:

- **Safer search default:** offer a literal FTS mode (escape/OR-rewrite) for `query -s` so users don’t need to know FTS5 syntax to get value.
- **First-class read-only mode:** expose `--readonly` (even if it’s mostly internal) so users can force “do not attempt to mutate anything”.
- **More drill-down symmetry:** now that `tags <name>` exists, consider mirroring drill-down for tool tags/workspace tags in similarly ergonomic ways.
- **Opinionated “review playbooks”:** ship a small built-in query pack for release reviews (costs, tool intensity, long conversations, ingestion errors), and document a recommended review workflow.

