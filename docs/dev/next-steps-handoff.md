# Next steps — preservation handoff

Date: **2026-09-26**. Source: clean `main` at
`58daf4ac0101a012ff0ea286931fce8e2e621110`, verified in the original checkout.
This slice preserves local history; it does **not** ratify or implement a design.

## Delivered code versus publication

The source identifies the package as **0.12.1**; the latest reachable release tag
is `v0.12.1`. Work accumulated after that tag is on main, **not thereby published**.
[Unreleased](../../CHANGELOG.md#unreleased) records replacement tag/owner carry,
schema-owned merge cleanup, scoped FTS maintenance, WAL-aware reads, sync/date
fixes, adapter fixes, and the #79 delete-keyed replacement ratchet. In the ingest
plan, only slice 0 (that ratchet) is recorded as shipped. This is not a claim that
the streaming/upsert architecture or the other proposed slices exist.

Release readiness still needs fresh all-lane evidence and compatibility review;
historical tests, performance numbers, issue claims, and live-corpus counts are
not current validation. Repairs are not automatically retroactive (notably lost
ownership); this run must not ingest, repair, or reparse production data.

## Preserved documents and their authority

These four formerly ignored files are copied byte-for-byte, not rewritten:

- [Ingest stream v2](ingest-stream-design-2026-08-13.md): proposal following a
  failed v1 review; independent R2 is pending. Key identity, old unkeyed-event
  metadata, parser invalidation, graph reconciliation, ordering, and source
  authority need verification before an executable first slice is approved.
- [R1 review](ingest-stream-review-r1-2026-08-13.md): historical Codex REWORK
  verdict and findings, including duplicated output. It is not the requested
  Claude approval. Original absolute source links/line numbers are historical
  pointers; the new handoff's links are repository-relative.
- [CLI surface](design/cli-surface-0.13.0-2026-07-18.md): still **DRAFT**.
  It explicitly records Kyle's 2026-07-18 ratification of D-A (atomic sync split),
  D-B (mandatory list object; catalog plus exit 2), and D-C (tag subtree with
  legacy grammar compatibility until 1.0). Preserve those recorded decisions;
  D-D tier trims and the overall draft are not newly ratified here.
- [Package split](package-split-plan-2026-06-22.md): historical working plan
  grounded at `99eadefa`, with outstanding packaging/default-capability choices.
  Some premises have aged (e.g. current base dependencies include mistune and
  numpy). It is neither implementation authority nor a promise of a version.

The ingest draft's “Decided (flagged, not asked)” section is its author's recorded
position, not evidence of new user ratification. No document's force-add changes
its status. Local paths, aggregate measurements, and reviewer/session metadata
remain; inspection found no credentials or private transcript excerpts. Commands
in historical reproduction sections are **not** permission to access live data.

## Gates and next lanes

Preparation results below are historical evidence from preservation `4552bacf`,
not the current design lane's validation. See the
[design R2 record](ingest-stream-review-r2-2026-09-26.md) for the separate original
eight-failure attempt and corrected validation after merging the reviewed #40
guard. Neither validation nor a reviewer-policy change authorizes identity work.

1. **Preparation BLOCKED:** Python 3.12.12, its own `.venv` with dev/serve/embed
   extras, regenerated docs, and `./dev check -v` in disposable HOME/XDG paths.
   Docs regeneration succeeded without generated changes. The check exited **1**:
   lint passed; architecture had **94 passed, 5 skipped**; base tests had
   **4,039 passed, 2 failed**.
   `TestNotice.test_notice_when_newer` expects a notice suppressed by
   `SIFTD_NO_UPDATE_CHECK=1`. That setting isolates the known #40 live-update
   network behavior; it does not fix #40 or test an unmodified environment.
   `test_hybrid_rrf_surfaces_keyword_hits_with_empty_index` requests local
   `BAAI/bge-small-en-v1.5`, absent from the disposable cache while
   `HF_HUB_OFFLINE=1`. No remote embedding provider was called. No tests were
   changed, skipped, or retried to hide these failures. The harness stopped before
   its final docs gate; standalone `./dev docs --check` subsequently passed (exit
   0). Optional runtime lanes were not run. Environment provisioning/isolation
   needs resolution before preparation can clear. The staged whitespace check
   flags an existing blank line at the R1 review's EOF, retained for byte fidelity.
2. New independent review in this wave uses actual **`/opt/homebrew/bin/codex`,
   model `gpt-6-astra`, reasoning high**, as temporarily authorized by the user.
   Historical Fable reviews remain valid for their exact scope; the old Fable
   availability preflight was not approval. No Claude requests or quota polling
   in this wave. A future wave can return to Fable after availability is established.
3. Separate release-readiness and design worktrees are to start from a passing
   preservation commit; they are **not created while preparation is blocked**.
   A bounded identity slice may follow only after its exact migration,
   metadata preservation, invalidation, and tests clear the design gate. CLI
   redesign, package split, full streaming/upsert, daemon, and merge-door redesign
   are outside this first implementation lane.

Run artifacts (commands, hashes, preflight JSON, dependency and check logs) live
under `/tmp/siftd-next-slices-01a0ddb3/logs`. A candidate “ready” means preparation
passed, never that the external review gate or release decision has passed.

## Permission and deferred ceremony

No push, tag, version bump, publication, GitHub mutation, user-tool installation,
or production mutation is authorized. Release version and publication permission
remain user-controlled; local branches named “release” are assessment workspaces,
not publishing authority. Do not merge into or alter the original main checkout.

Loops ceremony is deferred to the user: no `sl`, `.loops` changes, store/cache/key
changes, or emitted records. Future handoff may summarize preserved provenance,
actual tested/reviewed SHAs, gate results, unresolved decisions, and release HOLD
versus candidate status; this document performs none of that ceremony.
