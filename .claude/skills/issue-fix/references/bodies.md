# PR and issue bodies

Read this when writing a PR body, an issue, or re-syncing either after review.

## The schema

**Four fixed headings, ~400 words.** Same words every time — improvised
headings invite improvised content, which is where the length comes from.

```markdown
Fixes #N. <one line of context if the PR follows another.>

## Defect     What broke and the verified mechanism. Point at the docstring for
              the full explanation; do not retell it.
## Scope      What actually changed (corrected against the tree), what dissolved
              with it, and any user-visible trade, stated plainly.
## Evidence   Falsification list, measurements, `./dev check` state. Tables and
              lists, not prose.
## Deferred   Filed issue numbers, one line each.
```

Issues use the same four, one renamed: **Defect** · **Scope** (with counts —
sites, occurrences, callers) · **Why now** (or why deferred, if filed from a
PR's Deferred section) · **Shape of the fix**. Same budget.

## Why the budget holds

**Rationale belongs in the durable artifact.** Mechanism goes in the docstring
or the folder README, and the PR links to it. Otherwise the same paragraph gets
hand-written into the issue, the PR, the commit message, and the docstring —
the `/simplify` pass on #42 flagged exactly that duplication *inside* the code
while the same arc's prose was doing it across four artifacts.

A merged PR body is **write-only**: nobody reads it again, so length spent
there is length spent nowhere. That is also why the Deferred section is issue
numbers rather than descriptions.

The budget is empirical, not a preference. PRs #41/#44/#46 ran 886–1399 words
across 8–9 improvised sections; issues written to this schema landed at ~450
words in 4–5 without effort. Rewriting #46 to the schema came to **361 words**
and lost nothing — every measured number and the whole falsification list
survived. What was cut was narrative retelling of a mechanism that already
lives in the code.

## End state, not journey

**The body describes what ships, not how it got there.** Review provenance —
what `/simplify` and `codex` caught, which round, what you tried first — goes
in a PR *comment*, where it is timestamped, append-only, and sits next to the
review it came from. It is durable in the fix commits besides.

The split is not "move the section", and getting it wrong loses real content:
when review changes the *design*, that change is a property of the fix and must
be **integrated** into Defect/Scope/Evidence. Only the story of finding it
becomes a comment.

From #46:

- *"The fallback silently dropped committed `-wal` content, so it now refuses"*
  → **Scope**. It is what ships.
- *"codex found it on round one, and the guard that fixed it was too blunt
  until it tested the journal's magic"* → **comment**. It is how it was found.

## Re-syncing after review

If review changed the design, the body still argues for the design you removed.
Rewrite Defect/Scope/Evidence to describe what now ships, then post what review
caught as a comment. This is the step most easily forgotten, and it stays easy
to forget precisely because nothing fails when you skip it.

## Note

`gh pr create --body` bypasses `.github/PULL_REQUEST_TEMPLATE.md` entirely, so
a template file would not constrain this path. The skill is the enforcement
point.
