# Hardening and release-readiness follow-up

2026-09-27 · **LOCAL CANDIDATE; PUBLICATION HOLD.** No version is chosen.
This supersedes the current-status portions of the
[earlier release assessment](release-readiness-next.md), not its historical evidence.
A fresh exact-head Fable High assessment is required; the earlier release report's
Astra startup-timeout eligibility is not reused or silently waived.

## Scope and delivered work

Baseline local integration `eff4bf7d26d47fc2597c5df94ee81b83aa239ddc` contains
preserved design drafts, the reviewed test-isolation fix, and the independently
Fable-approved private Pi preflight. The preflight measures exposure and
uncertainty; it is fixture-only, has no public command, and assigns no new keys.
Original main remains outside this local integration.

Hardening commit `6af53f81b4d85f7170802e208eea7307cfe8f186`:

- Whole-request refusals count once with unit `request`, including empty source
  scopes; affected-source counts remain separate. This clarifies the private
  report contract, not a public compatibility surface.
- Strengthens DEBUG-level secrecy, exact candidate denominators, keyed exposure
  for non-finite arguments, no opening of unselected source files, refusal byte
  preservation, and single-transaction assertions. Pins raw tool candidates from
  discarded assistants separately from stored emitted-event counts.
- Makes recursion-error propagation deterministic while retaining a real deep
  input test. Python 3.14 can decode the nested array that raises on older
  interpreters; the unchanged shared parser wraps non-object JSON as raw text.
  The diagnostic must follow that behavior, not require one interpreter's limit.
- Corrects merge/receive `--no-fts` help and examples: **compatibility no-op; does
  not disable indexing**. All three supported Python help snapshots are updated.
  `db slice --no-fts` still suppresses output indexing; no flag is removed, no
  default changes, and no removal version is invented.

This follow-up also narrows Unreleased's tag-preservation wording to what
`storage/replacement.py` actually carries. Conversation and matching source-keyed
event/exchange assignments can carry; block tags and unkeyed/unmatched event
assignments still cannot. Previously lost tags are not restored by upgrade.
Disclosure is not acceptance of metadata loss or authorization for migration.

## Verification and exact subjects

All evidence is local macOS arm64, owned venvs, external disposable HOME/XDG/TMP,
foreground default signals, suite-owned passive-update suppression, and offline
model inference. Dependency wheels missing for the additional Python versions
were fetched into the isolated development cache; no user tool was reinstalled.

| Check | Observed result | Subject / qualification |
|---|---|---|
| `./dev check --all -v`, Python 3.12.12 | Exit 0; architecture 94/5 skipped, base 4,284, serve 359/4 collection skips, embeddings 111, slow 2; **4,850 passed / 9 skipped** | Exact clean `6af53f81…`, tree `17d84daa3d1e54fe45551c0137b9851ee55e80b0`; lint/docs passed |
| Base-marker suite, Python 3.13.11 | Exit 0; **4,376 passed / 11 skipped** | Corrected working tree before `6af53f81…`; same code/tests, no post-commit claim |
| Base-marker suite, Python 3.14.6 | Exit 0; **4,376 passed / 11 skipped** | Same qualification as 3.13 |
| Help snapshot regeneration, Python 3.12/3.13/3.14 | All exit 0; only intended receive-help changes | Separate version-owned venvs/snapshot directories; no hand-edited snapshots |
| Real-browser T3 | **Harness fault, exit 2; no browser PASS** | See below |

The local 3.13/3.14 commands include architecture tests; their totals are not
additive with the split-lane 3.12 total. Their 11 skips are five missing-fastembed
collection guards, one noncanonical-interpreter generated-CLI check, and five
existing session-strategy cases. Embeddings are tested in the 3.12 all-lane run.
No skip conditions were added or relaxed.

Receipts and raw logs are under
`/tmp/siftd-next-slices-01a0ddb3/hardening-evidence/`:
`check-code-head-all/`, `base-313-corrected/`, `base-314-corrected/`, and
`snapshots-312/313/314/`. Each receipt records command, environment, exit and
before/after subject state. Final candidate and local-integration checks/review
must be bound to their own exact heads in external receipts; the table above
never retroactively changes its tested subject. This report and the final
changelog qualification follow the code checks as documentation-only changes.

Unsuccessful attempts remain recorded: an external launcher's concurrent Git
index-lock collision before venv execution; an offline cache miss for a 3.14
wheel; early matrix runs finding stale generated inventories and the 3.14
recursion-test assumption. The launcher now reads index metadata without writing
it, dependencies were explicitly provisioned, inventories regenerated, and the
native-decoder/propagation distinction tested. None was silently retried as a pass.

## Browser evidence: execution attempted, not certified

The unmodified `tests/browser_smoke/smoke.py::run` used a disposable fixture DB,
from-source server on a fresh loopback port, and a separate Chromium profile.
Four HTTP-header checks passed, but Chromium never exposed its CDP endpoint;
positive-control and interactive browser assertions **did not execute**. The
standalone CLI's passive updater was explicitly suppressed for this fixture run.

A separate blank-page startup probe also hung and emitted:
`Trying to load the allocator multiple times. This is *not* supported.`
The root cause is not fully diagnosed. Both owned Chromium children were stopped
after SIGTERM failed; no user profile or unrelated process was touched. No browser
reinstall, sandbox weakening, or application/test change was used to manufacture
a result. Logs: `browser-smoke-01/`, `browser-fixture/server.log`, and
`chromium-startup-probe/` under the artifact directory above.

This hardening delta changes no serve/CSP/JS behavior. Accumulated release work
also includes earlier serve request/date/error handling and rendered timestamps.
A working T3 run or an explicit applicability disposition remains outstanding;
HTTP/TestClient checks are not browser execution. Fresh Ubuntu/Python 3.12–3.14
CI is also unverified: local macOS coverage does not certify it, and no push or
workflow dispatch was performed.

## Remaining decisions and release boundary

1. Obtain fresh independent review of this exact hardening/report candidate;
   resolve material findings, then integrate only into the isolated local branch
   and rerun the harness. Historical Astra/Fable approvals retain their scopes.
2. Resolve browser availability/applicability and obtain fresh CI evidence before
   declaring publication readiness. Keep those gaps visible rather than weakening
   checks or claiming that local green is CI green.
3. User chooses release scope/version and authorizes eventual main integration
   and publication separately. Existing no-op controls stay accepted; any later
   removal needs a compatibility ledger and explicit removal version.
4. Live preflight use, a public surface, stable event-key extraction, and any
   lossless mapping/migration remain separate work requiring the corresponding
   authorization and proofs. Neither runtime diagnostic nor green fixtures
   establish historical mapping authority or corpus-wide impact.

No production data/configuration, original-main, GitHub, loops, push/tag/version,
or publishing changes are part of this slice.
