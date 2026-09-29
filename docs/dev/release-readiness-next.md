# Next release readiness — evidence candidate

> Historical assessment of the earlier release candidate. See the
> [current hardening and release follow-up](release-readiness-followup.md) for
> newer implementation, compatibility wording, local matrix, and browser evidence.
> The preparation-time pending-review labels below are retained as history.

Assessment date: **2026-09-26**. Status: **LOCALLY TESTED CANDIDATE — PENDING
ASTRA REVIEW VIA CODEX CLI; PUBLICATION HOLD.** This is an assessment of accumulated patches,
not a version choice, release authorization, or approval of preserved designs.

## Subject and authority

- Branch: `work/next-slices-01a0ddb3-release`; normalization entered clean at
  `4b00195f873bbb3f27997c8405070e8868cc02d2`. The review base remains the
  ancestor `53d0b9cb4de553bc003af61d02942c9cfa03bcd2` (reviewed test fix).
- Earlier full-suite tested commit: `53d0b9cb4de553bc003af61d02942c9cfa03bcd2`;
  tested tree: `840ec14fc197466ef167fae7a2b3948e9def1d88`.
- Original main: `58daf4ac0101a012ff0ea286931fce8e2e621110`;
  preservation: `4552bacf60ec6ac6a7b8b40334bc8c1e1d1bfb0c`.
- Scope baseline: reachable `v0.12.1`, commit
  `4e6db9868dc37b4d58ab01cf33fa282871f83358`, through the reviewed test fix.
  Package version remains **0.12.1**. Accumulation on main is not publication.
- This candidate adds only this report and review provenance. Normalization
  changes only prospective review policy and evidence/status reporting; it does
  not alter historical actual reviews, source, tests, lock files, or version.
  Exact candidate commit/tree and test subjects are in the new receipt below.

[CHANGELOG.md — Unreleased](../../CHANGELOG.md#unreleased) is the scope container.
The release skill was read but its publishing steps were not executed. The
[preservation handoff](next-steps-handoff.md) remains a historical record: its
preparation failures are not erased, but subsequent exact-diff review and fresh
local checks below supersede its earlier lack of test evidence. Its unapproved
designs remain unapproved. No roadmap/loops state was read or changed; defining
versus ride-along scope still requires the user's decision.

## Accumulated patch assessment

| Area | Delivered behavior and remaining boundary |
| --- | --- |
| Replacement integrity (#20, #51, #54, #77, #79) | Ingest carries ownership; merge carries target conversation/event tags and ownership. Merge deletion uses schema cascades, removes stale derived/FTS rows, and checks foreign keys even on dry-run. The delete-keyed architecture ratchet guards replacement sites; it is not proof of all dynamic control paths or all metadata survival. Changelog's ~15–20% replacement-heavy merge cost is historical measurement, not rebenchmarked here. |
| Search maintenance (#49) | Merge indexes the content it writes, transactionally; first receive indexes the copied database. Newly merged conversations no longer depend on full-corpus rebuild configuration to become searchable. Unrelated historical index drift is not incidentally repaired. |
| IDs (#33) | Event-prefix ambiguity now produces candidates/error instead of first-match success, including the event HTTP endpoint. Prefix lookup uses indexed ranges. This is not stable event/block identity across re-ingest. Historical speed/collision figures were not remeasured. |
| Dates and sync (#21, #31, #32) | ISO cursors parse, same-second sync boundaries no longer omit rows, naive shared timestamps mean UTC, and serve validates date filters. Aider's explicitly local source timestamps are converted at parse time; this is distinct from the shared naive-as-UTC rule. |
| Adapters (#36) | Aider histories use session dedup so later sessions ingest; previously stuck files are reconsidered. Gemini without `lastUpdated` can notice later edits. No adapter or production corpus was ingested in this assessment. |
| Reads/doctor/backup (#34, #38, #42, #43, #45, #47, #48) | WAL-aware read connections, per-thread doctor connections, conservative backup behavior, reduced read-open overhead, and architecture routing/shared-support ratchets. No new production repair is implied. CI uses setup-uv v7. |
| Test isolation (#40) and preservation | Pytest owns passive-update suppression; dedicated enabled tests fake IO/threads; Prysk transcripts explicitly suppress passive checks. Historical ingest/CLI/package-split drafts are preserved, not newly ratified or implemented. |

Sources: [replacement](../../src/siftd/storage/replacement.py),
[merge](../../src/siftd/api/merge.py), [receive](../../src/siftd/api/receive.py),
[read-only connections](../../src/siftd/storage/sqlite.py), and the relevant
folder READMEs and regression tests. Issue numbers identify changelog/source
provenance, not a fresh audit of GitHub issue status.

## Compatibility and operator caveats

1. **Repairs are not retroactive.** Already lost owners do not reappear merely
   on upgrade; the changelog prescribes re-push to restore ownership. Already
   lost tags cannot be reconstructed by the carry snapshot. Existing aider
   local-time `started_at` values persist until their source changes and is
   re-ingested. Same-second omissions are picked up on the next sync; this is
   not a general historical-data migration. No production repair was attempted.
2. **Block tags still do not survive replacement.** `ConversationCarryover`
   counts and warns about dropped block assignments but does not re-point them.
   Event tags without external IDs, or whose matching event is absent in the
   replacement, also cannot be restored. Empty parses report held/lost metadata.
   Warnings are not a loss-prevention policy. The source's future-version note
   is not a release promise or permission to implement identity/migration work.
3. **FTS compatibility knobs are accepted but ignored on merge/receive.**
   `--no-fts`, the API `rebuild_fts` argument, and `serve.fts_rebuild` no longer
   disable indexing received content or request an extra full-corpus rebuild.
   This does not mean `db slice --no-fts` is ignored. For existing index drift,
   use the documented `siftd doctor fix` or `siftd ingest --rebuild-fts` repair
   workflow under separate operator authorization. API removal is tracked in
   #74; no removal version is established by this report. CLI merge/receive
   help still says “Skip FTS5 index rebuild,” despite the no-op behavior;
   config/API docs and Unreleased are more explicit. Resolve that communication
   gap and compatibility/removal policy before declaring readiness; no source
   or help-text fix is smuggled into this documentation candidate.
4. **WAL correctness trades away the old no-sidecars promise.** Plain `mode=ro`
   readers used by query/search/show/doctor can create persistent `-wal`/`-shm`
   files in writable directories. Do not interpret these alone as corruption or
   delete live sidecars. Truly read-only media may use the guarded immutable
   fallback; a WAL containing frames or a hot journal can cause refusal rather
   than a complete-looking stale backup. The WAL refusal is deliberately
   conservative, including potentially already-backfilled frames.
5. **Error/time contracts change deliberately.** Ambiguous event prefixes now
   require a longer ID (HTTP 400 with structured candidates); invalid serve
   `since`/`before` now return 400. Naive timestamps' displayed/filtered meaning
   changes to UTC. Dry-run merges may now fail foreign-key checks they formerly
   skipped. Consumers must not rely on the former silent-success behavior.
   The removed internal `EmbeddingsConnection`/`siftd_immutable` machinery is
   also recorded in Unreleased; third-party internal imports need assessment.
6. **No CLI redesign or package split ships by implication.** Historical
   ratified sub-decisions recorded in the CLI draft retain their provenance,
   but neither the whole draft nor its proposed aliases/expiry ledger is current
   implementation. No identity implementation or metadata-loss policy is
   authorized here. Any later breaking rename needs the actual compatibility
   ledger and stated removal version, not a draft as a substitute.

## Earlier local verification

Both completed commands ran in **this worktree**, macOS arm64, **Python 3.12.12**,
an owned `.venv`, dev/serve/embed extras, disposable HOME/XDG state, and a private
copy of the existing dependency cache. Tests used
`FASTEMBED_CACHE_PATH=/tmp/siftd-next-slices-01a0ddb3/model-cache` and
`HF_HUB_OFFLINE=1`; dependency sync was offline after provisioning.
Caller-global `SIFTD_NO_UPDATE_CHECK` was **unset**. No remote embedding provider
was called. No source/test/lock changes or generated-doc drift resulted.

| Command | Exact exit | Observed result |
| --- | --- | --- |
| `./dev check --all -v` (signal-corrected runner) | **0** | Lint; architecture 94 passed / 5 skipped; base 4,042 passed; serve 359 passed / 4 collection skips; embeddings 111 passed; slow 2 passed; strict generated-doc gate passed. |
| `./dev test-all -v` | **0** | **4,608 passed, 5 skipped**. All optional dependencies installed together. |

The five full-suite skips are pre-existing session-strategy cases excluded from
`test_file_strategy_yields_at_most_one_conversation`: aider minimal/multi,
antigravity minimal, Gemini minimal, and OpenCode tool-use. The harness's serve
sync installs only dev/serve extras; its four collection skips are distinct from
the five full-suite skips. No tests were newly skipped, weakened, or deleted.

**Unsuccessful setup/execution attempts are retained, not hidden:** initial
uncached dependency provisioning hit the tool's 200-second timeout (no command
exit status returned); private cached provisioning then exited 0. The first
background/nohup check stalled at
`tests/test_follow.py::TestFollowSession::test_on_turn_with_invalid`, whose helper
uses self-SIGINT. Asynchronous shell execution inherited ignored SIGINT; this
runner mistake was terminated with SIGTERM, exact check exit **143**. It is an
incomplete run, not a pass. A separate foreground launcher explicitly restored
SIGINT's default disposition before the successful full check and test-all.
No test/code edits or selective reruns were used to obtain green.

Logs, command/subject/exit files, and interruption details are under
`/tmp/siftd-next-slices-01a0ddb3/logs/release-evidence/`:
`check-all.*` (interrupted), `check-all-signal-corrected.*`, `test-all.*`, and
`provision-*`. Completed check: **14:28:51–14:30:00 UTC**; test-all:
**14:30:00–14:30:34 UTC**, both on the exact commit/tree above.

### Current candidate normalization verification

The new `./dev check --all -v` completed with **exit 0** at
**14:50:28–14:51:23 UTC** on 2026-09-26 in this worktree. Its HEAD was
`4b00195f873bbb3f27997c8405070e8868cc02d2` (tree
`019f4d342772c03086cea11a736fdffa921f4c14`), with the two scoped policy/report
edits staged as tree `75c5dc30cce4c21369d6ce611cb57a3988baecb3` and no unstaged
changes. Lint, architecture **94 passed / 5 skipped**, base **4,042 passed**,
serve **359 passed / 4 collection skips**, embeddings **111 passed**, slow
**2 passed**, and strict generated-doc verification passed. No generated drift,
source/test/lock edits, or new skips resulted.

This run reused this worktree's owned **Python 3.12.12** `.venv`; dev/serve/embed
extras were provisioned offline from a private copy of the existing cache.
Each foreground command received a fresh allowlisted environment, disposable
HOME/XDG and TMPDIR **outside every Git checkout**, with SIGINT and SIGTERM
explicitly restored to default before exec. Caller-global `SIFTD_NO_UPDATE_CHECK`,
`SIFTD_DB`, `SIFTD_CONFIG`, `PYTHONPATH`, and `PYTEST_ADDOPTS` were absent.
The same local model cache and `HF_HUB_OFFLINE=1` were used; no remote embedding
provider was called. No old lane runner was sourced.

Evidence is under
`/tmp/siftd-next-slices-01a0ddb3/astra-review-wave/release/check-all/`:
`receipt.json` records exact command/exit, HEAD and index tree before/after,
environment and log hash; `output.log` and staged/unstaged patches are retained.
Only this report's evidence text follows that all-lane run. The final receipt
records tree comparison against the committed candidate to make **runtime-tree
equivalence**, rather than post-commit all-lane execution, explicit. A separate
`./dev check -v` at the exact final committed HEAD must pass, with a clean
post-check worktree, before the receipt can mark the candidate ready for review.
Neither check constitutes Astra approval. Earlier interrupted-run evidence
remains intact and is not reclassified as success.

### Coverage is local, not CI or browser certification

`.github/workflows/ci.yml` defines Ubuntu base tests/lint on Python **3.12, 3.13,
3.14**, and separate Python 3.12 embed, serve, and docs jobs. Slow tests are
configured only for reusable `workflow_call` (publication), not ordinary PR CI.
Local lint auto-fixes while CI checks; no autofix diff occurred here. This run
proves neither Linux nor the 3.13/3.14 matrix, and claims no fresh GitHub CI result.

`./dev check --all` and `./dev test-all` do **not** run T3 real-browser smoke.
T1/T2 CSP and TestClient tests are not browser JS execution. No CI job invokes
T3 and no browser smoke ran here. Accumulated serve changes include date-filter
HTTP/HTML behavior and rendered time labels, but not a new CSP/JS policy; an
explicit browser-smoke applicability decision/evidence remains for release
review. See [browser testing](../guides/serve-browser-testing.md).

## Review provenance and outstanding gates

[Exact-diff provenance receipt](reviews/test-isolation-exact-diff.json) rechecks
actual `is_error=false`, `modelUsage: claude-fable-5-1`, **Approved. No blockers.**,
and `--effort high` in the recorded command. The final diff extracted between the
prompt's delimiter lines equals `git diff 4552bac..53d0b9c`, trimming only trailing
newlines, SHA256:
`7ac9b09a6a154f4031362650b753802ff79aad9dd447bc92d81aa406b5ab1e6e`.
Historical final check-all/test-all logs postdate the final reviewed additions;
both exited 0, with test-all 4,608 passed / 5 pre-existing skips.

The later `reviews/test-isolation-fable-1` request returned 429/quota error,
`is_error=true`, no actual modelUsage: **not approval, not a code defect, and not
revocation of the earlier exact-diff approval**. Raw review payloads remain
outside the repository; the provenance record contains hashes, paths, and a
short verdict only. No reviewer, quota poll, credential extraction, or substitute
review was invoked here.

**Material HOLDs before publication readiness can be claimed:**

- **Astra 6 review of this new candidate via actual Codex CLI is PENDING.**
  The user authorized the temporary substitution: `/opt/homebrew/bin/codex`,
  model `gpt-6-astra`, reasoning **high**, as recorded in
  `/tmp/siftd-next-slices-01a0ddb3/checkpoints/reviewer-policy.json`.
  No review or quota poll is performed during this normalization stage; actual
  Codex CLI review of the exact committed candidate belongs to the next stage.
  Future return to Fable requires established availability in a future wave;
  no Claude requests or quota polling occur in this wave. Historical Fable
  approval remains valid only for the exact test-isolation diff, not this report,
  preservation, accumulated runtime scope, designs, or combined integration.
- User scope/version/compatibility decisions remain open, particularly the
  documented block/synthetic-event metadata limitation and misleading legacy
  FTS help/removal policy. Local green does not ratify accepting metadata loss.
- No fresh CI matrix evidence or T3 browser execution/applicability disposition
  for this subject is attached. Re-run the harness on the eventual integrated
  main tree; independent branch green is not transitive across merges.
- No permission to push, tag, bump, publish, mutate GitHub/loops/production, or
  reinstall user tools was given. All remain unperformed and user-controlled.

Current machine-readable candidate receipt:
`/tmp/siftd-next-slices-01a0ddb3/astra-review-wave/release/candidate.json`.
The earlier `/tmp/siftd-next-slices-01a0ddb3/checkpoints/release-candidate.json`
is retained as historical evidence; its prospective Fable-only labels are
superseded by the user's temporary Astra authorization, not by a new approval.
This is a **locally tested documentation candidate on HOLD**, not a declaration
that publication or substantial new work's mandatory review gate is complete.
