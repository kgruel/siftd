# Ingest design R2: durable review and disposition

Recorded 2026-09-26. **PENDING ASTRA REVIEW — safeToImplement=false.**
This candidate responds to a real **REVISE**, not an approval. The verbatim
review preserved below describes `4552bacf`, not this revised candidate.
Historical R1 is unchanged, including its duplicated findings.

## Provenance verified this run

- Reviewed base: `58daf4ac0101a012ff0ea286931fce8e2e621110`.
- Reviewed candidate/preservation: `4552bacf60ec6ac6a7b8b40334bc8c1e1d1bfb0c`.
- Full original: `/tmp/siftd-next-slices-01a0ddb3/reviews/ingest-r2-corrected-isolation/review.md`.
- Raw result: `raw.json` in that directory; SHA256
  `daed2ea699926b54739e1f0fdd3f641526fcb19cf2dfbdb78178dd5de0b9173e`.
- Raw `modelUsage` names `claude-fable-5-1`, canonical model identical,
  provider `firstParty`; `is_error=false`, permission denials `[]`.
- Recorded invocation (`command.json` / `command.sh`): requested
  `claude-fable-5-1[1m]`, `--effort high`, plan mode, Read/Glob/Grep only.
  Recorded process exit is 0. Effort is invocation evidence, not inferred from
  the model name. Raw `structured_output` exactly equals `assessment.json` and
  its verdict is `revise`.
- Session: `2562b87a-8c87-44f7-b4f0-7ccf3beba080`. R2 ran no tests.
- No Claude invocation, quota poll or credential extraction occurred here. New
  review is pending actual `/opt/homebrew/bin/codex`, model `gpt-6-astra`,
  reasoning high, under the user-authorized temporary policy. Historical Fable
  attribution is unchanged. The approved test-isolation commit `53d0b9cb` was
  subsequently merged by ancestry as a validation prerequisite, not copied or
  re-reviewed; that approval does not approve this design diff.

## Disposition, independently checked rather than adopted wholesale

| R2 topic | Disposition / evidence |
|---|---|
| NULL-key and block tag loss | **Accepted, material blocker.** `replacement.py::snapshot_conversation` counts NULL-key event assignments instead of retaining them; block assignments are all excluded. `restore_conversation` only rejoins retained keys. Disposable fixture replacement loses prompt, response, exchange and block assignments; conversation/tool assignments and ownership survive. New keys assigned after snapshot cannot recover old assignments. |
| pi_agent coverage and namespace | **Accepted with qualification.** Current prompt/response constructors omit external_id; tool calls use raw call IDs. Propose prefixed record IDs for emitted prompts/responses only, pending valid-ID/duplicate policy. Minimal fixture has three distinct candidates. Historical 45/45 is presence, not uniqueness. |
| “each message emits exactly one stored event” / acceptance (a) | **Corrected.** toolResult emits no separate event, a toolCall-bearing assistant contributes a response plus tool event, and pre-prompt assistant responses are dropped. State the rule per emitted kind. A synthetic same-kind duplicate record has an ID on every record but colliding candidate keys. |
| Bounded sentinel invalidation | **Mechanism verified; sufficiency rejected.** `_stat_unchanged` and `_hash_unchanged` can both be bypassed. The UPDATE has no completion/version/coverage guard. Two invalidate→ingest cycles in the probe both replace and remint, contradicting acceptance (c)'s second-invalidation no-op without an additional mechanism. NULL-only coverage heuristics also fail on legitimate ID-less records. |
| Duplicate sources | **Guard retained; coverage claim qualified.** `_conversation_claimed_elsewhere` is refusal, not election. Two rows linked to one conversation cause BOTH paths to settle without replacement. Probe leaves all three prompt/response keys NULL despite settled real hashes. Invalidation cannot promise every affected file re-parses. No unlinking/cleanup is authorized. |
| Positional carry suggestion | **Not proved, not selected.** Unchanged bytes and a key-only parser diff do not establish stored-event/source-order correspondence. Old parser/config, filtering, omitted records, timestamps, ties, repeated equal content and duplicate sources matter. Changed/append files are outside an unchanged-byte bridge. Probe inserts an earlier user record and demonstrates ordinal zero now denotes different content. Refuse ambiguity; do not zip timestamp/ULID ordering to source order. |
| Conversation/owner/keyed tool carry | **Accepted conditionally.** A replacement must exist and the tool key must survive. Empty parses lose metadata; changed tool IDs can leave unmatched tags. All block tags remain separate loss, including blocks on keyed parents. |
| Transaction/ULID/order/embedding caveats | **Accepted as scope limits.** Existing replacement commits store/restore/bookkeeping together and rollback protects failures; it does not make a prior separately committed invalidation atomic. Fresh ULIDs can reorder timestamp ties without any SQL diff. Re-embedding cost is a historical/source-inspection caveat, not a benchmark run here. No ULID-stability claim for re-parse. |
| Graph-upsert, NULL-scope attributes, content uniqueness, FTS, semantic parents | **Deferred, not authorized.** Source still has plain root/child insertion, nullable-scope conflict weakness and insert-only FTS. Existing semantic parents must remain unchanged. None of this should be bundled into extraction. |
| Narrow scope / historical evidence | **Accepted.** pi_agent only; other adapters, K2/K3, ordering/schema, live cleanup and #83 deferred. Prior corpus counts and prior failed check are historical, not newly verified or green. “Decided” locator/block positions were author proposals, not user ratification. |

The migration choice is **not** “accept loss unless proven otherwise.” Recommend
read-only impact/preflight before selecting a lossless transition. In-place
assignment of genuine IDs or a strict carry bridge each requires unambiguous
mapping and atomic refusal; fresh-DB-only extraction requires a genuine legacy
replacement barrier. None is authorized. See
[identity-first plan](ingest-identity-first-slice.md) for precise gates and
[revised design](ingest-stream-design-2026-08-13.md) for the corrected chronology.

## Original candidate attempt: verification and limitations

Original artifacts remain under
`/tmp/siftd-next-slices-01a0ddb3/logs/design-disposition/`:

- `isolated.sh`: exact environment wrapper; local worktree `.venv`, Python
  3.12.12, installed dev/serve/embed extras, disposable HOME/XDG, shared public
  local model cache with `HF_HUB_OFFLINE=1`, caller-global
  `SIFTD_NO_UPDATE_CHECK` unset. No remote embedding provider or production
  ingest was used. No source/test repair was made.
- `probes.py`, `probes.json`, `probes.stderr`, `probes.exit`: current-code
  disposable tests described above, exit **0**. The probe implements no new key
  extractor/migration. It tests old replacement loss, sentinel cycles, duplicate
  refusal and source candidate/ordinal counterexamples. It is not a full safe
  transition proof; ID-less, tie, rollback and concurrency cases are future
  acceptance requirements, not passing evidence here.
- `dev-check.log`, `dev-check.exit`: `./dev check -v`, exit **1**. Lint passed;
  architecture **94 passed / 5 skipped**; base **4033 passed / 8 failed**.
  The harness stopped before its docs step. The local wrapper mistakenly put
  TMPDIR inside the Git worktree. Non-Git fixture directories then inherited the
  parent repository, explaining the eight Git/workspace-related failures below.
  This is a **candidate-run isolation error**, not an ingest regression or a
  claim that these eight failures are the previously known notice/cache issue.
  The failed attempt is retained as failed. The corrected validation below uses
  temporary test directories outside every Git checkout and the merged, reviewed
  suite-owned isolation guard; it does not rewrite this attempt as green.
- `docs-check.log`, `docs-check.exit`: separate `./dev docs --check`, exit **0**;
  generated references/README spans inspected, no generated diff.
- Optional serve/embedding/slow lanes were not run in that attempt: no source/test behavior changes;
  the relevant base/architecture lanes were attempted above. Installing extras
  for strict docs/base imports is not execution of their optional lanes.

Failed nodes (exact):

```
tests/test_git.py::TestResolveWorktreeToMain::test_non_git_directory_returns_none
tests/test_git.py::TestGetCanonicalWorkspacePath::test_regular_path_unchanged
tests/test_git.py::TestGetCanonicalWorkspacePath::test_non_git_path_unchanged
tests/test_peek.py::TestListActiveSessions::test_workspace_name_disambiguation
tests/test_storage.py::TestMigrateWorkspaces::test_backfill_git_remotes_no_git
tests/test_workspace_identity.py::TestGetOrCreateWorkspaceWithGitRemote::test_creates_workspace_without_git_remote
tests/test_workspace_identity.py::TestGetOrCreateWorkspaceWithGitRemote::test_updates_existing_workspace_git_remote
tests/test_git.py::TestGetGitRemoteUrl::test_returns_none_for_non_git_directory
```

That original attempt has no green complete check. Corrected validation is
recorded separately below. Completion/version guards, lossless graph/block
mapping and duplicate authority remain unresolved; `safeToImplement=false`.

## Corrected validation — Astra candidate normalization

The user authorized temporary review by actual `/opt/homebrew/bin/codex`, model
`gpt-6-astra`, reasoning high. This preparation invokes no reviewer: approval
remains pending the next stage, not supplied by the preparing assistant.
Historical R1 and the verbatim Fable R2 below are unchanged.

Only the already-reviewed test fix `53d0b9cb4de553bc003af61d02942c9cfa03bcd2`
was merged into this branch, with both parents retained by merge commit
`cee67134c271d409a44033e318f62511149c838e`. The new-review base is `53d0b9cb`;
the delta is design documentation only. No identity implementation, additional
source/test changes, migration or metadata-loss acceptance was introduced.

New artifacts live under
`/tmp/siftd-next-slices-01a0ddb3/astra-review-wave/design/`:

- `original-attempt/` retains copies and `original-attempt.json` records hashes
  of the old failed check and wrapper; the originals remain untouched. The old
  TMPDIR was inside this checkout. Its eight failures show fixture paths resolving
  to the parent repository, inherited Git remote, or collapsed workspace names.
  Each new command receipt verifies its temp root lies outside **every** listed
  worktree and `git rev-parse` there exits 128, not a parent repository.
- `run.py` starts commands in this worktree, using its own Python 3.12.12 venv
  and fresh disposable HOME/XDG/TMPDIR under the external artifact directory.
  The environment is built afresh, without caller-global `SIFTD_NO_UPDATE_CHECK`,
  `SIFTD_DB`, `SIFTD_CONFIG`, `PYTHONPATH` or `PYTEST_ADDOPTS`. Tests own the #40
  suppression. SIGINT/SIGTERM are reset to default before child exec and the
  parent waits in the foreground. Local weights use the existing shared model
  cache with `HF_HUB_OFFLINE=1`; no remote embedding provider is called.
- `check-all.log/json`: first corrected-environment attempt was interrupted by
  the command harness's 120-second timeout during embedding dependency install.
  Architecture (94 passed / 5 skipped), base (4042 passed), and serve (359 passed /
  4 skipped) had passed. No child exit was captured; this is **incomplete**, not
  an exit-0 result. The log and incomplete receipt remain.
- `check-all-corrected.log/json/exit`: longer-timeout `./dev check --all -v`,
  exit **0**. Architecture 94 passed / 5 skipped; base 4041 passed / 5 skipped;
  serve 359 passed / 4 skipped; embeddings 111 passed; slow 2 passed; docs passed.
  The interrupted serve lane's dependency sync had removed embedding extras,
  so this attempt began with dependency-gated base skips. These are disclosed,
  not used to stand in for full-extra base coverage.
- `check-all-full-extras.log/json/exit`: after the prior run restored all extras,
  `./dev check --all -v`, exit **0**. Architecture **94 passed / 5 skipped**;
  base **4042 passed (no skips)**; serve **359 passed / 4 skipped**;
  embeddings **111 passed**; slow **2 passed**; lint and strict docs passed.
  All eight original failed nodes pass in the base log. The architecture skips
  are existing non-file-strategy cases; the serve harness intentionally syncs
  dev/serve extras, with embedding-dependent modules covered in the next lane.
  No tests or skip conditions were edited, weakened or removed.

The full-extra run subject was merge HEAD `cee67134`, HEAD tree
`4b8a530840bd3880856ac5fb4efcf7bbca51c54a`, with staged documentation tree
`4802e8549840df77c321c798972805a3d2ce6574` (unchanged across the run). Only this
validation report text follows that run. Runtime-tree equivalence to the final
candidate is checked explicitly: source, tests, scripts, dependency metadata and
all other non-design-doc paths must be identical. The external `candidate.json`
records the final exact commit/tree, equality checks and the post-commit
`./dev check -v` exit/log at that exact clean HEAD; it is not a review verdict.

A green existing suite validates candidate preparation, **not** future identity
semantics. `safeToImplement=false`: preflight surface/authorization, valid-key and
duplicate rules, lossless event/block correspondence, duplicate-source authority,
and durable completion/retry/rollback still require decisions and proof.

## Original R2 record (complete, verbatim)

The following is preserved review evidence, **not** endorsement of every
recommendation. In particular its “smallest correct invalidation” and “second
invalidation is a no-op” require the qualifications above.

---

# Independent ingest design R2 — Claude Code

- Candidate: `4552bacf60ec6ac6a7b8b40334bc8c1e1d1bfb0c` (detached, unchanged)
- Reviewer model: `claude-fable-5-1` (verified in modelUsage)
- Requested model: `claude-fable-5-1[1m]`; effort: `high` (exact invocation in command.json / command.sh)
- Process exit: 0; is_error: false; permission denials: none
- Test execution: none in this read-only bridge/review; supplied failed check is not green
- Raw response: `/tmp/siftd-next-slices-01a0ddb3/reviews/ingest-r2-corrected-isolation/raw.json`
- Verdict: **revise**

## Claude summary (verbatim)

Independent R2 of docs/dev/ingest-stream-design-2026-08-13.md against candidate HEAD 4552bacf (docs-only diff over base 58daf4ac; code is identical to base). The design's R1 disposition is accurate against current code: every R1 code claim I could check statically still holds (skip paths at orchestration.py:445/452, no parser revision in ingested_files at schema.sql:151-161, mint-and-insert writers at sqlite.py:2858-2927 and events.py:99-192, NULL-scope attribute upsert at attributes.py:23-27, insert-only FTS at fts.py:217-228, semantic parent_id readers at storage/events.py:270-318 and queries.py:140-144, pi_agent fixture line 7 pointing at a folded toolResult). The design is sound as an architecture document and correctly separates K1 key extraction from graph upsert. It is NOT yet executable as a smallest K1 slice for pi_agent, for one material reason: the slice needs a re-parse of unchanged pi_agent files, and the current replacement carry (storage/replacement.py:156-171) drops every prompt/response/exchange element tag whose event has a NULL external_id, which is exactly the population being keyed. The migration section says "sources must be re-parsed by fixed adapters" without stating or gating that loss. Secondary revisions: the historical Slice 1 bundles three K1 adapters, K2/K3 construction, and a live-DB residue sweep; the smallest viable slice is pi_agent-only with a harness-bounded invalidation and no schema change. Test evidence: the supplied dev-check log shows exit 1 with 2 failures (tests/cli/test_upgrade.py::TestNotice::test_notice_when_newer, tests/test_findability_review.py::test_hybrid_rrf_surfaces_keyword_hits_with_empty_index), both environment-related and unrelated to ingest; 4039 passed, architecture 94 passed / 5 skipped, docs-check exit 0. Nothing here is a green result and no tests were run by this review. Live-corpus counts in the design (G3, G5, G8, G9) are historical claims, not verified here.

## Claude findings (verbatim)

[1 Key uniqueness, pi_agent] What pi_agent can key on today, from src/siftd/adapters/pi_agent.py and tests/fixtures/adapters/pi_agent/minimal/input.jsonl: each `message` record carries a record-level `id` (fixture lines 4-7) and emits exactly one stored event of one kind (role user -> Prompt at pi_agent.py:127-131; role assistant -> Response at 133-184), so record `id` is unique per stored event within (conversation, kind) for the fixture. Tool calls are already keyed on the toolCall block `id` (pi_agent.py:165, 200) and never on the record id, so no cross-kind collision arises and the UNIQUE (conversation_id, kind, external_id) at schema.sql:232 is satisfied. toolResult records (186-203) patch an existing ToolCall and emit nothing; session/model_change/thinking_level_change emit nothing; no fabricated key is needed. Missing `id` must map to None (NULL is not constrained by the UNIQUE), never to a synthesized value. The design's G4 row says pi_agent is 0% keyed; that is true for prompt/response but tool_calls are already keyed (design G3 agrees: tool_calls 100% keyed overall). Fix G4 to say per-kind coverage. Fixture-level uniqueness is the only evidence available here; the design's 45/45 live figure is a historical claim.

[1 Key namespace] Existing convention is harness-prefixed message keys and raw tool ids: claude_code.py:373/438 and gemini_cli.py:139/166 write f"{NAME}::{id}" for prompt/response while tool calls use the raw id (claude_code.py:364, gemini_cli.py:213, pi_agent.py:200). J4 (prefix stripping) is an open call in the design, but the slice must pick one; recommend `pi_agent::<record id>` for prompt/response to match the two keyed adapters, and leave tool_call keys untouched. Not a blocker, a required slice decision.

[2 NULL-key events and tag preservation, MATERIAL] The slice's own re-parse triggers delete-then-insert through _take_conversation_for_replacement (orchestration.py:892-923). snapshot_conversation (storage/replacement.py:156-171) can only re-join event tags by (conversation_id, kind, external_id); rows whose event has NULL external_id are counted in dropped_events and lost, with a warning at replacement.py:288-293. For pi_agent every existing prompt/response event has NULL external_id (design G3, and the adapter never sets one), so every prompt/response/exchange tag on pi_agent conversations would be dropped by the very re-parse that introduces the keys; block tags are dropped too (dropped_blocks, replacement.py:177-188). Conversation tags, ownership rows, and tool_call tags carry. The design's Migration section ('sources must be re-parsed by fixed adapters') does not state this loss or gate it. Required before the slice: (a) a dry-run report counting affected files, conversations, and NULL-key element tags per harness before any invalidation; (b) an explicit choice between accepting the reported loss or adding a re-parse-only positional carry (sound only when bytes are unchanged and the parser change is key-only, so emission order is deterministic), documented as a decision rather than left implicit. Do not conflate this with slice 2's 'ULIDs stable' gate: under the current door, ULIDs are always re-minted (sqlite.py:2864, 2880, 2924), and stability is a graph-upsert property, not something the K1 slice can provide.

[3 Unchanged-file re-parse] Confirmed: file strategy skips on stat at orchestration.py:445 and on hash at 452; session strategy at 518 and 523; ingested_files records no parser revision (schema.sql:151-161). An adapter-only key change is inert for existing files. Two mechanisms that already exist can drive a bounded, non-blanket invalidation without schema v13: _stat_unchanged returns False when file_mtime is NULL (orchestration.py:139-144) and _hash_unchanged returns False when file_hash does not match (147-155), and link_ingested_file already uses an empty-string hash as a never-matching sentinel (orchestration.py:682-688, sqlite.py:3246-3280). A harness-scoped UPDATE of ingested_files (file_hash sentinel, file_mtime NULL, WHERE harness_id = pi_agent AND error IS NULL) routes every affected file through the ordinary re-ingest branch (459-502), which is guarded, snapshots, and settles duplicate paths via _settle_duplicate_path (832-869). That is the smallest correct invalidation. A durable parser_revision column is the better long-term answer but is deferrable; if chosen, it must feed both _stat_unchanged and _hash_unchanged, and record_ingested_file/link_ingested_file/record_session_file must stamp it. Either way the invalidation must be user-invoked, idempotent, and report counts; this review authorizes neither against production data.

[3 Transaction bookkeeping] _reingest_file commits once after store, restore, record_ingested_file, and pending-tag drain (orchestration.py:1072-1087); failures roll back and keep a resolving pointer (_record_file_error 739-801). rebuild_rollups runs once per ingest_all (728-730). The embeddings incremental index prunes chunks whose conversation id vanished and re-embeds changed conversations (embeddings/indexer.py:193-205, 266-271); because replacement mints a new conversation ULID, a harness-wide re-parse forces re-embedding of every pi_agent conversation. Not a correctness issue, but the cost belongs in the slice's caveat.

[4 Graph idempotency, deferred to slice 2 with evidence] Not needed for a K1 slice that keeps delete-then-insert. Confirmed by reading (not execution): events insert is a plain INSERT (storage/events.py:110-114); event_content is INSERT under UNIQUE (event_id, block_index) (events.py:145-149, schema.sql:294); set_attribute upserts ON CONFLICT (target_kind, target_id, key, scope) with nullable scope (attributes.py:23-27), and tool-call attributes are written with scope NULL at sqlite.py:3069 while conversation/response attributes use 'analyzer'/'provider' (2999, 3042), so under SQLite NULL-distinct semantics the tool-call path cannot upsert; insert_fts_content is insert-only (fts.py:217-228) with a scoped rebuild available (136-185); blob refcounts are maintained by triggers on delete/update of event_tool_call (schema.sql:262-285) and store_content on insert. All of these are exactly what the design's G7 and the write-path sketch say; none is touched by a key-only slice. Later work, in order: attributes.scope fix, event upsert with RETURNING id, event_content reconcile, FTS replace, blob adjust, vanished-descendant delete, rollup rebuild.

[5 Source authority and duplicate sources] _conversation_claimed_elsewhere (orchestration.py:811-829) is consulted only inside _take_conversation_for_replacement (919) and the collision repair (662); INGEST_REPLACEMENT_DOORS pins its three call sites (tests/architecture/test_replacement_carry.py:164-171) and DESTROY_SITES pins the delete population (145-156). A K1 slice that changes only the adapter and adds a harness-scoped bookkeeping invalidation leaves all of these unchanged, which is the R8 gate. Owner rows are carried by copy (replacement.py:242-248). The design's G9 (keys shared across conversations) is a live-corpus claim not verified here, but the per-conversation UNIQUE means it does not affect the slice.

[6 Source parent vs semantic parent] Confirmed: storage parent_id is semantic (response->prompt via insert_response prompt_id at sqlite.py:2881; tool_call->response at 2925), and readers depend on it (storage/events.py:280, 315; queries.py:142-144; api/events.py:175). pi_agent fixture line 7 has assistant a1b2c3d9 with parentId a1b2c3d8, a toolResult that pi_agent.py:186-203 folds into a ToolCall and never stores. A K1 slice must set only external_id and must not touch parent_id; source_parent provenance is slice 3 work. Records folded or ignored by pi_agent today: toolResult (patch), toolResult with no pending call (silently ignored, 193), assistant before any prompt (silently dropped at 183-184, the same G2 shape the design attributes only to claude_code.py:457; copilot_cli.py:172-173 has it too). A keyed-but-dropped response is a citation gap; R7 should enumerate these per adapter, but fixing G2 is an independent change, as the design says.

[7 Ordering] The design's G5 inventory matches the current tree: timestamp-only at queries.py:95/106/112/391/442/482/492, api/search.py:553/583/603/629/713, storage/events.py:208/281/316, api/file_refs.py:84, embeddings/chunker.py:398, fts.py:515; (timestamp, id) at api/target_ref.py:216, api/events.py:176, storage/events.py:261, sqlite.py:1628; DESC variants at storage/events.py:232, api/events.py:277/284, api/export.py:249, api/search.py:1783; parent_id-first at queries.py:144. A key-only slice changes no ordering column and no read SQL, so read-site semantics are unchanged in text. One disclosure the design lacks: ids.py:25-99 generates ULIDs with random low bits inside a millisecond, so re-minting on replacement can reorder events that tie on timestamp at the (timestamp, id) sites, including get_prompt_by_index (storage/events.py:257-267) used by exchange-index pending tags. This is pre-existing on every replacement today and is not introduced by the slice, but a harness-wide re-parse applies it at once; note it as a caveat and defer the fix to the sequence/ordering decision (J10).

[Scope: smallest viable K1 slice] The historical Slice 1 (design lines 441-451) bundles pi_agent + copilot_cli + opencode extraction, K2/K3 construction for antigravity/codex under undecided J2 declarations, and a cline/cursor/goose harness-row sweep against the live DB. The smallest K1 slice is: pi_agent prompt/response external_id from record id (prefixed), regenerated golden fixture, a fixture-level uniqueness ratchet, a harness-bounded invalidation with dry-run report, and the tag-loss decision from finding 2. Deferred with reasons: copilot_cli and opencode (same shape, separate slices after pi_agent proves the invalidation path; copilot fixture confirms record ids are present at tests/fixtures/adapters/copilot_cli/minimal/input.jsonl lines 3/5/9 but copilot_cli.py:130/139 never reads them); antigravity/codex K2/K3 (need J2 stability/mutability declarations first); harness-row residue sweep (production mutation, unrelated to keys); schema v13 sequence/source_hash/parser_revision (slice 3 or the durable-invalidation variant); all of slice 2's graph upsert; the 'Decided' section (locator grammar, cross-owner 404, block anchoring) which is author position, not ratification, and is not exercised by a K1 slice.

[Acceptance gates for the pi_agent K1 slice] (a) Adapter unit test: every fixture message record with an id yields exactly one event carrying pi_agent::<id>, records without id yield external_id None, tool_call keys unchanged; expected.json regenerated and reviewed. (b) Architecture ratchet (R9 at fixture level): for every adapter fixture, emitted keys are unique within (conversation, kind); per-adapter keyed-kind set is a declared population where pi_agent moves from {tool_call} to {prompt, response, tool_call}; live-corpus R9 cannot be a CI gate and should be a doctor report. (c) Ingest behavior test over a temp DB: ingest fixture, apply a conversation tag, an owner row, a tool_call tag, and a prompt tag; ingest again unchanged -> skipped; run the bounded invalidation -> replaced through the guarded door; conversation tag, owner, and tool_call tag survive; the prompt tag outcome matches the decision in finding 2 and is reported, never silent; a second invalidation is a no-op. (d) Duplicate-path test: two paths carrying one pi_agent session; invalidation settles the loser via _settle_duplicate_path and never deletes the shared conversation. (e) DESTROY_SITES, REPLACEMENT_SITES, INGEST_REPLACEMENT_DOORS unchanged (tests/architecture/test_replacement_carry.py:78-171). (f) No diff to parent_id assignment, ordering SQL, FTS, attributes, or blob paths. (g) Dry-run of the invalidation prints file, conversation, and NULL-key element-tag counts before any write. (h) The design's G3 live-query 'gate' is replaced by fixture-level keyed-rate plus a doctor report; historical counts are not acceptance evidence.

[Test evidence and limitations] No tests were executed by this review. Supplied logs: dev-check.exit = 1; dev-check.log line 8362 reports 2 failed / 4039 passed, line 217 reports architecture 94 passed / 5 skipped; failures at lines 8360-8361 are tests/cli/test_upgrade.py::TestNotice::test_notice_when_newer (SIFTD_NO_UPDATE_CHECK=1 suppresses the notice) and tests/test_findability_review.py::test_hybrid_rrf_surfaces_keyword_hits_with_empty_index (HF_HUB_OFFLINE with empty model cache). dependencies.exit = 0, docs-check.exit = 0. preparation-result.json status is 'blocked'. Neither failure touches ingest, adapters, or storage, but the check is not green and this review does not call it green. All design statements marked measured (G3/G5/G8/G9 counts, 18,330 NULL-scope rows, 44,822 tied events) are historical live-DB claims outside this review's permitted evidence.

[R1 disposition check] All 17 R1 findings are accepted in the design and each code-level claim re-verified here still describes the current tree; only line numbers moved slightly (R1's orchestration.py:459/554 replacement calls are now 463/570; 811, 435/449 as 445/452, schema.sql:151/225/287/299, sqlite.py:2858/3001, fts.py:136/217, events.py:270, pi_agent.py:186, claude_code.py:309 as 312, codex_cli.py:335 all confirmed). The design's own 'verified/reproduced' labels remain the author's claims; nothing in the R2 disposition is rejected. The R1 document contains its findings twice (lines 10-125 and 130-245) and a trailing blank line, preserved byte-for-byte per the handoff; harmless.
