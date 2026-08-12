---
name: issue-fix
description: Take a GitHub issue from triage to merged PR — verify it against current main, fix on a branch, /simplify, external codex review, changelog, merge, then file what you deferred. Use when the user names a siftd issue number, says to fix or work an issue, or asks whether a reported issue is valid.
---

# siftd Issue → PR

The flow is: **triage → branch → fix → `/simplify` → `codex review` → merge →
file the deferrals → name what's next.** Each stage has caught something the
previous one missed. Don't skip the external review because the internal one
came back clean; they find different classes of thing.

Two things are templates rather than prose, and each step points at its own:
`references/bodies.md` (PR and issue bodies) and
`references/simplify-agents.md` (the four review-agent prompts).

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

**Re-measure every number the report states.** An issue's enumeration was true
the day it was written; the arcs that landed since are what make it stale, and
a stale count reads exactly like a fresh one. #39 named four duplicated
read-only opens — #42 had already rewired three. #48 cited "12 `sqlite3.connect`
call sites, 8 of them legitimate" against a real 8 and 5, and I carried 12/8
into a ratchet docstring before a reviewer re-ran the grep. That is the same
sampling error as §2's, arriving through the front door: inheriting a
measurement is not making one. Re-run the count in the body, and if it moved,
say so in the PR — a corrected count changes the carve-outs, and sometimes the
design.

If it turns out invalid or already fixed, say that and stop. Don't build.

**"Already fixed" is rarely all-or-nothing, and the partial case has its own
move.** An issue whose substance a sibling arc dissolved often leaves *residue*
— a comment, a test exemption, a docstring — that still argues from the
mechanism that is gone. Filing a new issue for it is noise, leaving it open is
false, and rebuilding it is waste. Close it inside the PR that removes the
residue's reason, with `Fixes #N` and a Scope paragraph stating plainly what
shipped where. On #39 every artifact it named was gone with #42, and what
survived was two sites arguing from the vocabulary-cache side effect that #47
was in the middle of removing — including an exemption permitting a
`sqlite3.connect` call that no longer existed.

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

**Then check you enumerated the right population** — grepping producers
faithfully still fails if you picked the wrong set to grep, and a complete
sweep of the wrong scope never looks incomplete from inside. On #32 I swept
every `since`/`before` handler in `serve/routes.py` and claimed the whole HTTP
surface; `serve/html_routes.py` serves the htmx UI without going through that
file, and one of its routes still passed raw dates into a filter.

So name the **role** first — "every input context", "every producer of this
column" — then find its members by grepping for the *capability*, not for
callers of the function you happen to be editing:

```bash
grep -rn 'Parameter(query="since")' src/     # every input context
grep -rn "started_at=" src/siftd/adapters/   # every producer
```

If the answer comes out as "all N in this file", that phrasing is the tell: the
file was the assumption, not the finding.

**A derived enumeration is still a sample when its derivation is
fixture-dependent.** That is the rule above one turn deeper, and it cost three
findings in one arc precisely because deriving *felt* like having answered.
#54's ratchet enumerated what a conversation delete removes by deleting one and
diffing row counts — derived from the database rather than from a registry,
which was the whole point, since the registry was already wrong. It missed
`ingested_files`, a declared cascade child the fixture never writes a row to.
Adding that table fixed the instance; external review then found `attributes`,
cleaned by a trigger so no foreign key describes it, and also unpopulated.
Neither half could see the whole: `PRAGMA foreign_key_list` is blind to
trigger-driven cleanup, and a row-count diff is blind to a table the fixture
skipped.

So when no single derivation is complete, ask twice and union the answers — and
add the assertion that runs the other way, `declared - reached == set()`, so a
fixture that *stops* exercising a listed member fails instead of quietly
passing. Without that second direction the population can only grow, which is
the same blindness in slower motion.

Prefer a mechanism that removes the fragile reasoning over one that patches its
conclusion. Same case: an ASCII-ordering argument about `.` vs `+` vs `Z` was
replaced by prefix containment, which doesn't depend on ordering at all.

**Write the test that would have caught it.** Then check whether a test already
claimed to cover the area and was asserting a fake — #21 survived four minor
versions behind a test that asserted `--since` was *carried* onto the wire with
the value `"2024-01"`, itself a string the parser rejects. If the round trip
crosses a CLI boundary, exercise the real parser (`from siftd.cli import
_build_parser`), not the underlying function ([[cli-argparse-test-gap]]).

**Then falsify it — and back the file up with `cp`, never `git checkout`.**
Falsifying means deliberately breaking code you have not committed, so the undo
has to restore your *working* state. `git checkout <path>` restores **HEAD**,
which silently discards everything uncommitted in that file:

```bash
cp src/siftd/storage/sqlite.py "$SCRATCH/sqlite.keep"   # before mutating
# ...mutate, run the test, confirm it reddens...
cp "$SCRATCH/sqlite.keep" src/siftd/storage/sqlite.py   # restore
```

On #48 I wrote `git checkout tests/architecture/test_readonly_opens.py || <fallback>`
to undo one mutation. The checkout **succeeded**, wiping a full ratchet rewrite,
and the `||` fallback never fired because nothing had failed. The green run that
followed was the *old* ratchet passing. Same hazard as §5's shared-tree writes,
one directory closer: the destructive command is yours.

**And bound both ends of any scripted edit.** `s.replace(old, new)` where `old`
was built as `s[s.index(marker):]` runs to the end of the file. On #33 that
rewrote one test and took four unrelated test classes with it — eight tests,
surviving two green `./dev check --serve` runs and a commit, because **deleted
tests do not fail**. The only residue was an import that had become unused, and
`codex` is what found it. Prefer the Edit tool, which errors on a non-unique or
non-matching target; when a script is genuinely the right instrument, end every
slice at an explicit second marker, then ask what disappeared:

```bash
for f in $(git diff --name-only main); do
  git show main:$f | grep -oE "^ *(async )?def [a-zA-Z_0-9]+" | sed 's/^ *//' | sort -u > /tmp/_a
  grep -oE "^ *(async )?def [a-zA-Z_0-9]+" "$f" | sed 's/^ *//' | sort -u > /tmp/_b
  lost=$(comm -23 /tmp/_a /tmp/_b); [ -n "$lost" ] && echo "--- $f" && echo "$lost"
done
```

Every name it prints should be one you meant to remove.

## 3. Changelog

Add the entry to `CHANGELOG.md`'s `[Unreleased]` as the work lands — that
section is the release container and is never reconstructed at cut time. If the
section doesn't exist (a release was just cut), create it above the newest
version heading.

**One line per change**, in the shape:

```markdown
- **What the user can now do (or stop hitting).** One clause of scope if the
  blast radius isn't obvious from the first. ([#N](https://github.com/kgruel/siftd/issues/N))
```

The reader is deciding whether to upgrade, not learning the mechanism. Mechanism
goes in the PR; provenance goes in the commits; both are one click from the
issue link — the same *rationale belongs in the durable artifact* rule that sets
the PR budget (`references/bodies.md`). It binds hardest here, because a
changelog entry **ships**: a PR body is read once, a changelog line is read by
every user of every later version.

Concretely, from #20:

> ~~Replacing a stale conversation deleted its raw children but left its
> derived-tier rows — `usage_by_conv_model` and `conversation_stats` both declare
> `ON DELETE CASCADE`, which the merge's `foreign_keys = OFF` disables — so the
> pre-commit `PRAGMA foreign_key_check` found them dangling and rolled the entire
> merge back… (14 lines)~~
>
> **`siftd db pull`/`push` no longer fails permanently once the other side
> re-ingests a conversation you already have.** The replaced conversation also
> stops answering searches from its deleted text. (#20)

If a change genuinely needs a paragraph — a breaking change, a stated trade, a
migration the user must act on — write the paragraph. That is the exception the
one-line rule exists to make visible, and it should read as one.

Revisit it after review: if `codex review` changes what you shipped, the
changelog describes the old design until you fix it.

## 4. Green, then PR

```bash
./dev check          # lint + architecture + base lane + docs gate
```

**Match the lane to the diff.** `./dev check` runs the *base* lane — it skips
serve, embeddings, and slow. A fix touching `serve/` that only ever ran the
base lane is branch-green on tests that never executed, which is the same class
of error as a test lane no CI job runs. CI will run them all on push, so this
is about finding out before the merge rather than after:

```bash
./dev check --serve     # anything under src/siftd/serve/ or tests/test_serve*
./dev check --all       # ...plus embeddings and slow
```

Do **not** reach for `pytest -m ""` to "run everything" — it un-gates the
optional-dependency lanes, so embeddings tests execute without `[embed]`
installed and fail in ways that look like your regression. On #32 that cost a
cycle proving two such failures were identical on `main`.

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

PR body: **four fixed headings — Defect · Scope · Evidence · Deferred — at
~400 words.** Issues use the same four with *Why now* in place of Evidence.
Read `references/bodies.md` before writing either: it carries the schema, why
the budget holds, and the end-state-not-journey split that decides what goes in
the body versus a PR comment.

Improvised headings invite improvised content, which is where the length comes
from — the measured spread is 886–1399 words across 8–9 sections when the
schema is skipped, against ~400 when it isn't.

## 5. `/simplify`

Run it. Four parallel agents review reuse / simplification / efficiency /
altitude, then you apply what survives.

**Commit and push first, then launch every agent with `isolation: "worktree"`,
using the prompts in `references/simplify-agents.md`.** Copy that preamble
verbatim — it carries the two things that are mechanical to include and
expensive to omit (the checkout literal and the time budget), and the per-angle
"specifically worth checking" bullets are what separate a review that finds
something from one that returns generalities.

A "read-only" review agent is not read-only in practice: to answer *is this
slower?* or *does this finding reproduce?* the honest move is to run the code,
which means A/B-swapping files or reverting the fix. On #34 two agents did
exactly that in the shared tree, with overlapping windows — a `git commit -a`
in that window would have shipped the pre-fix code under a message describing
the fix. Isolation is the structural answer, not "tell them not to write": the
writes are legitimate, the shared target isn't.

Two failure modes worth knowing even with the templates:

- **The worktree starts at the base commit, not your branch**, so
  `git diff main...HEAD` inside it is *empty*. On #38 all four agents burned
  their full budget on this and were killed by the stall watchdog with zero
  findings. That is what the preamble's `git checkout -B review-local
  origin/<branch>` exists to prevent.
- **Reproduce any measurement before citing it.** On #34 the efficiency agent
  reported a 4× speedup from a change that measured ~18% when I ran it —
  contaminated by warm caches and by the file-swapping above.

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

**A review can correct your reasoning rather than your code, and that is the
finding to take most seriously.** The recurring tell is a rationale that
defends a scope boundary by restating it as a principle. On #32 my comment
argued the app-level handler covered `UserInputError` alone because widening it
"would silently re-status errors that currently surface as 500s" — which
inverts the contract, since `errors.py` declares those statuses *as* the serve
contract, so the new status is the declared behavior and the 500 was the gap.
The scope was still right; the reason was wrong. Fix the comment to say plainly
that it is a scoping decision, and file the coherent end state — otherwise the
next reader inherits an argument for never doing it.

**Then hold the replacement rationale to the same test.** Rewriting a rationale
you just criticized is where the sin recurs, because the new wording arrives
feeling earned. On #47 I replaced doctor's workaround note with a reason — *a
diagnostic must not migrate the database it inspects* — that
`open_database(auto_upgrade=False)` already answers, and which six call sites
already use. I had reproduced the exact shape the PR existed to delete. The
check is one question: **is there an existing flag, parameter, or helper that
already settles the reason I just wrote?** If so it is not the reason; keep
looking until the surviving one is the one nothing else covers.

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

**Then re-sync the PR body and comment the provenance**
(`references/bodies.md`). This is the step most easily forgotten, and it stays
easy to forget precisely because nothing fails when you skip it.

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
flake, not your regression.

**"Pre-existing flake" is a routing decision, not a diagnosis.** Report it and
file it; don't silently rerun past it, and don't rework code that isn't yours —
but spend the five minutes to find the actual mechanism first, because the
mechanism is often bigger than the flake, and **a wrong label retires the
question**. This skill carried `tests/cli/test_upgrade.py` as a *real-clock*
flake for two arcs. It isn't: `main()` spawns a live daemon update-check thread
that hits PyPI, and `_write_cache` resolves `state_dir()` **at write time**, so
the thread can land in an unrelated test's `tmp_path` long after its own test
finished. The flaky assertion was just the one that happened to notice; the
real finding was that the suite makes live network calls and one test can
silently corrupt another's fixture (#40).

Because of that, this skill deliberately keeps no standing flake list — a
stale label here is worse than none. Check the open issues instead:

```bash
gh issue list --repo kgruel/siftd --search "flake in:title,body" --state open
```

**Never chain the check into the push.** `./dev check | grep -E "All checks|failed"`
followed by `&& git push` will push on failure, because grep *succeeds* when
it matches the word "failed". Run the check, read it, then push.

## 8. File what you deferred

Everything in a PR's **Deferred** section disappears from view the moment it
merges — the body is write-only, which is why that section is issue numbers
rather than descriptions. File them before wrapping, then back-fill the numbers
into the body — and never write a not-yet-filed number anywhere durable. On #49
a commit message named the knob-removal issue as #73 before filing it, and #73
went to the PR.

Decompose by *cause*, not by symptom. If several deferrals are sites where one
thing is missing, that's one issue naming the absence and listing the sites,
not N issues that each fix it again — substrate first, instances as
consequences ([[rollup-layer-design-2026-06-02]] is the precedent: one layer
dissolved 13 recompute sites and 4 bugs).

**Split a deferral along what is actually compat-bound.** A dissolution that
retires a knob has two halves, and only one of them is a compatibility surface:
the knob's *name* — a config key, a CLI flag, a public parameter — needs a
ledger and a stated removal version; its *behavior* almost never does. #49's
plan was to defer the whole thing, which would have shipped the incremental
index step while `rebuild_fts=True` still ran the O(corpus) rebuild it made
redundant — so the default configuration, the one the bug was reported from,
gained nothing at all. Deferring the flag's *name* is compatibility; deferring
its *cost* ships the bug. Make it a no-op now, file the removal, and state the
trade in the changelog, since a no-op flag usually drops a side effect someone
could have been relying on.

Then ratify into the version's roadmap node so it answers "is this in the
release?". Read it first, then append — the node is a fold, so emitting the
same key updates it rather than duplicating:

```bash
sl read project --kind roadmap --plain
sl emit project roadmap name=siftd-0.13.0 status=pending --stdin message < /tmp/node.md
```

`sl emit` takes `[vertex] <kind> KEY=VALUE ...` — **not** `--kind/--key/--status`
flags, which is the first thing you will try. Write the message to a file and
pipe it via `--stdin message`: these nodes run to several paragraphs and shell
quoting will mangle them. State whether the item is **defining or ride-along**,
since that is the question the node exists to answer at cut time.

And ask whether any established invariant can become an enumerable-property
ratchet with a shrink-only allowlist, in the shape of
`tests/architecture/test_imports.py` — invariants that live only in review
vigilance drift.

## 9. Name what's next

Last thing every run. Rank the open issues and say which comes next, with the
reason:

```bash
gh issue list --repo kgruel/siftd --state open --limit 40 \
  --json number,title,createdAt --jq '.[] | [.number,(.createdAt|split("T")[0]),.title] | @tsv' | sort -rn
```

Rank by **who is currently getting a wrong answer**, not by what is interesting
to build:

1. A silent wrong result reaching a user — no error, no signal.
2. A broken user-facing capability, weighted by whether it is live on the
   homelab (a receive-only server makes merge/push/search gaps real today).
3. A safety net that isn't catching anything — a vacuous fitness function is
   worse than none, because green is read as evidence.
4. Internal coherence.

Substrate that dissolves several open issues outranks its own instances, the
same rule as step 8's decomposition.

Two constraints override the ranking, and say which you applied: a **sequencing**
note recorded in the roadmap node or a memory ("#63 before #59, or the scaffold
lands on an ownership that then moves"), and **context heat** — an issue adjacent
to what you just touched is cheapest now and most expensive after a compaction.

Apply §1's re-measure rule to the issues you are *ranking*, not only to the one
you fix. A severity number in a body is a claim from the day it was filed, and
severity is what the ranking turns on. #33 states "~1/1000" collisions for
12-char ULID prefixes, which looks absurd — 12 base32 chars is 60 bits, so 12k
conversations collide at ~10⁻¹¹. It is defensible only because the first 10
chars of a ULID are the *timestamp*: the prefix carries ~10 bits of real
randomness, so the collision is confined to events created in the same
millisecond, which is exactly what bulk ingest produces. Right number, and the
reason changes the fix from prefix *length* to prefix *composition*.
