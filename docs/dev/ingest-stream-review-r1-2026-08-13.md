# External review R1 — ingest-stream design (2026-08-13)

Reviewer: codex `gpt-5.6-sol`, reasoning effort high, read-only, zero-context.
Session `019ffdd8-a0c8-7993-9248-adab5a98b6fd`. Verdict: **REWORK** (8 P1 / 7 P2 / 2 P3).

Verified independently before acceptance: findings 1, 3, 5, 9, 16 — all confirmed.

---

## P1 blocking

1. **GROUND TRUTH FALSE: G5 incorrectly claims all ordinal reads use `ORDER BY timestamp, id`; several canonical paths use timestamp alone.**

   Evidence: transcript construction orders prompts, responses, and tool calls only by timestamp in [queries.py](/Users/kaygee/Code/siftd/src/siftd/storage/queries.py:95), [queries.py](/Users/kaygee/Code/siftd/src/siftd/storage/queries.py:140), and [queries.py](/Users/kaygee/Code/siftd/src/siftd/storage/queries.py:385). Turn-index enrichment does likewise in [search.py](/Users/kaygee/Code/siftd/src/siftd/api/search.py:582). The live-DB counts do reproduce 1,360,176 groups, 19,076 tied groups, and 44,822 tied events, but they do not prove those paths use the ULID tiebreak.

   Change: inventory and centralize every positional ordering expression before migration. Backfill from the actual legacy semantics or explicitly accept an ordering change; remove the “semantically a no-op” claim.

2. **GROUND TRUTH OVERCLAIM: G3’s counts are correct, but “the gaps are extraction gaps, not structural ones” is false.**

   Evidence: the live query reproduces every G3 count, but Codex user/assistant records have no per-message ID in [input.jsonl](/Users/kaygee/Code/siftd/tests/fixtures/adapters/codex_cli/minimal/input.jsonl:3). Antigravity supplies `step_index`, yet one planner record can declare multiple stored tool calls in [antigravity_cli.py](/Users/kaygee/Code/siftd/src/siftd/adapters/antigravity_cli.py:260), including unresolved calls with no later result step. Those require fabricated composite identities, not extraction.

   Change: replace keyed/unkeyed with an enumerated key taxonomy: supplied opaque ID, supplied composite ID, derived ordinal composite, and unkeyable. Gate slice 2 by that taxonomy.

3. **Slice 1 will not raise existing citation coverage “naturally”: unchanged files are skipped after an adapter-only key-extraction change.**

   Evidence: ingest skips immediately when stored stat matches in [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:435), and also skips byte-identical files after hashing at [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:449). `ingested_files` records no parser/adapter revision in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:151).

   Change: include parser revision in ingest invalidation, or ship an explicit one-time invalidation/backfill for affected harnesses. If automatic durable invalidation is chosen, slice 1 is not schema-free.

4. **Slice 2’s fallback and its dissolution gate contradict each other: retaining delete-and-reinsert means the ingest destroy door cannot disappear.**

   Evidence: the design retains the old path for unkeyed and rewritable sources, while current file and session paths both call `_take_conversation_for_replacement` at [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:459) and [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:554). `DESTROY_SITES` correctly enumerates that ingest door in [test_replacement_carry.py](/Users/kaygee/Code/siftd/tests/architecture/test_replacement_carry.py:145).

   Change: keep the ingest destroy/carry ratchet for the fallback population. Define a per-adapter reconciliation policy before slice 2, and shrink the ratchet only when the last replacement path actually leaves.

5. **The proposed event upsert is not an implementation-depth graph upsert; the existing constraint only solves the root-row collision.**

   Evidence: existing writers mint a new ID and return it unconditionally in [sqlite.py](/Users/kaygee/Code/siftd/src/siftd/storage/sqlite.py:2858), then blindly insert response extensions, content, FTS rows, attributes, and tool extensions in [sqlite.py](/Users/kaygee/Code/siftd/src/siftd/storage/sqlite.py:3001). A SQLite reproduction showed the event upsert returns the old row ID, while two NULL keys both insert. Worse, tool attributes use `scope=NULL`, but the unique key includes nullable `scope` in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:299); reproducing two current `set_attribute` calls produced two rows, not an update. FTS is insert-only in [fts.py](/Users/kaygee/Code/siftd/src/siftd/storage/fts.py:217).

   Change: specify transactional upsert semantics for every child table, including `RETURNING id`, removal of vanished blocks/attributes, blob refcounts, and FTS replacement. Normalize `attributes.scope` to non-NULL or add suitable partial uniqueness. The blanket “no new constraint required” verdict is unsupported.

6. **The shared-tuple invariant breaks for ordinal keys because the upsert key and durable citation key have different admissibility requirements.**

   Evidence: the design assigns Codex/Aider record ordinals as event keys while also deciding that durable locators contain no ordinal. The schema accepts any `external_id` under the same uniqueness rule in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:225), so it cannot distinguish durable source identity from positional ingest identity.

   Change: split `IngestKey` from `CitationKey`. Opaque stable source IDs may implement both; ordinal/composite keys may support upsert while citation remains refused until durability is demonstrated.

7. **Raw source parent pointers cannot replace semantic `parent_id`; they frequently point to ignored predecessor records, not the stored prompt/response parent.**

   Evidence: Pi’s second assistant record points to a tool-result record in [input.jsonl](/Users/kaygee/Code/siftd/tests/fixtures/adapters/pi_agent/minimal/input.jsonl:7), while that tool-result record is folded into a prior `ToolCall` and never stored as its own event in [pi_agent.py](/Users/kaygee/Code/siftd/src/siftd/adapters/pi_agent.py:186). Current readers require semantic response→prompt relationships via `parent_id` in [events.py](/Users/kaygee/Code/siftd/src/siftd/storage/events.py:270).

   Change: keep separate `source_parent_external_id` provenance and semantic parent linkage, or store every raw source node. Do not derive the existing tree directly from raw predecessor pointers.

8. **Upsert does not dissolve the existing duplicate-source authority guard; without it divergent copies can overwrite the same natural conversation.**

   Evidence: the live DB has four bookkeeping paths containing `adadee7e1245fdc43`; two paths are unlinked while another owns the conversation containing 211 responses. Re-parsing the plain subagent path yields zero responses. Current `_conversation_claimed_elsewhere` deliberately prevents a second path from replacing the shared conversation in [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:811).

   Change: retain or generalize single-writer source authority before enabling upsert. Reconciliation must operate against the elected authoritative source, not merely `(harness_id, external_id)`.

## P2 should-fix-before-build

9. **G4’s Claude key row names the wrong usable identity relationship: `message.id` is not unique and `parentUuid` targets top-level `uuid`.**

   Evidence: the live G2 transcript has 13 distinct assistant `uuid` values but only seven distinct `message.id` values; one `message.id` occurs three times. The adapter correctly keys events from `record["uuid"]` in [claude_code.py](/Users/kaygee/Code/siftd/src/siftd/adapters/claude_code.py:309), not `message.id`.

   Change: rewrite G4 per emitted entity, explicitly naming key namespace, uniqueness scope, and parent namespace. Add fixture/live-corpus uniqueness ratchets rather than recording only field presence.

10. **`StreamRecord` cannot be “one per source record” with one `kind` and one `sequence`.**

   Evidence: a single Claude assistant record contains response content plus tool uses in [input.jsonl](/Users/kaygee/Code/siftd/tests/fixtures/adapters/claude_code/minimal/input.jsonl:2); Antigravity planner records can contain multiple tool calls; tool-result and token-count records mutate earlier events rather than creating new ones. A single source position can therefore emit zero, one, or several stored events.

   Change: define either an operation IR containing event snapshots/patches, or use `(source_position, emission_index)` provenance per emitted event. Add a conversation envelope carrying conversation ID, workspace, branch, and time bounds.

11. **J1 misses stale descendants and parser evolution; diffing only event keys does not restore total source equivalence.**

   Evidence: retained events can lose or reorder `event_content`, attributes, tool results, or FTS text while keeping the same event key. Current content identity is positional `(event_id, block_index)` in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:287). Adapter changes also alter old parses without changing source bytes, which current ingest skips.

   Change: reconciliation must compare the complete adapter-owned event graph, including descendants and derived indexes, and include parser revision. Define fail/replace semantics for kind or key changes.

12. **J2’s content-hash check cannot distinguish an illegal ordinal rewrite from a legitimate mutable-record lifecycle.**

   Evidence: OpenCode reads stable message/part primary keys whose data and `time_updated` fields can evolve in [opencode.py](/Users/kaygee/Code/siftd/src/siftd/adapters/opencode.py:124). Antigravity later system records mutate a previously emitted background tool call in [antigravity_cli.py](/Users/kaygee/Code/siftd/src/siftd/adapters/antigravity_cli.py:272). Both legitimately change derived content under stable identity.

   Change: declare key stability and content mutability separately. Use prefix/checkpoint verification for truly append-only sources, monotonic transition rules for mutable records, and fail the whole transaction on an illegal rewrite.

13. **The dissolution claims for `dropped_events`, `dropped_blocks`, and block-tag re-pointing are false while fallback replacement and positional block identity remain.**

   Evidence: fallback delete remains in slice 2. Even on upsert, updating `(event_id, block_index)` can silently move an existing block tag to different content; deleting a vanished block triggers cleanup. Block IDs and tags are explicitly positional in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:287) and [events.py](/Users/kaygee/Code/siftd/src/siftd/storage/events.py:86).

   Change: retain loss counters for fallback paths and add block-content/key mismatch reporting. Do not claim block carryover dissolved until block identity or explicit refusal is settled.

14. **R1–R6 do not yet cover the write path’s load-bearing enumerable properties, and R6 tests a different claim than its name.**

   Evidence: R6 is titled “No adapter fabricates a parent” but proposes checking `external_id IS NULL`, which says nothing about parent fabrication. R5 pins one Claude fixture rather than enumerating record conservation. R1 cannot assert equality across independently embedded SQL and locator logic without a shared key definition. The existing ratchet idiom explicitly enumerates exact populations and detector limits in [test_replacement_carry.py](/Users/kaygee/Code/siftd/tests/architecture/test_replacement_carry.py:69).

   Change: centralize typed key definitions, then add parametrized properties for:

   - same input twice → stable IDs and unchanged row, FTS, attribute, and blob-ref counts;
   - ingest prefix then append → identical database to one full ingest;
   - rewritten ordinal source → detected with transaction unchanged;
   - full reconcile → exact adapter-owned graph equality;
   - every emitted parent resolves or carries an explicit orphan reason;
   - every adapter declares key strategy, rewrite policy, and parser revision.

   Keep R2; R3 is enumerable but belongs to the separate citation/resolve plan; broaden R4 and R5 across the adapter inventory.

15. **“Incremental FTS is already built” is only a location claim, not proof that update/delete semantics exist.**

   Evidence: scoped rebuild support exists in [fts.py](/Users/kaygee/Code/siftd/src/siftd/storage/fts.py:136), but the per-row writer only inserts and has no update/delete synchronization in [fts.py](/Users/kaygee/Code/siftd/src/siftd/storage/fts.py:217).

   Change: make touched-conversation rebuild part of slice 2’s transaction, or implement explicit per-block FTS replacement and test for duplicates/orphans after repeated upsert.

## P3 worth-knowing

16. **GROUND TRUTH FALSE: G6 says six adapters implement `normalize_record()`; the repository has seven.**

   Evidence: `rg -l '^def normalize_record' src/siftd/adapters/*.py` returns Antigravity, Claude, Codex, Copilot, Gemini, Pi, and VSCode. `NormalizedRecord` also already has an extensible `extra` dictionary in [sdk.py](/Users/kaygee/Code/siftd/src/siftd/adapters/sdk.py:532).

   Change: correct the count and test whether the new canonical record can replace/evolve `NormalizedRecord` with a compatibility projection, rather than asserting a net-new parallel type is required.

17. **G1’s “only the middle is a tree” framing does not generalize across the nine-adapter scope.**

   Evidence: OpenCode starts from relational session/message/part tables in [opencode.py](/Users/kaygee/Code/siftd/src/siftd/adapters/opencode.py:94), VSCode reconstructs a nested requests array in [vscode.py](/Users/kaygee/Code/siftd/src/siftd/adapters/vscode.py:107), and Aider parses markdown sessions in [aider.py](/Users/kaygee/Code/siftd/src/siftd/adapters/aider.py:92).

   Change: narrow G1 to “storage is flat and several append-log adapters have flat source records”; do not use it to justify a universal one-source-record stream contract.

REWORK
tokens used
219,961
## P1 blocking

1. **GROUND TRUTH FALSE: G5 incorrectly claims all ordinal reads use `ORDER BY timestamp, id`; several canonical paths use timestamp alone.**

   Evidence: transcript construction orders prompts, responses, and tool calls only by timestamp in [queries.py](/Users/kaygee/Code/siftd/src/siftd/storage/queries.py:95), [queries.py](/Users/kaygee/Code/siftd/src/siftd/storage/queries.py:140), and [queries.py](/Users/kaygee/Code/siftd/src/siftd/storage/queries.py:385). Turn-index enrichment does likewise in [search.py](/Users/kaygee/Code/siftd/src/siftd/api/search.py:582). The live-DB counts do reproduce 1,360,176 groups, 19,076 tied groups, and 44,822 tied events, but they do not prove those paths use the ULID tiebreak.

   Change: inventory and centralize every positional ordering expression before migration. Backfill from the actual legacy semantics or explicitly accept an ordering change; remove the “semantically a no-op” claim.

2. **GROUND TRUTH OVERCLAIM: G3’s counts are correct, but “the gaps are extraction gaps, not structural ones” is false.**

   Evidence: the live query reproduces every G3 count, but Codex user/assistant records have no per-message ID in [input.jsonl](/Users/kaygee/Code/siftd/tests/fixtures/adapters/codex_cli/minimal/input.jsonl:3). Antigravity supplies `step_index`, yet one planner record can declare multiple stored tool calls in [antigravity_cli.py](/Users/kaygee/Code/siftd/src/siftd/adapters/antigravity_cli.py:260), including unresolved calls with no later result step. Those require fabricated composite identities, not extraction.

   Change: replace keyed/unkeyed with an enumerated key taxonomy: supplied opaque ID, supplied composite ID, derived ordinal composite, and unkeyable. Gate slice 2 by that taxonomy.

3. **Slice 1 will not raise existing citation coverage “naturally”: unchanged files are skipped after an adapter-only key-extraction change.**

   Evidence: ingest skips immediately when stored stat matches in [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:435), and also skips byte-identical files after hashing at [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:449). `ingested_files` records no parser/adapter revision in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:151).

   Change: include parser revision in ingest invalidation, or ship an explicit one-time invalidation/backfill for affected harnesses. If automatic durable invalidation is chosen, slice 1 is not schema-free.

4. **Slice 2’s fallback and its dissolution gate contradict each other: retaining delete-and-reinsert means the ingest destroy door cannot disappear.**

   Evidence: the design retains the old path for unkeyed and rewritable sources, while current file and session paths both call `_take_conversation_for_replacement` at [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:459) and [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:554). `DESTROY_SITES` correctly enumerates that ingest door in [test_replacement_carry.py](/Users/kaygee/Code/siftd/tests/architecture/test_replacement_carry.py:145).

   Change: keep the ingest destroy/carry ratchet for the fallback population. Define a per-adapter reconciliation policy before slice 2, and shrink the ratchet only when the last replacement path actually leaves.

5. **The proposed event upsert is not an implementation-depth graph upsert; the existing constraint only solves the root-row collision.**

   Evidence: existing writers mint a new ID and return it unconditionally in [sqlite.py](/Users/kaygee/Code/siftd/src/siftd/storage/sqlite.py:2858), then blindly insert response extensions, content, FTS rows, attributes, and tool extensions in [sqlite.py](/Users/kaygee/Code/siftd/src/siftd/storage/sqlite.py:3001). A SQLite reproduction showed the event upsert returns the old row ID, while two NULL keys both insert. Worse, tool attributes use `scope=NULL`, but the unique key includes nullable `scope` in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:299); reproducing two current `set_attribute` calls produced two rows, not an update. FTS is insert-only in [fts.py](/Users/kaygee/Code/siftd/src/siftd/storage/fts.py:217).

   Change: specify transactional upsert semantics for every child table, including `RETURNING id`, removal of vanished blocks/attributes, blob refcounts, and FTS replacement. Normalize `attributes.scope` to non-NULL or add suitable partial uniqueness. The blanket “no new constraint required” verdict is unsupported.

6. **The shared-tuple invariant breaks for ordinal keys because the upsert key and durable citation key have different admissibility requirements.**

   Evidence: the design assigns Codex/Aider record ordinals as event keys while also deciding that durable locators contain no ordinal. The schema accepts any `external_id` under the same uniqueness rule in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:225), so it cannot distinguish durable source identity from positional ingest identity.

   Change: split `IngestKey` from `CitationKey`. Opaque stable source IDs may implement both; ordinal/composite keys may support upsert while citation remains refused until durability is demonstrated.

7. **Raw source parent pointers cannot replace semantic `parent_id`; they frequently point to ignored predecessor records, not the stored prompt/response parent.**

   Evidence: Pi’s second assistant record points to a tool-result record in [input.jsonl](/Users/kaygee/Code/siftd/tests/fixtures/adapters/pi_agent/minimal/input.jsonl:7), while that tool-result record is folded into a prior `ToolCall` and never stored as its own event in [pi_agent.py](/Users/kaygee/Code/siftd/src/siftd/adapters/pi_agent.py:186). Current readers require semantic response→prompt relationships via `parent_id` in [events.py](/Users/kaygee/Code/siftd/src/siftd/storage/events.py:270).

   Change: keep separate `source_parent_external_id` provenance and semantic parent linkage, or store every raw source node. Do not derive the existing tree directly from raw predecessor pointers.

8. **Upsert does not dissolve the existing duplicate-source authority guard; without it divergent copies can overwrite the same natural conversation.**

   Evidence: the live DB has four bookkeeping paths containing `adadee7e1245fdc43`; two paths are unlinked while another owns the conversation containing 211 responses. Re-parsing the plain subagent path yields zero responses. Current `_conversation_claimed_elsewhere` deliberately prevents a second path from replacing the shared conversation in [orchestration.py](/Users/kaygee/Code/siftd/src/siftd/ingestion/orchestration.py:811).

   Change: retain or generalize single-writer source authority before enabling upsert. Reconciliation must operate against the elected authoritative source, not merely `(harness_id, external_id)`.

## P2 should-fix-before-build

9. **G4’s Claude key row names the wrong usable identity relationship: `message.id` is not unique and `parentUuid` targets top-level `uuid`.**

   Evidence: the live G2 transcript has 13 distinct assistant `uuid` values but only seven distinct `message.id` values; one `message.id` occurs three times. The adapter correctly keys events from `record["uuid"]` in [claude_code.py](/Users/kaygee/Code/siftd/src/siftd/adapters/claude_code.py:309), not `message.id`.

   Change: rewrite G4 per emitted entity, explicitly naming key namespace, uniqueness scope, and parent namespace. Add fixture/live-corpus uniqueness ratchets rather than recording only field presence.

10. **`StreamRecord` cannot be “one per source record” with one `kind` and one `sequence`.**

   Evidence: a single Claude assistant record contains response content plus tool uses in [input.jsonl](/Users/kaygee/Code/siftd/tests/fixtures/adapters/claude_code/minimal/input.jsonl:2); Antigravity planner records can contain multiple tool calls; tool-result and token-count records mutate earlier events rather than creating new ones. A single source position can therefore emit zero, one, or several stored events.

   Change: define either an operation IR containing event snapshots/patches, or use `(source_position, emission_index)` provenance per emitted event. Add a conversation envelope carrying conversation ID, workspace, branch, and time bounds.

11. **J1 misses stale descendants and parser evolution; diffing only event keys does not restore total source equivalence.**

   Evidence: retained events can lose or reorder `event_content`, attributes, tool results, or FTS text while keeping the same event key. Current content identity is positional `(event_id, block_index)` in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:287). Adapter changes also alter old parses without changing source bytes, which current ingest skips.

   Change: reconciliation must compare the complete adapter-owned event graph, including descendants and derived indexes, and include parser revision. Define fail/replace semantics for kind or key changes.

12. **J2’s content-hash check cannot distinguish an illegal ordinal rewrite from a legitimate mutable-record lifecycle.**

   Evidence: OpenCode reads stable message/part primary keys whose data and `time_updated` fields can evolve in [opencode.py](/Users/kaygee/Code/siftd/src/siftd/adapters/opencode.py:124). Antigravity later system records mutate a previously emitted background tool call in [antigravity_cli.py](/Users/kaygee/Code/siftd/src/siftd/adapters/antigravity_cli.py:272). Both legitimately change derived content under stable identity.

   Change: declare key stability and content mutability separately. Use prefix/checkpoint verification for truly append-only sources, monotonic transition rules for mutable records, and fail the whole transaction on an illegal rewrite.

13. **The dissolution claims for `dropped_events`, `dropped_blocks`, and block-tag re-pointing are false while fallback replacement and positional block identity remain.**

   Evidence: fallback delete remains in slice 2. Even on upsert, updating `(event_id, block_index)` can silently move an existing block tag to different content; deleting a vanished block triggers cleanup. Block IDs and tags are explicitly positional in [schema.sql](/Users/kaygee/Code/siftd/src/siftd/storage/schema.sql:287) and [events.py](/Users/kaygee/Code/siftd/src/siftd/storage/events.py:86).

   Change: retain loss counters for fallback paths and add block-content/key mismatch reporting. Do not claim block carryover dissolved until block identity or explicit refusal is settled.

14. **R1–R6 do not yet cover the write path’s load-bearing enumerable properties, and R6 tests a different claim than its name.**

   Evidence: R6 is titled “No adapter fabricates a parent” but proposes checking `external_id IS NULL`, which says nothing about parent fabrication. R5 pins one Claude fixture rather than enumerating record conservation. R1 cannot assert equality across independently embedded SQL and locator logic without a shared key definition. The existing ratchet idiom explicitly enumerates exact populations and detector limits in [test_replacement_carry.py](/Users/kaygee/Code/siftd/tests/architecture/test_replacement_carry.py:69).

   Change: centralize typed key definitions, then add parametrized properties for:

   - same input twice → stable IDs and unchanged row, FTS, attribute, and blob-ref counts;
   - ingest prefix then append → identical database to one full ingest;
   - rewritten ordinal source → detected with transaction unchanged;
   - full reconcile → exact adapter-owned graph equality;
   - every emitted parent resolves or carries an explicit orphan reason;
   - every adapter declares key strategy, rewrite policy, and parser revision.

   Keep R2; R3 is enumerable but belongs to the separate citation/resolve plan; broaden R4 and R5 across the adapter inventory.

15. **“Incremental FTS is already built” is only a location claim, not proof that update/delete semantics exist.**

   Evidence: scoped rebuild support exists in [fts.py](/Users/kaygee/Code/siftd/src/siftd/storage/fts.py:136), but the per-row writer only inserts and has no update/delete synchronization in [fts.py](/Users/kaygee/Code/siftd/src/siftd/storage/fts.py:217).

   Change: make touched-conversation rebuild part of slice 2’s transaction, or implement explicit per-block FTS replacement and test for duplicates/orphans after repeated upsert.

## P3 worth-knowing

16. **GROUND TRUTH FALSE: G6 says six adapters implement `normalize_record()`; the repository has seven.**

   Evidence: `rg -l '^def normalize_record' src/siftd/adapters/*.py` returns Antigravity, Claude, Codex, Copilot, Gemini, Pi, and VSCode. `NormalizedRecord` also already has an extensible `extra` dictionary in [sdk.py](/Users/kaygee/Code/siftd/src/siftd/adapters/sdk.py:532).

   Change: correct the count and test whether the new canonical record can replace/evolve `NormalizedRecord` with a compatibility projection, rather than asserting a net-new parallel type is required.

17. **G1’s “only the middle is a tree” framing does not generalize across the nine-adapter scope.**

   Evidence: OpenCode starts from relational session/message/part tables in [opencode.py](/Users/kaygee/Code/siftd/src/siftd/adapters/opencode.py:94), VSCode reconstructs a nested requests array in [vscode.py](/Users/kaygee/Code/siftd/src/siftd/adapters/vscode.py:107), and Aider parses markdown sessions in [aider.py](/Users/kaygee/Code/siftd/src/siftd/adapters/aider.py:92).

   Change: narrow G1 to “storage is flat and several append-log adapters have flat source records”; do not use it to justify a universal one-source-record stream contract.

