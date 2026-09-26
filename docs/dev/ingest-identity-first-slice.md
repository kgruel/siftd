# Ingest identity: smallest pi_agent K1 direction

Date: 2026-09-26. **PENDING FABLE REVIEW. safeToImplement=false.**

This is a plan, not implementation authorization or acceptance of metadata loss.
It follows actual Fable 5.1 High R2's **REVISE** of preservation `4552bacf`.
[Design/disposition](ingest-stream-design-2026-08-13.md) ·
[full R2 and verification](ingest-stream-review-r2-2026-09-26.md).
No new Fable review was requested; approval of any other exact diff does not
transfer to this candidate.

## Separate extraction from transition

The mechanically implementable extraction is small: in `adapters/pi_agent.py`,
set `Prompt.external_id` and `Response.external_id` from the enclosing message
record's valid nonempty `id`, proposed namespace `pi_agent::<id>`. Keep absent IDs
as `None`; never use a timestamp, ordinal, content hash or fabricated placeholder.
Malformed/empty IDs need an explicit validation/refusal rule before implementation;
do not stringify arbitrary JSON into identities. Keep raw toolCall block IDs,
conversation identity, content, usage, semantic parentage and dropped-record
behavior unchanged. `parentId` is provenance, not a response→prompt link.

The minimal checked-in fixture has one user and two emitted assistant records
with three distinct IDs; toolResult patches the existing `tc-001` tool. An
assistant with a toolCall can contribute both a response and a tool event.
An assistant before any user is currently discarded. Thus the gate is unique
keys **per emitted (conversation, kind)**, not “every message emits exactly one
event.” Presence in a fixture or the historical 45/45 count is not a durability
proof. Duplicate same-kind IDs need detection/refusal, not last-write-wins or
silent de-duplication. Cross-kind and cross-conversation namespaces differ.

Changing extraction alone is **not a safe rollout**: unchanged files stay skipped,
but the next natural append/change triggers replacement of old NULL-key rows.
Disabling only the proposed forced re-parse would not protect those old tags.
Tests on new databases can prove extraction without proving legacy migration.
The existing replacement door continues reminting ULIDs; no stable-ID or stable
ordinal promise belongs to this slice.

## Why migration is blocked

- `storage/replacement.py::snapshot_conversation` cannot rejoin old NULL-key
  event tags. Prompt/response/exchange assignments are lost before new IDs exist.
  Exchange is a tag target kind anchored on a prompt, not another event kind.
- All block assignments are excluded, including blocks of keyed events.
  Conversation tags and ownership copy when a replacement exists; keyed tool
  tags survive only when their event key is still present. These are different
  preservation properties, not evidence that all annotations carry.
- A pi_agent-scoped empty hash plus NULL mtime bypasses skip gates, but re-ingest
  restores real values. The next invocation repeats replacement. A sentinel is
  invalidation, not durable completion state. Do not call the whole operation
  idempotent merely because two UPDATEs before ingest yield the same values.
- A completion guard must distinguish successfully migrated, already covered,
  legitimately ID-less, failed, empty/missing, and duplicate/refused sources.
  “Any NULL remains” never completes ID-less inputs; “any non-NULL exists” misses
  partial migration. Hash/stat settlement is not event coverage.
- When two paths already claim a conversation, both can refuse replacement and
  settle hashes without parsing. Neither becomes authoritative by invalidation.
  Do not unlink/delete a path or bypass the guard to manufacture progress.
- Positional carry requires old stored graph ↔ old parse ↔ new parse bijections.
  Current-byte equality against a trustworthy prior hash is necessary, not
  sufficient: parser revision/config/filtering, omitted records, fallback times,
  repeated content, timestamp ties and ULID reordering can destroy correspondence.
  `ingested_files` has no parser revision and a duplicate settlement can stamp
  bytes that were never stored. A sentinel would overwrite useful old hash
  evidence. Changed/append/reordered/truncated files must not enter an
  unchanged-byte bridge. Block type/content must match, not just block index.

No safe lossless transition has been proved or ratified. Existing loss warnings
are disclosure, not consent. Historical database counts are not current impact
estimates, and no production corpus was examined here.

## Alternatives requiring a decision

| Direction | Benefit | Missing proof / trade-off |
|---|---|---|
| Read-only impact/preflight **(recommended next)** | Bounds the affected population without creating loss | Needs an explicit read-only contract and actionable unknown/refused categories; does not migrate anything |
| Verified in-place assignment of genuine source IDs | Could preserve existing ULIDs, tags, block IDs and ordering | Must uniquely map each stored event to its source record and reject conflicting/partial mappings atomically; no generic ordinal backfill |
| Strict key-only re-parse carry bridge | Could preserve assignments through existing replacement | Same mapping proof plus full block/tag carry, rollback and completion semantics; ULIDs still change, tied ordering and re-embedding costs remain |
| Gated fresh-DB-only extraction | Allows new-data contract tests while deferring old data | Must prevent every legacy replacement entry, including natural changes and mixed old/new data; not a transparent global adapter-only change |
| Explicit acceptance of measured loss | Avoids bridge complexity | **Not selected or authorized.** Requires a separate user policy decision; a warning/backup is not that decision |

A general parser-revision schema could later supply durable invalidation, but
cannot reconstruct missing old event identity or prove losslessness. A no-schema
coverage/completion scheme is also unproved. Decide that only after measuring
what can actually be mapped. Neither approach is part of this docs-only candidate.

## Bounded next step: propose a read-only preflight, then stop

Seek authorization for a pi_agent-only diagnostic (surface/API placement remains
a decision; no new CLI/package abstraction is needed now). It must:

1. Take an explicit database and explicit source scope; never discover production
   defaults implicitly. Open using the storage read-only helper, with automatic
   upgrade disabled (`open_database(..., read_only=True, auto_upgrade=False)`),
   no schema ensures, ingest, invalidation, pending-tag drain, model calls or
   embedding work. Missing/stale/unsupported schemas are reportable refusals.
2. Report files separately from distinct conversations and assignment counts.
   Group NULL-key tags by prompt/response/exchange/tool_call; count all block
   tags separately. Include keyed-tag unmatched risk, conversation tags,
   ownership, and any pending targets as distinct categories. Do not double
   count conversation/tag totals when multiple paths point to one conversation.
3. Enumerate unlinked bookkeeping rows, conversations with no path, error rows,
   unavailable files, empty/malformed sources, legitimate missing IDs, duplicate
   candidate keys, already-keyed/partially-keyed graphs, and multiple source paths
   (identical or divergent). `error IS NULL` is not a completeness certificate.
4. Compare current hashes with preserved bookkeeping evidence without changing
   either. Report changed and unverified separately from byte-identical; do not
   claim byte-identical means authoritative or historically equivalent. Compare
   parse shape/content to the stored graph read-only; ambiguities are findings,
   not inferred positional matches. Stable-source capture/recheck is needed to
   detect writes during inspection. No apply action may trust a stale preflight.
5. Produce deterministic, machine-readable counts/statuses and bounded reason
   samples without transcript content or secrets. Explicitly state report limits:
   this measures exposure, not migration approval; unknown is not zero loss.
   A diagnostic must not acquire new keys by mutating stored rows.

Stop after a disposable-fixture-backed report and review. Do not run against a
live DB without separate authorization. Return decisions about mapping/coverage,
not a flag that silently enables invalidation.

## Acceptance gates for future work (not implemented tests)

| ID | Scenario | Required assertion |
|---|---|---|
| P1 | Preflight of current, stale, absent and unsupported disposable DBs | Logical contents/schema/metadata/source bytes unchanged; no migration, SQL writes, pending drain, indexing or network; explicit refusal instead of writable fallback |
| P2 | Six tag kinds + owner on old fixture; two paths link same conversation | Exact counts: three NULL-key event assignments, one block assignment, one conversation and one tool assignment, one owner; files counted twice, conversation/assignments once; duplicate status explicit |
| P3 | Missing/error/unlinked/orphan/empty paths; legitimately ID-less, duplicate-ID and partially-keyed sources | Separate nonzero categories, no false “covered”; no writes even on parse/hash errors |
| P4 | Changed, appended, truncated, reordered and concurrently changing bytes; divergent duplicate copies | Read-only report of refusal/unknown; hash equality alone never authorizes correspondence; repeated reports over frozen state identical |
| K1 | pi fixture, missing IDs, multiple tools, folded toolResult, pre-prompt assistant | Only emitted prompt/response IDs change under agreed namespace; original tool keys, all content/usage, hierarchy and emission counts unchanged; golden diff limited to intended IDs |
| K2 | Same-kind duplicate record IDs with different content; cross-kind and cross-conversation reuse | Duplicate candidate keys detected before partial write; valid namespace reuse permitted; never last-write-wins. Source presence is not the uniqueness assertion |
| M1 | Old DB with NULL prompt/response/exchange tags, block tags, keyed tool tag, conversation tag and owner | Exactly preserved assignment targets/meaning, applied_at, owner/push/assigned_at after the elected transition; no silently dropped or misattached tags; if mapping unproved, entire mutation refused |
| M2 | Identical content/timestamps, omitted records, missing timestamps, config/filter drift and old parser unknown | Ambiguity blocks before delete/invalidation; never zip SQL timestamp/ULID order to source ordinals; block type and content identity checked |
| M3 | Unchanged stat and hash; second apply; interrupted apply; new eligible files later | First authorized transition bypasses both skips only for eligible sources; successful second apply has zero replacements and unchanged counts/IDs; failed/refused/ID-less/duplicate rows not falsely marked complete; retry policy explicit and atomic |
| M4 | Changed source and duplicate paths, including both rows already linked | No automatic positional carry on changed bytes; no shared-conversation deletion or false completion via `_settle_duplicate_path`; retained guard and explicit unresolved authority |
| M5 | Natural changed-file ingest after enabling extraction; empty parse and injected failures | Legacy annotations protected on all entry paths, not only a migration command; rollback leaves prior graph and evidence intact; no destructive empty-parse success |
| M6 | Final scope and harness | `DESTROY_SITES`, `REPLACEMENT_SITES`, `INGEST_REPLACEMENT_DOORS` unchanged; no parent/order/FTS/attribute/blob write changes for extraction; relevant tests and full check pass in isolated Python 3.12 environment |

M1–M5 are requirements for a future **lossless** proposal, not properties of
current replacement. If they cannot be proved without widening scope, defer the
transition rather than weaken the tests. Any later in-place mapping or bridge
requires its own explicit storage design and review.

## Decisions and gates to reopen

1. Ratify the read-only preflight contract and intended surface; no apply mode.
2. Ratify `pi_agent::<record id>`, valid-ID rules and duplicate-key refusal.
3. Choose/prove a lossless mapping strategy (or explicitly defer legacy data),
   including block identity, changed inputs and duplicate authority. Loss has not
   been accepted; no production cleanup is an option in this scope.
4. Define durable completion/coverage, retry and rollback before invalidation.
5. Obtain actual **Fable 5.1 High** review of the new exact diff when permitted.
   The last request was quota-limited until 2026-09-26 18:20 Europe/Budapest;
   no quota polling, alternate reviewer or invocation was authorized in this run.

Deferred: all other adapters; K2/K3 construction; full graph upsert/streaming;
ordering/sequence/schema rewrite; source-parent storage; #83 abstraction;
CLI/package split; live harness cleanup; citation/resolve and merge changes.
