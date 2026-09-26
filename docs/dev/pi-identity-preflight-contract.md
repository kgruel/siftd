# Pi identity preflight: internal diagnostic contract

2026-09-26 · Contract-only candidate; implementation pending independent review.
Migration `safeToImplement=false`. No identity-key or public-product policy is
ratified here. This narrows the recommended diagnostic in
[the first-slice plan](ingest-identity-first-slice.md#bounded-next-step-propose-a-read-only-preflight-then-stop),
whose P1–P4 are the acceptance gates; K1 and every migration gate remain deferred.
Earlier exact-scope planning approvals do not approve this new contract.

## Surface and scope

Proposed private entry point in `src/siftd/api/_pi_identity_preflight.py`:

```python
def inspect_pi_identity(
    *, db_path: Path, source_paths: tuple[Path, ...],
    orphan_conversation_ids: tuple[str, ...],
) -> dict[str, object]: ...
```

All arguments required, including explicitly empty tuples. SQL and connection
ownership live in `storage/_pi_identity_preflight.py`; the API coordinates source
capture and pure comparisons. No export from `api/__init__.py`, Operation,
CLI/HTTP route, plugin discovery, default path/config lookup, or apply switch.
Only disposable-fixture callers are authorized in this experiment.

- Paths must be absolute lexical paths without `..` or expansion syntax; do not
  resolve aliases, expand home, recurse directories, or glob. Refuse symlinks
  (including symlink ancestors), non-regular files, and DB paths containing `?`
  or `#` (the existing SQLite URI helper does not escape those). Missing source
  paths are findings; a missing DB refuses the report. No implicit source reads.
- Deduplicate identical input path strings, sort them, and select bookkeeping by
  exact `ingested_files.path` equality. A selected row must have harness name
  `pi_agent`; absent rows are `untracked`, NULL pointers `unlinked`, dangling or
  cross-harness pointers `invalid_link`. Never infer/rewrite a link from content
  or a session ID. Untracked/unlinked sources can still have source-only findings.
- Conversation scope is the union of valid selected-row links and explicit orphan
  IDs, deduplicated by stored conversation ID. Orphan IDs must exist, have harness
  `pi_agent`, and have **no bookkeeping reference anywhere in the DB**, including
  other harnesses and unselected paths. Invalid orphan selectors refuse the whole
  request; do not silently omit them. Empty scope means empty, not “all Pi”.
- Inspect all bookkeeping references to selected conversations, but open only
  explicitly selected paths. Report reference counts outside source scope. Two
  different paths (including hardlinks) remain two files; conversation assignments
  are counted once. No path becomes authoritative. Unselected references make
  multi-source byte comparison `unknown`, even if selected copies agree.
- No corpus-wide orphan census is implied. Unselected conversations and unrelated
  polymorphic dangling assignments cannot be attributed to this scope; say so in
  fixed report limitations, not by claiming zero orphans/loss.

## Read-only execution

Storage opens exactly the explicit DB via
`open_database(db_path, read_only=True, auto_upgrade=False)`, never the API's
optional/default-path wrapper. Start one deferred read transaction; validate and
read the graph/bookkeeping/annotation snapshot on that connection, then close it
in `finally`. `BEGIN`/`ROLLBACK` are transaction control, not writes. No DDL/DML,
ATTACH, schema ensures, migration, integrity repair, checkpoint, temp SQL tables,
pending resolution/drain, indexing, embedding, ingest or storage writer calls.
No network, subprocess, config, discovery or model work in the diagnostic.

Accept only `PRAGMA user_version == SCHEMA_VERSION` (currently 12). Version 0,
older/newer versions, missing DB, unreadable/corrupt/locked DB, or incompatible
required tables/columns refuse, rather than falling back to a writable open.
Validate ordinary tables (not substituted views) and required columns using
`sqlite_schema` and read-only `table_info` before domain queries: `harnesses`,
`conversations`, `ingested_files`, `events`, `event_content`, `event_tool_call`,
`content_blobs`, `tag_assignments`, `conversation_owners`, `pending_tags`, and
`active_sessions`. The last three can be ensured outside versioned migrations;
absence here is **unsupported**, never an empty owner/queue count. Read only
columns needed below; do not fetch tag names, owner names or stored error text.
The helper's connection-local `foreign_keys` setting is allowed; persistent
PRAGMA setters and forced `immutable=1` are not.

This promises **logical read-only**, not zero filesystem effects: SQLite may
create/use WAL/shm bookkeeping, and OS reads may update access times. Preserve
WAL correctness through the existing helper's derived-immutability/refusal
behavior. Never delete sidecars. The DB snapshot and source captures are not a
cross-filesystem atomic snapshot, and the report is not a reusable apply token.

Capture each selected regular source once into memory, with stat/fstat before
and after (device/inode, size, mtime_ns, ctime_ns) and SHA256 of those exact bytes.
After all comparisons, reopen/recheck every captured path's bytes and metadata.
Replacement, append, disappearance, read error or any difference yields
`source_changed_during_read`; affected source comparisons become unknown, never
“equal”. No automatic retries. Check/recheck detects observed changes, not an
adversarial change-and-restore or a write after the final check.

## Bounded parsing and comparisons

Use only the bundled Pi parser. A mechanical private `_parse_records(records,
path)` extraction in `adapters/pi_agent.py` may let existing `parse(Source)` and
the diagnostic consume the same parser without rereading a mutable file or writing
a temporary transcript. Preserve adapter behavior/output exactly; parity tests
are required. This is not K1 extraction: prompt/response external IDs stay NULL.
If this cannot be factored narrowly, stop rather than clone a second Pi parser.

Decode captured UTF-8/JSONL strictly first: blank lines are allowed; invalid JSON,
non-object records or malformed consumed fields are findings, not the loader's
silent skips. Distinguish zero-byte/whitespace-only `empty`, no emitted events,
and `malformed`. Unknown record types/roles are counted as ignored. Refuse graph
comparison when missing/invalid timestamps would use `now_iso()` or when session
headers are absent, conflicting or invalid (the adapter's filename fallback is
not authority). Parsing failures yield a fixed reason, never exception text.
Raw candidate scanning can still report successfully inspected records, marked
partial when any record could not be inspected.

Raw message IDs are **diagnostic candidates only**: absent/null is `missing`;
a string of 1–256 ASCII `[A-Za-z0-9_-]` characters is `candidate`; every other
value is `unclassified`. No coercion, trimming, normalization or namespace
assignment. This conservative classifier is not the policy for stored keys.
Count user/assistant candidates separately, pre-prompt assistants separately as
discarded records, and raw toolCall IDs separately from message IDs. Duplicate
candidate groups and excess occurrences are counted per source and emitted kind;
cross-kind reuse is separate, not a duplicate-key verdict. Detect repeated tool
IDs before the parser's pending-call dictionary can overwrite them; graph/key
comparison is unknown for those sources. toolResult is not another response or
candidate tool event. No new key is placed on a domain object or stored row.

For each captured source and its existing linked conversation, report independent
observations (not a correspondence proof):

1. Bookkeeping hash: `equal`, `different`, `unverified` (absent/empty/non-SHA256
   evidence), or `unknown` (capture failure/change). Compare to the preserved
   `file_hash`, regardless of stored error; keep error presence separate. Never
   echo hashes or replace evidence. Equality can reflect duplicate settlement,
   not ingestion. Source-to-source bytes are `identical`, `divergent`, `unknown`,
   or `not_multiple`; require all references selected and stably captured before
   claiming identical. Identical copies still have unknown authority.
2. Graph projection: `equal`, `different`, or `unknown`. Compare rooted multisets
   of prompt → response → tool nodes, preserving parent edges and ordered blocks
   **within** a node. Compare event kind, block type and decoded JSON content;
   tools additionally compare decoded input, blob-resolved result and status.
   Ignore IDs, sibling ordering, timestamps, model/usage, attributes, tool aliases
   and workspace metadata in this explicitly limited projection. No zip by
   timestamp/ULID/ordinal, no hash-based identity join. Missing blobs, invalid
   JSON/edges/kinds/extensions or historical binary-filter uncertainty give
   unknown where comparison cannot be made; never filter using current config.
   Repeated identical node projections are counted as ambiguous groups. Even a
   unique equal projection leaves historical parser/mapping authority unknown.
3. Keyed assignment exposure uses **current parser** event keys, not hypothetical
   message candidates: per target kind, partition keyed assignments into
   `key_present_in_all_parses`, `key_absent_from_all_parses`, `mixed`, `unknown`.
   Require all referenced sources selected, stable, parseable, and consistent in
   conversation external ID to use the first three buckets; orphan/malformed/
   ambiguous inputs give unknown. Match exact `(kind, external_id)` only; exchange
   uses its prompt key. Present means key presence, not preserved meaning. Since
   current Pi prompt/response keys are NULL, raw IDs do not “cover” stored keys.

## Deterministic report

JSON-compatible dict, internal `report_version: 1`; canonical serialization uses
sorted keys, no time/run ID/duration, filesystem path, raw ID, hash, error text,
transcript, tag, owner, pending key or content sample. Source and conversation
references are report-local integers assigned by sorted deduplicated selectors /
stored IDs; they reveal no identifiers and are not stable handles across runs.
Reason codes are fixed enums with at most five sorted references per code and an
exact total; sample truncation never truncates counts. No logging of raw inputs.

Top-level fields:

- `status`: `measured` (all requested observations available), `partial` (some
  unknown/refused observations), or `refused` (invalid scope/DB/schema or failed
  DB snapshot). Never `safe`, `covered`, or zero-loss success. Nonempty graphs
  always have unknown historical mapping, so their overall status is partial.
- `migration_safe: false`, `limitations`: fixed codes including
  `exposure_only`, `mapping_unproved`, `selected_scope_only`,
  `projection_not_full_graph`, `logical_read_only`, `not_atomic_with_sources`.
- `scope`: input occurrences, distinct selected paths, matched bookkeeping rows,
  valid links, unique conversations, explicit orphans, outside-scope references.
- `inventory`: exact selected-DB counts, or `null` on whole-report refusal.
  Event row counts split by kind and NULL/non-NULL key (empty string remains
  non-NULL and separately flagged invalid). Conversation key state is `empty`,
  `all_null`, `all_nonnull`, `partial`, or `invalid`; none means complete.
  Count assignment **rows**, not distinct tag names or tagged events: prompt,
  response, exchange and tool_call each split NULL-key/non-NULL-key; all block
  assignments regardless of event key; conversation assignments and ownership
  rows separately. Invalid target-kind/event-kind pairs are separate unknown
  assignments, not silently dropped. Include keyed-exposure partitions above.
- `pending`: row counts, never targets applied. Match selected conversations'
  external IDs using the existing directed `_covered_keys` rule, but retain all
  matching conversations across the DB rather than picking the newest winner.
  Count uniquely selected matches and ambiguous matches touching scope, once per
  queue row; group by entity_type/last_marker with an `other` bucket. Additionally
  give DB-context counts of unmatched Pi-attributed rows (literal `pi_agent::`
  prefix or exact active-session key with adapter `pi_agent`) and remaining
  unattributed/out-of-scope rows. These context counts are **not** selected Pi
  impact; unknown attribution is never zero pending risk. Do not resolve ordinals.
- `sources`, `conversations`: one observation row per distinct selected entity,
  with enum states and counts defined above. Candidate counts are source-record
  counts, never stored-event counts; include inspected/uninspectable denominators.
  `findings`: overlapping reason totals, unit (`source`, `conversation`,
  `assignment`, `record`, `pending_row`) and bounded local-reference samples.
  Unknown/unavailable counts are `null`, not zero; each unassessed source,
  conversation and keyed assignment also has an exact unknown/refused count.

Source reasons include untracked, unlinked, invalid_link, bookkeeping_error,
missing, unreadable, non_regular, empty, no_events, malformed, missing_id,
unclassified_id, duplicate_id, missing_timestamp, invalid_session,
source_changed_during_read, hash_different, hash_unverified. Conversation reasons
include orphan, partial_keys, invalid_keys, multiple_sources_identical,
multiple_sources_divergent, multiple_sources_unknown, graph_different,
graph_unavailable, repeated_projection, authority_unknown, mapping_unknown.
Schema/open/scope refusals use fixed codes; unexpected programming errors must
fail tests/raise rather than masquerade as successful zero-count reports.

## Disposable acceptance matrix (implementation pending)

`tests/test_pi_identity_preflight.py` will exercise the internal API/storage
boundary; adapter parity belongs with existing Pi adapter tests. No live run.

| Gate | Fixture/assertion |
|---|---|
| P1 | Current, absent, v0, stale, future, incomplete-current schema; locked/corrupt/unreadable DB. Explicit refusals; no writable fallback. Full logical dump/schema/user_version/bookkeeping/queue/blob metadata and source bytes unchanged on success and every refusal. SQL trace/authorizer disallows DDL/DML/persistent PRAGMA writes; deny discovery/config/network/ingest/ensure/drain calls. WAL fixture sees uncheckpointed committed rows; no zero-sidecar assertion. |
| P2 | NULL-key prompt + response + exchange assignments, one block assignment, one keyed tool assignment, one conversation assignment, one owner. Two selected paths link one conversation: files=2, conversations=1, assignments=6, owners=1; NULL event assignments=3, blocks=1, keyed tool=1. Repeat selectors do not inflate totals. Add keyed-parent block and NULL-key tool variants; ambiguous pending suffix matches count once, queue unchanged. |
| P3 | Untracked/unlinked/error/missing/unreadable/non-regular/empty/no-event/malformed sources; explicit Pi orphan, invalid/non-orphan/non-Pi selectors; ID-less/unclassified/duplicate IDs, cross-kind reuse, pre-prompt assistants, folded/repeated tools, partial and fully keyed graphs. Separate nonzero reasons and true unknowns; no inferred orphan linking or coverage. Unselected duplicate reference, absent owner/queue table, dangling/invalid scoped targets and blobs must not become zero risk. |
| P4 | Changed/appended/truncated/reordered bytes, stable stat with changed hash, concurrent write/replace/delete via deterministic barriers, identical/divergent duplicate copies, equal hash with different stored graph, repeated content/tied timestamps, missing timestamps, binary-filter uncertainty. Recheck downgrades affected comparisons; no positional matches. Frozen inputs and reordered/duplicated selectors produce identical canonical output except the explicit input-occurrence count; secret sentinels never appear in output/logs. |

Implementation must pass `./dev check` and `./dev check --all` at its exact
committed HEAD. Contract-only preparation runs `./dev check`; it does not claim
P1–P4 are already implemented. All extraction/K1, migrations, invalidation or
completion schema, tag carry bridge, parentage/order changes, graph upsert and
CLI/package redesign remain out. If this limited experiment needs a broader
rewrite, stop with a narrower proposal, not an expanded migration design.
