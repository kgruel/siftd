# Using Strata During a Release Review (vs a Standard Review)

This document describes what it felt like to have `strata` available while reviewing `strata` for release readiness, and how that compares to a “standard” review where the only artifacts are code, tests, issues, and commit history.

## The baseline: what a “standard” review looks like

Without a tool like `strata`, a release review is usually constrained to:

- **Static artifacts:** README/docs, code structure, test suite, changelog, build metadata.
- **Version-control artifacts:** commit history (if clean), PR discussions (if present), issues.
- **Inference:** reviewers reconstruct rationale by reading the code and guessing intent.
- **Human dependency:** if the “why” isn’t written down, you ask the maintainer.

This baseline works, but it has predictable failure modes:

- Design intent is *implicit* (or missing), so reviewers over-focus on surface style or infer the wrong invariants.
- “Why is this here?” takes a long time to answer, even if the answer is simple.
- You can confirm *what* the system does, but not confidently confirm *why* it does it that way.

## What changes when `strata` is available

`strata` adds a new class of artifact to the review: **the decision trail** as it happened.

Instead of just reading the final state (code + docs), I could:

- Retrieve the *origin* of architectural constraints (“storage is SQLite and not pluggable”) and see how they were justified.
- Confirm that docs match the evolution of the system (and catch mismatches early).
- Observe real usage patterns from tool calls (what was actually run, how often, what workflows emerged).
- Tag key conversations as “review findings” so the review itself becomes a reusable artifact.

In practice, this changes the review from:

> “Does this code look reasonable?”

to:

> “Does this implementation match its stated philosophy and constraints?”

That’s a more meaningful question for release readiness.

## Concrete differences (task-by-task)

### 1) Recovering architectural intent

**Standard:** search for comments, read docs, inspect code shape, infer boundaries.

**With strata:** ask directly for the intent and retrieve the thread:

- `strata ask "adapters own parsing" --thread`
- `strata ask "storage not pluggable" --thread`

This compresses “architecture archaeology” dramatically. It also reduces the risk of reviewer misinterpretation: you can see the boundary being explicitly stated, argued, and re-stated.

### 2) Checking philosophical adherence (not just correctness)

**Standard:** a reviewer can say “this is clean” or “this is complex”, but it’s hard to judge whether complexity is *aligned* with the project’s stated philosophy.

**With strata:** the philosophy is queryable:

- `strata tags --prefix principles:`
- `strata query -l principles:design`
- `strata ask -l principles:design "manual first" --full`

This is especially valuable for `strata`, where the philosophy (“manual-first, automate later”) is a design constraint that should be visible in UX and architecture.

### 3) Understanding what “real usage” looks like

**Standard:** you guess. Maybe you read a few examples in docs.

**With strata:** you can quantify:

- tool-call categories via `strata tools --by-workspace`
- workflows via `strata query --tool-tag shell:test` / `shell:vcs`
- repeated usage of specific CLI subcommands (by querying `shell.execute` inputs)

This helps a reviewer distinguish between:

- “This feature is theoretical” vs
- “This feature is actively used and therefore should be polished.”

### 4) Catching doc/CLI mismatches

**Standard:** you manually compare docs and behavior; it’s easy to miss.

**With strata:** you naturally exercise the CLI while researching and reviewing, so mismatches show up as friction immediately.

This creates a useful review loop: the tool itself becomes part of the review harness.

## A “review playbook” that emerged

If I were to formalize the workflow I used, it would look like this:

1. **Scope**
   - `strata query -w <repo-substring> -n 20 -v`
2. **Principles**
   - `strata tags --prefix principles:`
   - `strata query -l principles:architecture -v`
3. **Rationale threads**
   - `strata ask --thread "why we chose X"`
   - `strata ask --first "topic"` when you want the earliest origin
4. **Cross-check docs**
   - `strata ask --refs README.md "topic"` to tie rationale to concrete text/code
5. **Operational health**
   - `strata doctor`
6. **Preserve the review**
   - `strata tag <id> review:<topic>`

The key thing: *tag the review itself*. This turns the review into future retrieval material.

## How it compares overall

### What improves dramatically

- **Time-to-context:** the “why” is recoverable in seconds, not hours.
- **Confidence:** reviewers can evaluate against explicit constraints instead of inferred ones.
- **Continuity:** the review can be tagged and becomes part of the institutional memory.

### What stays the same

You still need the standard checks:

- tests passing
- packaging and build metadata correct
- docs accurate and complete
- ergonomics evaluated with real hands-on usage

`strata` doesn’t replace code review; it makes the *conceptual* side of code review much higher fidelity.

### Tradeoffs / new risks

- **Bias toward logged discussions:** if something wasn’t discussed in logs, it can be invisible in this channel.
- **Noise:** conversational artifacts include dead ends; reviewers must still filter.
- **Privacy and scope:** conversation history can contain sensitive material; reviewers need to respect the boundary between “project docs” and “personal corpus”.
- **Operational dependency:** embeddings require an index; health checks can fail if the environment is constrained (read-only media, sandboxed file system, etc.).

## The “meta” benefit for a tool like strata

Because `strata` is itself a tool for recovering rationale and workflow patterns, it creates a tight feedback loop:

1. Use `strata` during development.
2. Use it again during release review.
3. The friction you hit is *real*, and it’s captured in the same system.
4. Fixes can be grounded in observed workflows, not hypotheticals.

This is a strong product-development advantage and aligns with the project’s philosophy of collapsing loops and reducing context loss.

