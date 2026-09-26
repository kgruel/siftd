# Next steps — integration handoff

Updated **2026-09-26**. **LOCAL INTEGRATION; FINAL ASTRA REVIEW PENDING;
RELEASE/PUBLICATION HOLD.** Original main remains
`58daf4ac0101a012ff0ea286931fce8e2e621110`; only local branch
`work/next-slices-01a0ddb3` changed. Package version remains **0.12.1**.
Accumulated code on main is not thereby published.

## Actual approvals and integrated scope

The user explicitly authorized switching this review wave to actual
**`/opt/homebrew/bin/codex`, model `gpt-6-astra`, reasoning high** until Claude
Code limits reset. The older checkpoint's assertion that this substitution was
unauthorized is stale and incorrect. No Claude request or quota poll was made
in this wave. Historical Fable approvals retain their exact scope; a future
wave may return to Fable after availability is established.

| Candidate | Exact approved head | Authority |
| --- | --- | --- |
| Test isolation #40 | `53d0b9cb4de553bc003af61d02942c9cfa03bcd2` | Genuine prior Fable approval, unchanged exact diff; not re-reviewed. |
| Release assessment | `448d50540e4afc24ff92a513fb088a0417d41fd5` | Actual Codex Astra/high PASS; round 2 verified and reused round 1's passing raw review. Documentation only. |
| Design disposition | `b98b27660164c05ee471e5641076fb5c7fd6fc35` | Fresh actual Codex Astra/high round-2 PASS. Documentation only; **safeToImplement=false**. |

Both documentation reviews use test-fix `53d0b9cb…` as their base. Exact argv,
subjects, result paths and hashes are in
[Codex provenance](reviews/astra-candidates.json); historical test approval is in
[the exact-diff receipt](reviews/test-isolation-exact-diff.json). Full reports and
raw transcripts remain in the external artifact paths, not copied into prose.
The earlier design review's provider timeout remains recorded; its PASS was
**not** reused. The clean fresh round-2 review supplies that candidate's approval.

Release and design were merged with ancestry, respectively at `342b4c26…` and
`8b7cb327…`; their shared test-fix ancestor was not cherry-picked or duplicated.
Neither input candidate was excluded. This handoff and integration evidence are
new changes: **input approvals do not approve the final combined candidate**.
Actual final Codex review remains a separate gate.

## Local validation and retained failures

The merged HEAD `8b7cb327776c8f29111f621becc4cddd5d271236`, tree
`e4bdd48173f2dea522f176998796ec8e59d87c99`, passed `./dev check --all -v`
with **exit 0**, clean before and after:

- Lint and strict generated-doc checks passed.
- Architecture: **94 passed / 5 pre-existing skips**; base: **4,042 passed**.
- Serve: **359 passed / 4 dependency collection skips**; embeddings:
  **111 passed**; slow: **2 passed**. No tests or skip conditions changed.

This is local macOS arm64 / Python 3.12.12 evidence. The worktree owns its venv
with dev/serve/embed extras. Each command uses disposable HOME/XDG and TMPDIR
outside all Git checkouts, a fresh environment without caller-global
`SIFTD_NO_UPDATE_CHECK`, `SIFTD_DB`, `SIFTD_CONFIG`, `PYTHONPATH` or
`PYTEST_ADDOPTS`, and default foreground SIGINT/SIGTERM. The suite owns passive
update suppression. Dependencies and model weights are offline;
`FASTEMBED_CACHE_PATH=/tmp/siftd-next-slices-01a0ddb3/model-cache`,
`HF_HUB_OFFLINE=1`. No remote embedding-provider calls were made.

Integration evidence lives under
`/tmp/siftd-next-slices-01a0ddb3/astra-review-wave/integration/`:
`check-merged-all/receipt.json` and `output.log` record that run. The final
`candidate.json` must bind subsequent committed-head `check-head-all` and
`check-head` receipts to the exact final HEAD/tree before marking readiness.
A ready candidate means ready for final review, not approved or publishable.

Earlier failures remain failures, not erased by later green: preservation had
notice/cache failures; the original design attempt put temporary fixtures inside
Git and failed eight workspace tests; release had an interrupted signal-unsafe
run; normalization had a dependency-install timeout. Their raw attempts and
corrections remain in [release evidence](release-readiness-next.md) and
[design R2 evidence](ingest-stream-review-r2-2026-09-26.md). Those documents'
“pending review” labels describe their preparation stage; the exact-head reviews
above supersede those labels, not the underlying limitations.

Local checks do not certify Ubuntu/Python 3.12–3.14 CI or T3 real-browser smoke.
Fresh CI evidence and browser execution/applicability disposition remain release
gates. The eventual main merge must rerun the harness; main was not merged here.

## What the design approval does not decide

[Identity-first plan](ingest-identity-first-slice.md) and
[revised ingest design](ingest-stream-design-2026-08-13.md) are approved as
**planning documents**, not ratified implementation contracts. Current
replacement loses NULL-key/unmatched-event and block assignments; warnings are
not consent. Neither production identity migration nor acceptance of metadata
loss is authorized. No identity extraction, preflight, invalidation, runtime or
storage changes were implemented in this integration.

The historical [R1 review](ingest-stream-review-r1-2026-08-13.md),
[CLI draft](design/cli-surface-0.13.0-2026-07-18.md), and
[package-split plan](package-split-plan-2026-06-22.md) remain preserved. CLI D-A,
D-B and D-C retain their recorded historical ratification; that does not ratify
the whole draft or D-D. Package split, CLI redesign, full streaming/upsert,
daemon and merge-door redesign remain outside this run. Historical corpus and
performance figures are not newly verified. Repairs are not automatically
retroactive; no production corpus was read, ingested, reparsed or repaired here.

## Remaining user decisions

1. **Release scope and compatibility:** defining versus ride-along patches,
   version and eventual publication permission; disposition of ignored legacy
   FTS controls/misleading help and their communication/removal policy. These
   are not settled by local green or the release report's approval.
2. **Next diagnostic scope:** whether to authorize the proposed pi_agent-only,
   fixture-backed read-only preflight and its surface/contract. No apply mode;
   live-data inspection needs separate explicit authorization.
3. **Future identity transition:** ratify valid-ID/namespace and duplicate-refusal
   rules, then prove lossless event/block mapping and source authority, durable
   coverage/completion, interruption/retry/rollback, and protection on natural
   changed-file ingest—or defer legacy data. These proofs remain engineering
   gates, not an invitation to silently accept loss.

No push, tag, bump, publication, GitHub mutation, user-tool installation,
production/config mutation or original-checkout edit occurred. No `sl` or
`.loops` access/write occurred. Eventual user-owned emit summary only: preserved
provenance; exact reviewed/tested SHAs; integrated/excluded scope; gate results;
these unresolved decisions; final-review pending versus release HOLD. No ceremony
is performed by this document.
