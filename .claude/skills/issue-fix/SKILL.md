---
name: issue-fix
description: Take a GitHub issue from triage to merged PR — verify it against current main, fix on a branch, /simplify, external codex review, changelog, merge, then file what you deferred. Use when the user names a siftd issue number, says to fix or work an issue, or asks whether a reported issue is valid.
---

# siftd Issue → PR

The flow is: **triage → branch → fix → `/simplify` → `codex review` → merge →
file the deferrals.** Each stage has caught something the previous one missed.
Don't skip the external review because the internal one came back clean; they
find different classes of thing.

## 1. Triage — is it real, on *current* main?

Reports arrive against old versions. Verify before believing.

```bash
gh issue view <N> --repo kgruel/siftd --json number,title,state,body,comments
```

Plain `gh issue view` can print nothing in this environment — use `--json`.

Then **reproduce it yourself** against the working tree, and read every file
the report names. Two things to produce before writing any code:

- The failure, triggered locally, ideally matching the reporter's error text.
- The **actual** scope, which is usually narrower than claimed. Check sibling
  paths: if it's a sync bug, do all three transports (`_pull_ssh`,
  `_pull_local`, `_pull_http`) share it? If it's a CLI bug, does every
  subcommand share it?

Reporters diagnose from reading source and often get the mechanism subtly
wrong even when the symptom is real. Say so plainly in the PR — a corrected
mechanism changes what the right fix is.

**When a report blames a change, check whether the symptom predates it.** This
is one query and it can redirect the whole arc. #34 argued that bumping
`setup-uv` broke two doctor tests — "two different tests failing across two
runs is a changed environment, not two coincidental flakes," which reads as
sound reasoning. One look at CI history showed the same test failing with the
identical assertion on `main` *before* the bump, under the old version. The
report had a real defect and a real infrastructure fix in it, wired to each
other by an inference that a single query dissolved:

```bash
gh run list --repo kgruel/siftd --branch main --limit 40 \
  --json databaseId,conclusion,headSha,displayTitle \
  --jq '.[] | [.databaseId,.conclusion,.displayTitle] | @tsv'
gh run view <run-id> --repo kgruel/siftd --log-failed | grep -E "FAILED|assert "
```

The general form: a correlation offered as a cause is a claim you can test
against history, and testing it is cheaper than investigating the wrong
subsystem. Where a report says "X started failing when we changed Y", the
first move is to find out whether X was already failing without Y.

If it turns out invalid or already fixed, say that and stop. Don't build.

## 2. Branch and fix

```bash
git checkout -b fix/<short-slug>
```

Commit as work goes green ([[auto-commit-on-feature-branches]]); push is fine
once a branch exists. Never commit on `main`.

**Leave pre-existing dirty files alone.** `.loops/data/project.db` and
sometimes `CLAUDE.md` are dirty at session start — stage explicit paths, never
`git add -A` at the repo root.

**Enumerations must be derived, not sampled.** The trap this skill exists to
prevent: when a fix depends on "what shapes can this field hold", answering it
by querying the live database gives you the shapes that database happens to
contain, not the shapes the code can emit. Grep the producers. In the #21 fix,
a survey of `started_at` found four spellings; a fifth was reachable from
`epoch_ms_to_iso` whenever milliseconds were zero, and the bound silently
excluded it. External review found what the sample missed.

Prefer a mechanism that removes the fragile reasoning over one that patches its
conclusion. Same case: an ASCII-ordering argument about `.` vs `+` vs `Z` was
replaced by prefix containment, which doesn't depend on ordering at all.

**Write the test that would have caught it.** Then check whether a test already
claimed to cover the area and was asserting a fake — #21 survived four minor
versions behind a test that asserted `--since` was *carried* onto the wire with
the value `"2024-01"`, itself a string the parser rejects. If the round trip
crosses a CLI boundary, exercise the real parser (`from siftd.cli import
_build_parser`), not the underlying function ([[cli-argparse-test-gap]]).

## 3. Changelog

Add the entry to `CHANGELOG.md`'s `[Unreleased]` as the work lands — that
section is the release container and is never reconstructed at cut time. If the
section doesn't exist (a release was just cut), create it above the newest
version heading.

Revisit it after review: if `codex review` changes what you shipped, the
changelog describes the old design until you fix it.

## 4. Green, then PR

```bash
./dev check          # lint + architecture + base lane + docs gate
```

Gotchas that will cost you a cycle:

- **`./dev docs --check` regenerates before it diffs**, against the *index*. So
  it fails on regenerated-but-unstaged docs, and re-running `./dev docs` can
  never fix it — `git add` the files it lists, and commit them with the change.
- **Changing any `--help` string** moves `docs/reference/cli.md` and the help
  snapshots. Regenerate all three interpreter lanes:
  ```bash
  for v in 3.12 3.13 3.14; do uv run --python $v --extra dev pytest tests/snapshots/ --snapshot-update -n 0; done
  ```
  **`uv run --python 3.14` replaces the project `.venv`.** Restore it after:
  ```bash
  uv venv --clear --python 3.13 .venv && uv sync --extra dev --extra serve
  ```
  Better: if you're only deduping help text, interpolate a constant so the
  rendered bytes don't change at all — then nothing regenerates.

Then open the PR with `Fixes #N` in the body (and in the fix commit message, so
the issue closes even on a local merge):

```bash
git push -u origin fix/<short-slug>
gh pr create --repo kgruel/siftd --base main --title "<conventional subject>" --body "..."
```

PR body should carry: what broke and the verified mechanism, the corrected
scope, why this fix over the alternative, why it survived (the test gap), and
an explicit **Out of scope** section.

## 5. `/simplify`

Run it. Four parallel agents review reuse / simplification / efficiency /
altitude, then you apply what survives.

**Commit first, then launch every review agent with `isolation: "worktree"`.**
A "read-only" review agent is not read-only in practice: to answer *is this
slower?* or *does this finding reproduce?* the honest move is to run the code,
and running it means A/B-swapping files, reverting the fix, or checking out the
base revision. On #34 two of the four agents wrote to the shared tree — one
restored the pre-fix file to reproduce the bug and left it there, the other
copied `main`'s version over the working copy to benchmark, then restored it —
and their windows overlapped with each other and with my own edits. The fix
survived only because a later read happened to show the reverted file; a
`git commit -a` in that window would have shipped the pre-fix code under a
message describing the fix.

Isolation is the structural fix, not "tell the agents not to write" — the
writes are legitimate, the shared target isn't. Consequences to know:

- **The worktree is created at the base commit, not at your branch**, so
  `git diff main...HEAD` inside it is *empty* and the agent reviews nothing.
  On #38 all four agents burned their full budget on this and were killed by
  the stall watchdog with zero findings. Push the branch first, then give every
  agent the checkout as a literal instruction:
  ```
  git fetch origin
  git checkout -B review-local origin/<your-branch>
  git diff origin/main...HEAD
  ```
- Give them a time budget in the prompt ("finish well under 10 minutes, return
  partial findings rather than nothing") — the watchdog kills a silent agent at
  600s, and an agent told to benchmark will happily spend that on `./dev setup`.
- Findings come back as text and you apply them in the real tree. Anything an
  agent "fixed" in its worktree is discarded — which is what you want from a
  reviewer.
- Cite an agent's measurements only after reproducing them yourself. On #34 the
  efficiency agent reported a 4× speedup from the same change that measured
  ~18% when I ran it; its numbers were contaminated by warm-cache effects and
  by the file-swapping above.

It reliably finds adjacent defects, not just style — on #21 it caught that
`db send/push/pull` passed `parse_date` to argparse as a bare `type=`, which
swallows the exception message, so the vocabulary hint the fix had just widened
never reached users on the three subcommands the fix was about. On #34 it found
that the fix left its own residue unswept: `open_database` still carried the
`check_same_thread` knob whose docstring recommended the pattern the fix had
just disproved, citing doctor — the caller that had stopped using it.

Skip findings that need changes well outside the diff, but **record them** —
they become issues in step 8. A same-name-different-contract collision is worth
renaming in-diff even when the full consolidation isn't.

## 6. `codex review` (external)

```bash
codex review --base main -c model="gpt-5.6-sol" -c model_reasoning_effort="medium"
```

`--base` and a prompt argument are mutually exclusive — pass one or the other.

Then, per finding:

- **Verify it yourself before acting.** Reproduce the claim in the venv.
- If confirmed, fix it and **re-run the review** against the corrected branch.
- Later findings are often pre-existing conditions rather than regressions.
  Disposition those with *evidence*, not assertion: on #21, an aider
  local-time skew looked like a new data-loss path until a two-line check
  showed the raw cursor the other transports already use excludes the same
  rows, and the new bound is a prefix of it — strictly more inclusive.

Stop when a pass returns only items you've dispositioned with evidence. Don't
loop for a clean sheet; deferred-with-reasoning is a valid terminal state.

**Re-sync the PR body.** If review changed the design, the body still argues
for the design you removed. This is the step most easily forgotten.

## 7. Merge

`main` must be releasable at all times, and branch-green is **not** transitive
across merge ([[post-merge-harness-rerun-discipline]]).

```bash
git checkout main && git fetch origin
git merge --no-ff fix/<short-slug> -m "Merge fix/<short-slug>: <what it does> (#N)

<2-4 line summary: the defect, and anything the review pass changed.>"
./dev check                                    # on main, before pushing
git push origin main
git branch -d fix/<short-slug>
git push origin --delete fix/<short-slug>
```

Confirm the PR and issue closed, **and that CI on the push went green**:

```bash
gh pr view <PR> --repo kgruel/siftd --json state --jq .state
gh issue view <N> --repo kgruel/siftd --json state --jq .state
gh run list --branch main --limit 1
```

Local green is not CI green: CI runs a Python matrix (3.12/3.13/3.14) your
venv doesn't, and it runs on cold caches and different timing. A local `./dev
check` passing three times in a row does not tell you the push was clean.

If CI failed, identify the test before assuming either flake or regression:

```bash
gh run view <run-id> --repo kgruel/siftd --log-failed | grep -E "FAILED|assert " | grep -v PASSED
```

Then check `git log --oneline -3 -- <that test's file>` — if the file predates
your arc and the failure is timing- or randomness-shaped, it's a pre-existing
flake, not your regression. Known ones: `tests/cli/test_upgrade.py` (see below)
and ULID prefix collisions in ID-resolution tests (a 12-char `short_id`
carries only ~10 bits beyond the millisecond, so same-millisecond IDs collide
~1/1024).

**"Pre-existing flake" is a routing decision, not a diagnosis.** Report it and
file it; don't silently rerun past it, and don't rework code that isn't yours —
but spend the five minutes to find the actual mechanism first, because the
mechanism is often bigger than the flake. This skill listed
`tests/cli/test_upgrade.py` as a *real-clock* flake for two arcs. It isn't. On
#34, reading it properly showed `main()` spawns a live daemon update-check
thread that hits PyPI, and `_write_cache` resolves `state_dir()` **at write
time** — so the thread can land in an unrelated test's `tmp_path` long after
its own test finished. The flaky assertion was the only one that happened to
notice; the real finding is that the suite makes live network calls and one
test can silently corrupt another's fixture (#40). A wrong label in this list
is worse than no label, because it retires the question.

**Never chain the check into the push.** `./dev check | grep -E "All checks|failed"`
followed by `&& git push` will push on failure, because grep *succeeds* when
it matches the word "failed". Run the check, read it, then push.

## 8. File what you deferred

**A merged PR body is write-only.** Everything in its "Out of scope" section
disappears from view the moment it merges. Before wrapping, file the durable
items as issues.

Decompose by *cause*, not by symptom. If several deferrals are sites where one
thing is missing, that's one issue naming the absence and listing the sites,
not N issues that each fix it again — substrate first, instances as
consequences ([[rollup-layer-design-2026-06-02]] is the precedent: one layer
dissolved 13 recompute sites and 4 bugs).

Then ratify into the version's roadmap node so it answers "is this in the
release?":

```bash
sl read project --kind roadmap --plain
```

And ask whether any established invariant can become an enumerable-property
ratchet with a shrink-only allowlist, in the shape of
`tests/architecture/test_imports.py` — invariants that live only in review
vigilance drift.
