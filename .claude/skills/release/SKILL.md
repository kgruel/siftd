---
name: release
description: Cut and publish a siftd release (changelog, version bump, tag, push, CI/PyPI monitor, local reinstall). Use when the user says to release/ship/tag siftd, cut a version, or asks "let's release 0.x.0".
---

# siftd Release

Precondition: `CHANGELOG.md` already has a filled-out `[Unreleased]` section
(added incrementally during development, per repo convention) and
`./dev check` is green on `main`. If `[Unreleased]` is empty, stop and ask
what belongs in the release — don't invent changelog content.

## Steps

1. **Pricing sync**: run `./dev pricing-sync` and read its summary: added,
   changed (old -> new), kept-unlisted, pin disagreements, and the tiered count.
   It fetches models.dev live. If the fetch fails, `--from FILE` takes a saved
   copy; say so to the user. Review every changed price, since a repriced model
   changes the cost of history users already have. Pin a value you verified by
   hand, or leave it, before tagging. If the summary says `file changed`, commit
   the data on its own, with its changelog line in the same commit:

   ```bash
   git add src/siftd/data/pricing.toml CHANGELOG.md
   git commit -m "chore(pricing): sync from models.dev"
   ```

   The `[Unreleased]` line names the models added or repriced. If the summary
   says `file unchanged`, skip the commit. `./dev pricing-sync --check` gives the
   same answer without writing.

   This is a release step, not a CI step. A tag must publish exactly what a
   person reviewed, and the publish path must not depend on a third-party fetch.
   A CI job that rewrote prices would make a tag's contents depend on whatever
   models.dev served when CI happened to run.

2. **Changelog**: rename `## [Unreleased]` to `## [X.Y.Z] - <today's date>`
   (check `currentDate` context, don't guess). Leave the rest of the section
   content as-is. Then fix the link footer: point `[Unreleased]:` at
   `compare/vX.Y.Z...HEAD` and add `[X.Y.Z]: …/compare/v<previous>...vX.Y.Z`
   above the previous version's line. `tests/test_changelog.py` fails until
   both are right.

3. **Version bump**: `pyproject.toml` `[project].version = "X.Y.Z"`.

4. **Lockfile**: `uv lock` (updates the `siftd` version pin inside).

5. **Docs regen**: `./dev docs` — regenerates `docs/reference/*.md`
   (version string is embedded in `cli.md`'s `--help` capture) and the
   generated spans of the managed per-folder READMEs. Note: the tracked
   pre-commit hook (`.githooks/pre-commit`, wired by `./dev setup`) lints
   and re-runs this on commit, auto-staging `docs/reference/` (READMEs are
   not auto-staged; stage those yourself if their spans changed).

6. **Full check**: `./dev check` (lint + test). Must be green before
   proceeding — do not tag on red. After the bump, `tests/test_compat_ledger.py` fails on any
   `siftd.api.compat` entry whose removal version this release reaches: remove
   the surface with its entry, or restate the entry with a later version.

7. **Confirm with the user before pushing** — tagging and pushing to origin
   triggers a public PyPI publish. Use AskUserQuestion if not already
   explicitly authorized in this conversation.

8. **Commit, tag, push**:
   ```bash
   git add CHANGELOG.md docs/reference/*.md pyproject.toml uv.lock
   git commit -m "chore(release): X.Y.Z\n\n<1-2 sentence summary of the release>"
   git tag -a vX.Y.Z -m "vX.Y.Z - <short tagline>"
   git push origin main
   git push origin vX.Y.Z
   ```
   Leave unrelated dirty files (e.g. `.loops/data/project.db`) out of the
   release commit unless the user asked for them.

## Monitor CI/PyPI

Two **separate** CI runs fire: one on the `main` push (`ci.yml`), one on the
tag push (`publish.yml`, which runs its own `ci` job gate before `publish`
and `update-homebrew`). Check both:

```bash
gh run list --branch main --limit 1
gh run list --workflow publish.yml --limit 1
gh run watch <publish-run-id> --exit-status
```

**Known flake**: `tests/cli/test_upgrade.py::TestCache::test_fresh_within_interval`
occasionally fails in CI (real-clock timing, not mocked) but passes locally
and on rerun. If *only* that test fails, don't rework code — just
`gh run rerun <run-id> --failed`. Any other failure is a real regression;
stop and investigate, don't rerun past it.

Once `publish` and `update-homebrew` jobs are green, confirm PyPI actually
has the new version (propagation can lag ~30s):

```bash
curl -s https://pypi.org/pypi/siftd/json | python3 -c \
  "import json,sys; print(json.load(sys.stdin)['info']['version'])"
```

## Local reinstall

The `siftd` binary on PATH is a `uv tool` snapshot — it does **not**
auto-update. Reinstall after every release before dogfooding
([[uv-tool-install-drift]] in project memory):

```bash
uv tool uninstall siftd
uv tool install "siftd[embed,serve]"
siftd --version   # confirm it matches the tag
siftd doctor      # sanity check, pre-existing warnings are fine
```

## Scope notes

- Never `--force` push or rewrite a pushed tag — PyPI publishes are
  permanent per version; if a release is broken, ship a new patch version,
  don't retag.
- `update-homebrew` runs automatically off the `publish` job; no manual tap
  step needed.
