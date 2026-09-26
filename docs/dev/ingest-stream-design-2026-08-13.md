# Ingest stream — design v2 (2026-08-13)

Taking siftd's write path from delete-and-reinsert to append, so that a
transcript lands in the database in the shape it arrived in.

**This is a rewrite, not a revision.** v1 came back REWORK from external review
(codex `gpt-5.6-sol` high, zero-context, repo access — findings and dispositions
in `ingest-stream-review-r1-2026-08-13.md`). Two of its ground-truth claims were
false and its organizing thesis was falsified. What changed:

- v1's spine was *"citation coverage, upsert coverage and the inverse fabrication
  rate are the same number."* **False.** Ordinal keys can carry an upsert and
  must not carry a citation. The relationship is a containment, not an identity,
  and the doc is now organized around a key taxonomy instead.
- v1 claimed adapters key on `message.id`. **False** — `claude_code.py:312` keys
  on the record-level `uuid`, and `message.id` is not unique per stored event.
- v1 claimed reads order by `ORDER BY timestamp, id`. **False for most of them.**
- v1 claimed the upsert needs no new constraint. **False**, and now reproduced.

Everything below that is not marked as measured is a proposal.

**Scope.** Slices 1–2 at implementation depth. Slices 3–5 at architecture depth
with forks named. Out of scope: the `siftd cite` / `siftd resolve` command design
(citation-anchors plan) and `api/merge.py`'s replacement door (own arc).

---

## Goals (user-set)

1. **Real-time ingest.** A session should be queryable while still open.
2. **One representation of a transcript.** Today there are two — flat
   `NormalizedRecord` for `peek`, nested `Conversation` for `ingest`.
3. **Citation durability co-extensive with what the write path can guarantee** —
   restated from v1, which said "co-extensive with the write path" and thereby
   assumed the identity that review falsified.

---

## The organizing axis: a key taxonomy

v1 sorted adapters into *keyed* and *unkeyed*. That axis is too coarse to
sequence work by, because it hides the distinction that actually decides what a
key may be used for.

| class | definition | may drive upsert | may drive citation |
|---|---|---|---|
| **K1 supplied opaque** | source emits a stable per-record id | yes | yes |
| **K2 supplied composite** | stable identity exists but spans fields (e.g. one record declaring several tool calls) | yes | yes, once the composite is pinned |
| **K3 derived ordinal** | identity is the record's position (`step_index`, file ordinal) | yes, under an append-only guarantee | **no** |
| **K4 unkeyable** | no stable identity at any granularity | no | no |

The two columns differ because the requirements differ. An upsert needs a key
that is **stable for as long as the source is stable**. A citation needs a key
that is **stable across source rewrites** — a strictly stronger property. K3
satisfies the first and fails the second, which is exactly where v1's single
tuple broke.

**The corrected relationship — a containment, not an identity:**

```
citable (K1,K2)  ⊆  upsertable (K1,K2,K3)  ⊆  storable (all)
```

This is weaker than v1's claim and still load-bearing: it means citation
refusals remain a *lower bound* on upsert coverage, so the citation surface
still measures the arc's progress — it just no longer measures it exactly.

**Two key types, not one.** `IngestKey` and `CitationKey` are separate types.
K1/K2 adapters implement both from the same value; K3 adapters implement only
`IngestKey`, and citation refuses with a stated reason. The schema cannot make
this distinction — `events.external_id` accepts anything under one uniqueness
rule — so it has to live in the adapter contract and be enforced by a ratchet.

---

## Ground truth

Measured 2026-08-13, reproducible; commands at the end. **Corrections from v1
are marked.** A reviewer should re-run these; if one is wrong, what rests on it
is wrong.

### G1 — Storage is flat; *several* sources are flat streams

`events` is flat and polymorphic. `domain/models.py` is a strict nesting built
by every adapter and immediately walked back down by `store_conversation`.

**Narrowed from v1**, which said "both ends are flat" and generalized from Claude
Code. That does not hold across the inventory: `opencode` reads relational
session/message/part tables, `vscode` reconstructs a nested requests array,
`aider` parses markdown prose. The defensible claim is *storage is flat, and the
append-log sources are flat* — it does not license a universal one-source-record
contract.

### G2 — A response with no preceding prompt is silently discarded

`claude_code.py:457` attaches a built response only `if current_prompt is not
None`. No `else`, no counter, no warning.

Reproduced: synthetic fixture, 2 responses in / 1 out. Re-parsing 995 real
transcripts: **13 assistant records dropped in 1 file** — a sub-agent transcript
whose first three records are `assistant` and whose six `user` records are all
tool_results, so `current_prompt` is never set.

`codex_cli.py:335` hit the same case and fabricated a prompt; `claude_code`
never did. **This is an independent bug and should ship on its own, not inside
this arc.**

### G3 — NULL `external_id` is all-or-nothing per (harness, kind)

```
antigravity_cli  prompt      3 / response     43 / tool_call 40   100% NULL
codex_cli        prompt  3,089 / response  6,364                  100% NULL
copilot_cli      prompt      1 / response     34                  100% NULL
opencode         prompt      5 / response     12                  100% NULL
pi_agent         prompt    650 / response 16,598                  100% NULL
```

Overall: prompts 93.0% keyed, responses 97.2%, tool_calls 100.0%.

**Corrected from v1**, which concluded "the gaps are extraction gaps, not
structural ones." The counts are right; the conclusion was too strong. Some gaps
are extraction (K1 available, never read); others need a composite or ordinal
identity constructed (K2/K3). The taxonomy above replaces the binary.

### G4 — Key availability per adapter *(rebuilt; v1's version was wrong)*

v1 recorded field *presence* and named `message.id` for Claude Code. Both were
errors: the adapter keys on the record-level `uuid` (`claude_code.py:312`), and
in the G2 transcript 13 distinct record `uuid`s collapse to only **7 distinct
`message.id` values**, one appearing three times. Presence is not uniqueness.

| adapter | tier | key source | class | events keyed |
|---|---|---|---|---|
| `claude_code` | core | record `uuid`; parent is `parentUuid` → `uuid` | K1 | 100% |
| `gemini_cli` | frozen | message `id` | K1 | 100% |
| `vscode` | contrib | `requestId` / `responseId` / `toolCallId` | K1 | 100% |
| `pi_agent` | contrib | record `id` (45/45); `parentId` (44/45) | K1 | 0% |
| `copilot_cli` | contrib | record `id` (9/9), `parentId` (9/9), `data.toolCallId` | K1 | 0% |
| `opencode` | contrib | SQLite primary keys | K1 | 0% |
| `antigravity_cli` | core | `step_index`; multi-tool records need a composite | K2/K3 | 0% |
| `codex_cli` | core | `call_id` for tools; no per-message id | K3 | 0% |
| `aider` | frozen | markdown, no ids at any granularity | K3/K4 | 0% |

**J7 is closed:** `copilot_cli` does carry ids — read from its fixture
(`tests/fixtures/adapters/copilot_cli/minimal/input.jsonl`), which is the
contract, rather than from live files that no longer exist. It is K1 with an
extraction gap, same family as `pi_agent`.

*Method note, twice-learned.* v1's table was enumerated from live-DB harness rows
and missed `aider` entirely (in-tree, but never ingested here — the #36 arc).
`cline`/`cursor`/`goose` are the inverse: harness rows with no module anywhere in
`src/siftd`. Enumerate by role, never by what the database happens to hold. Then
v1 made the *second* version of the same error — reading field names instead of
measuring uniqueness. Presence is not identity.

### G5 — There is no single ordering semantics *(corrected; worse than v1 said)*

v1 claimed reads use `ORDER BY timestamp, id`. Inventory of every event-ordering
expression in `src/siftd/`:

| expression | sites |
|---|---|
| `ORDER BY timestamp` (no tiebreak) | ~19 — `queries.py:95,106,112,391,442,482,492`, `search.py:553,583,603,629,713`, `events.py:208,281,316`, `file_refs.py:84`, `chunker.py:398`, `fts.py:515` |
| `ORDER BY timestamp, id` | ~8 — `target_ref.py:216`, `events.py:176,261`, `sqlite.py:1628` |
| `ORDER BY timestamp DESC, id DESC` | 5 — `events.py:277,284`, `export.py:249`, `search.py:1783` |
| `ORDER BY parent_id, timestamp` | 1 — `queries.py:144` |
| event timestamp mixed with *content* id | 2 — `export.py:308`, `search.py:1836` |

Live: 1,360,176 (conversation, kind, timestamp) groups, 19,076 tied, **44,822
events (3.2%) inside a tied group** — where the four expressions above can
disagree with each other.

**Consequence: v1's "the backfill is semantically a no-op" is withdrawn.** There
is no single legacy semantics to reproduce. Any `sequence` backfill is a
*choice* of ordering, and the ordering expressions must be inventoried and
centralized before it is made.

### G6 — The flat form exists, is lossy, and has an extension point

`sdk.py:533` defines `NormalizedRecord`; **seven** adapters implement
`normalize_record()` (v1 said six). Wired only into `make_peek_hooks`.

Lossy against ingest: no `external_id`, no parent, no attributes dict,
`input_tokens: int = 0` where ingest needs `int | None`. Cache tokens ride
`Response.attributes` (`claude_code.py:407-420`) and feed the v10/v11 cost model.

**Softened from v1:** it does carry an extensible `extra` dict, so "net-new
parallel type" is not established. Whether the canonical record *evolves*
`NormalizedRecord` with a peek-compatibility projection, or replaces it, is now
an open call (J9).

### G7 — Re-ingest is delete-and-reinsert, and the delete has been masking defects

`_take_conversation_for_replacement` snapshots, deletes, reinserts under fresh
ULIDs, restores. `storage/replacement.py` exists to cancel the delete.

**New, measured, and the most consequential finding of this rewrite:** because
every re-ingest wipes first, **no write on this path has ever had to be
idempotent**, and non-idempotent writes are therefore invisible. Removing the
delete exposes them. Three found in one afternoon:

1. **A naive events upsert hard-fails on its first child insert.** Reproduced
   against the real schema: second write raises `IntegrityError: UNIQUE
   constraint failed: event_content.event_id, event_content.block_index`. It
   fails loudly rather than corrupting — but "no new constraint required" is
   false as a statement about the write.
2. **`set_attribute` does not upsert when scope is NULL.** Reproduced with the
   repo's own function: two calls with the same `(target, key)` and default
   scope produce **two rows**; with `scope='provider'` they correctly produce
   one. `UNIQUE (target_kind, target_id, key, scope)` cannot constrain NULL in
   SQLite. The live DB holds **18,330 NULL-scope rows and 0 duplicates** — the
   constraint is inert and the delete is what has been hiding it.
3. **FTS is insert-only.** `insert_fts_content` has no update or delete path, so
   repeated upsert leaves stale text searchable.

### G8 — A source record does not map one-to-one onto stored events *(new)*

Measured by parsing every adapter fixture and dividing stored events by source
records:

```
gemini_cli 1.50   claude_code 1.00   vscode 0.67
antigravity 0.57  pi_agent 0.57      copilot 0.44   codex 0.43
```

A source record emits **zero, one, or several** events. One Claude assistant
record yields a response *and* its tool uses; antigravity planner records declare
several tool calls; metadata and tool-result records emit nothing and instead
mutate events already emitted. **"One `StreamRecord` per source record" is
therefore not a viable contract**, and v1's `sequence: int` field cannot be a
source position.

### G9 — The same source record legitimately appears in more than one conversation *(new)*

```
events sharing an external_id with another conversation:  48,155
  of which in sub-agent conversations:                    20,409
  of which in ordinary conversations:                     27,746
```

Zero share a key across *kinds* within a conversation, so the
`(conversation_id, kind, external_id)` uniqueness holds. But event
`external_id` is **not globally unique**, and the sub-agent case is not the
whole story — the ordinary-conversation share is larger. Any reconciliation that
diffs "stored keys vs source keys" must therefore be scoped to an elected
authoritative source, which is what `_conversation_claimed_elsewhere`
(`orchestration.py:811`) currently provides and an upsert would otherwise
discard.

---

## Open judgment calls

**J1 — Reconciliation is a graph problem, not a key diff.** Upsert cannot see a
record *removed* from a source. Worse (review #11): an event can keep its key
while its `event_content`, attributes, tool results, or FTS text change or
vanish, and content identity is positional `(event_id, block_index)`. And
adapter changes alter old parses *without changing source bytes*, which ingest
skips. Reconciliation must compare the complete adapter-owned event graph
including descendants and derived indexes, and must include a parser revision.
Still the largest open question.

**J2 — Key stability and content mutability are separate declarations.** A
content hash cannot distinguish an illegal ordinal rewrite from a legitimate
mutable-record lifecycle: `opencode` reads primary keys whose rows evolve, and
antigravity's later system records deliberately mutate an already-emitted
background tool call. Proposal: adapters declare *key stability* and *content
mutability* independently; append-only sources get prefix/checkpoint
verification; mutable-identity sources get monotonic transition rules; an
illegal rewrite fails the whole transaction.

**J3 — Quote placement in a citation record.** Recommended sibling field, not
inside the locator. Unratified.

**J4 — Harness-prefix stripping on event keys.** Uniform or conversation-only.

**J5 — OpSpec registration for `resolve`.** A serve-side endpoint is forced
regardless; the open half is only whether the CLI delegates thin-client style.

**J6 — Does the tree view have a consumer?** And note review #7: raw source
parent pointers **cannot** be used directly as semantic `parent_id` — pi_agent's
assistant records point at tool-result records that are folded into a `ToolCall`
and never stored. Source parentage and semantic parentage are different edges
and both may need storing.

**J8 — Is `aider` worth keying at all?** `frozen`, zero local conversations,
markdown, K3/K4. Declining is legitimate; declining silently is not.

**J9 — Evolve `NormalizedRecord` or replace it?** New, from review #16: it has an
`extra` dict, so evolution with a peek-compatibility projection may dissolve the
net-new type entirely.

**J10 — Which ordering does `sequence` adopt?** New, forced by G5. There is no
status quo to preserve; this is a decision with a visible-behavior change.

---

## Dissolution check

**Dissolves**

| artifact | why | slice |
|---|---|---|
| `cline` / `cursor` / `goose` harness rows | residue; no module exists | 1 |
| `storage/replacement.py` (ingest half) | **only when the last replacement path leaves** — see below | 2+ |
| issue #83 — `replace_conversation()` | moot once one door stops replacing | 2+ |
| the nested tree in `domain/models.py` | storage is flat; the tree is intermediate | 3 |
| synthetic-prompt fabrication | a record needs no parent to be stored | 3 |
| the ordering ambiguity (G5) | one recorded sequence replaces four expressions | 3 |
| "exactly one conversation per source" | a stream yields as many as it yields | 3 |
| 7 × `normalize_record()` | projection, if J9 goes that way | 4 |

**Withdrawn from v1's ledger** (review #13, accepted): `dropped_events`,
`dropped_blocks`, and block-tag re-pointing do **not** dissolve. A fallback
replacement path survives slice 2, so the loss counters must stay for it; and
even under upsert, rewriting `(event_id, block_index)` can silently move an
existing block tag onto different content. Block carryover is not dissolved
until block identity is settled or explicitly refused.

**Net-new, with justification**

- **Stream record type** — pending J9; may reduce to evolving `NormalizedRecord`.
- **`sequence` column** — net-new; G5 shows there is no coherent order today.
- **Reconciliation (J1)** — net-new, and the honest price of removing the delete.
- **Key-stability / content-mutability declarations (J2)** — net-new.
- **Parser revision in ingest bookkeeping** — net-new, forced by slice 1 (below).

**Expectation, restated:** this does not shrink the codebase. Complexity moves
out of mutable parse-time state into keys, declarations, and reconciliation —
better, because those are testable and position state is not.

---

## Architecture

### Emission model *(replaces v1's one-record-per-source-record)*

G8 kills the 1:1 contract. An adapter emits a **sequence of event operations**
per source record — zero, one, or several — each carrying its own identity:

```
EventOp
  op                create | patch          # tool-result records patch, not create
  kind              prompt | response | tool_call
  ingest_key        IngestKey               # K1/K2/K3, class declared
  citation_key      CitationKey | None      # None for K3 — refused, with reason
  source_position   (record_index, emission_index)   # provenance, not identity
  parent            SemanticParent | None   # NOT the raw source predecessor (J6)
  source_parent     str | None              # raw provenance, kept separately
  content_blocks    list[Block]
  attributes        dict[str, str]
  input_tokens      int | None              # None ≠ 0
  output_tokens     int | None
  model             str | None
```

Plus a **conversation envelope** (review #10) carrying conversation identity,
workspace, branch, and time bounds — which v1 had nowhere to put.

### The write path

Upsert is a **graph** operation, not a row operation (G7):

```
per conversation, one transaction:
  upsert events            ON CONFLICT (conversation_id, kind, external_id)
                           RETURNING id          # never mint-and-assume
  reconcile event_content  upsert by (event_id, block_index); DELETE vanished
  reconcile attributes     requires the NULL-scope fix first
  reconcile FTS            explicit replace, or scoped rebuild in-transaction
  adjust blob refcounts
```

Every step needs specified semantics before slice 2 is buildable. The naive
version does not merely underperform — it raises `IntegrityError` on the second
write.

---

## Schema (sketch) — v12 → v13

```sql
ALTER TABLE events ADD COLUMN sequence INTEGER;
CREATE INDEX idx_events_conversation_sequence ON events(conversation_id, sequence);
ALTER TABLE events ADD COLUMN source_hash TEXT;         -- K3 verification (J2)
ALTER TABLE ingested_files ADD COLUMN parser_revision TEXT;   -- forced by slice 1
```

Plus a fix for `attributes.scope`: either `NOT NULL DEFAULT ''` or a partial
unique index, so the existing UNIQUE actually constrains the 18,330 NULL-scope
rows it currently does not.

---

## Migration

**v1's "semantically a no-op" claim is withdrawn.** G5 shows four ordering
expressions that disagree on 44,822 events, so there is no single legacy
semantics to reproduce. Sequence backfill is a *choice* (J10) with visible
behavior change, and the ordering expressions must be inventoried and
centralized first.

No backfill of `external_id` for NULL rows: sources must be re-parsed by fixed
adapters. A synthesized key would be indistinguishable from a real one and would
poison the citation guarantee.

---

## Test plan

Rebuilt per review #14, which correctly observed that v1's R6 tested a different
claim than its name and that R1 could not assert equality across independently
embedded SQL without a shared key definition. **Prerequisite: centralize typed
key definitions**, or none of these are assertable.

| id | property |
|---|---|
| **R1** | Same input ingested twice → stable ids, and unchanged row / FTS / attribute / blob-ref counts |
| **R2** | Ingest a prefix, then append → database identical to one full ingest |
| **R3** | Rewritten K3 source → detected, transaction unchanged |
| **R4** | Full reconcile → exact adapter-owned graph equality |
| **R5** | Every emitted parent resolves, or carries an explicit orphan reason |
| **R6** | Every adapter declares key class, key stability, content mutability, parser revision |
| **R7** | Record conservation across the adapter inventory — no adapter silently drops a source record (generalizes G2) |
| **R8** | `DESTROY_SITES` keeps its ingest entry while any replacement path remains; shrinks only when the last one leaves |
| **R9** | Emitted keys are unique within `(conversation, kind)` — enumerated over fixtures *and* live corpus, since fixtures are too small to catch G4's collision |

R8 replaces v1's assumption that the ratchet shrinks at slice 2; review #4 is
right that it must not while a fallback exists. Owner-scoping of resolution arms
moves to the citation plan, where it belongs.

**Falsification discipline:** each shown failing before its fix, and re-falsified
after any detector rewrite.

---

## Slices

**Slice 0 — Ratchet. SHIPPED.** #79, main `58daf4a`.

**Slice 1 — Keys.** Extract K1 ids never read: `pi_agent` (16,598 responses),
`copilot_cli`, `opencode`. Construct K2/K3 identities for `antigravity_cli` and
`codex_cli` under the J2 declarations. Sweep the `cline`/`cursor`/`goose`
residue.

**Not schema-free** *(review #3, accepted)*. Ingest skips unchanged files by stat
then hash (`orchestration.py:435,449`) and `ingested_files` records no parser
revision, so an adapter-only fix changes nothing until a file changes. Slice 1
must add `parser_revision` to ingest invalidation, or ship an explicit one-time
re-parse for affected harnesses. **Gate:** re-parse actually occurs, then G3's
query moves.

**Slice 2 — Upsert.** The graph upsert above, plus the `attributes.scope` fix and
FTS replacement. Fallback replacement retained for K4 and rewritable sources,
and reported as a Caveat. Source authority (`_conversation_claimed_elsewhere`)
retained or generalized — G9 shows 48,155 events share keys across
conversations, so authority is load-bearing. **Gate:** R1 and R2 green; ULIDs
stable; `DESTROY_SITES` unchanged (R8).

**Slice 3 — Stream.** The emission model, the envelope, `sequence`, schema v13.
Nine adapters, six with almost no validation corpus. The cost is concentrated
here.

**Slice 4 — Project.** Peek as projection, pending J9.

**Slice 5 — Live.** Tail, poll, or batch as a declared per-adapter capability.
Lands on arcs settled under bounded-write assumptions (#43/#42/#47, #38, the
0.12.1 tag-loss arc) and adds a daemon.

---

## Decided (flagged, not asked)

- **Locator grammar:** `<harness>/<conv-key>[/<event-kind>/<event-key>]`, `/`
  delimiter, colons unescaped (legal `pchar`), percent-encoding only for space,
  `/`, `%`. No scheme, no ULID, no ordinal.
- **The durable form contains no ordinal** — which is precisely why K3 keys are
  `IngestKey` only.
- **Refusals are part of the grammar**, with a stated reason.
- **A locator is an address, not a credential.** Auth inherited from the
  deployment regime; cross-owner is 404, not 403.
- **The merge door is out of scope.**
- **Block anchoring may dissolve rather than ship** — 98% of events are
  single-block, and a quote outperforms a positional block index.

---

## Review R1 disposition

| # | sev | disposition |
|---|---|---|
| 1 | P1 | **Accepted, verified.** G5 rewritten; no-op backfill claim withdrawn; J10 filed. |
| 2 | P1 | **Accepted.** Key taxonomy K1–K4 is now the doc's spine. |
| 3 | P1 | **Accepted, verified.** Slice 1 is not schema-free; `parser_revision` added. |
| 4 | P1 | **Accepted.** R8 replaces the shrink assumption. |
| 5 | P1 | **Accepted, reproduced** — and it hard-fails rather than corrupting. Graph-upsert section added; `attributes.scope` fix scheduled. |
| 6 | P1 | **Accepted.** `IngestKey` / `CitationKey` split; the identity claim became a containment. |
| 7 | P1 | **Accepted.** Semantic parent and source parent are separate fields (J6). |
| 8 | P1 | **Accepted**, and strengthened by G9's 48,155 shared keys. |
| 9 | P2 | **Accepted, verified.** G4 rebuilt around uniqueness rather than presence. |
| 10 | P2 | **Accepted**, and generalized by G8's measured 0.43–1.50 emission ratio. |
| 11 | P2 | **Accepted.** J1 restated as a graph problem including parser revision. |
| 12 | P2 | **Accepted.** J2 splits key stability from content mutability. |
| 13 | P2 | **Accepted.** Those three dissolutions withdrawn from the ledger. |
| 14 | P2 | **Accepted.** Test plan rebuilt as R1–R9 with a typed-key prerequisite. |
| 15 | P2 | **Accepted, verified.** FTS is insert-only; replacement is in slice 2's scope. |
| 16 | P3 | **Accepted.** Seven, not six; `extra` exists; J9 filed. |
| 17 | P3 | **Accepted.** G1 narrowed. |

Nothing rejected. Two findings were strengthened beyond what the review claimed
(#8 via G9, #10 via G8), and one was weakened in the design's favour (#5 fails
loudly rather than silently).

---

## Reproduction

```bash
# G3 — keyed rate per harness and kind
sqlite3 ~/.local/share/siftd/siftd.db "
  SELECT ha.name, e.kind, COUNT(*), SUM(e.external_id IS NULL)
  FROM events e JOIN conversations cv ON cv.id = e.conversation_id
  JOIN harnesses ha ON ha.id = cv.harness_id GROUP BY 1, 2;"

# G5 — ordering expression inventory
rg -n 'ORDER BY' src/siftd/ | rg -i 'timestamp|block_index'

# G5 — events inside a tied group
sqlite3 ~/.local/share/siftd/siftd.db "
  SELECT SUM(n) FROM (SELECT COUNT(*) n FROM events
  GROUP BY conversation_id, kind, timestamp HAVING n > 1);"

# G9 — keys shared across conversations
sqlite3 ~/.local/share/siftd/siftd.db "
  SELECT COUNT(*) FROM events WHERE external_id IN (
    SELECT external_id FROM events WHERE external_id IS NOT NULL
    GROUP BY external_id HAVING COUNT(DISTINCT conversation_id) > 1);"

# G7.2 — set_attribute does not upsert at NULL scope
#   two set_attribute calls, same (target, key), default scope → two rows
# G8 — emission ratio: parse each fixture, divide stored events by source records
#   (both scripts in the session transcript)
```

Companion: `ingest-stream-review-r1-2026-08-13.md`. Artifacts: *The Tree in the
Middle*, *Unbending the Pipeline*.
