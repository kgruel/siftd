# Search-log capture — design (2026-07-07)

Store search queries + their result rankings as first-class data. Ratified for 0.11.0.
Three consumers, in priority order:

1. **UX** — recent searches / re-run, in CLI and web.
2. **Bench ground truth (deferred)** — behavioral labels from "user searched X, then opened
   conversation Y." Solves the paraphrase ground-truth problem: real queries in the user's
   own vocabulary instead of synthesized paraphrases.
3. **Product feedback** — validate the per-preset search defaults (the strategy system
   landing in parallel) against real usage.

**Scope for 0.11.0: capture only.** Surfacing is minimal (a recent-searches list, optionally
`siftd search --history`). Bench consumption is explicitly deferred until data accrues.

---

## Open judgment calls (please review)

- **OJ-1 — Result-ID retention granularity.** Store the full ranked ID list per search
  (top-N conversation IDs + rank), or only the count + a truncated head (e.g. top-10)? Full
  list is what bench GT wants; it also drives row/blob size. **Rec: store top-N (N=result
  count actually returned, capped at 50) as a JSON array in one column.** See
  [Volume](#retention--volume).
- **OJ-2 — Opened-signal linkage window for CLI.** The heuristic that binds a later
  `siftd query <id>` to a preceding search needs a time/session window. **Rec: same session
  (via `siftd register`/session-id file) OR, absent a session, a 30-min wall-clock window,
  most-recent-search-wins.** See [Opened signal](#opened-signal).
- **OJ-3 — Sync scope.** Local-only vs synced to the homelab aggregator. **Rec: local-only
  in 0.11.0.** Search history is personal telemetry, not conversation data; the sync slice
  is keyed on conversations and would not carry it without net-new work. See [Sync](#sync-decision).
- **OJ-4 — Capture default on/off.** Ship on-by-default with an opt-out
  (`search.log = false`), or opt-in? **Rec: on by default, opt-out** — the data is
  local-only and low-sensitivity, and the whole point is that it accrues passively. A
  privacy note in docs + a `search.log` config key.
- **OJ-5 — Fire-and-forget vs inline write.** Search latency must not regress. **Rec:
  inline but post-response** — write after the `SearchView` is built, on the same
  thread/connection, wrapped so a write failure never fails the search. Not a background
  thread (SQLite + our `commit=False` connection convention make that fragile). See
  [Write path](#capture-point--write-path).
- **OJ-6 — Do we log zero-query facet-only searches?** `search_view` supports tag-facet
  enumeration with no query text. **Rec: skip capture when `q` is empty** — no query text
  means nothing for UX re-run or GT to key on.
- **OJ-7 — Agent issuer resolution.** Can we actually distinguish an agent-issued CLI
  search from a human-issued one? The CLI process looks identical. **Rec: default CLI
  issuer = `cli`; allow an explicit `SIFTD_ISSUER=agent` env / `--issuer` override that the
  session-start hook or agent harness sets.** Honest about the limit rather than guessing.
  See [Issuer taxonomy](#issuer-taxonomy).

---

## Dissolution check

Before adding a table, the question is whether this is a property/composition of existing
substrate.

- **Is it an `event`?** No. The `events` table (`kind IN prompt|response|tool_call`) models
  *conversation content ingested from logs*, keyed to a `conversation_id`. A search is an
  *action the user/agent took against the store itself* — it has no conversation, no
  harness, no ingested file. Forcing it into `events` would require a synthetic conversation
  and pollute every conversation-scoped query, rollup, and FTS index. Rejected.
- **Is it an `attribute`?** No. `attributes` hang schemaless KV off an existing entity;
  a search has no host entity.
- **Is there an existing side-table pattern to reuse?** Yes — the **`ensure_*_table`
  lightweight-side-table pattern** (`tag_pins`, `workspace_pins`, `active_sessions`,
  `sync_inbox`). These are operational/preference tables created idempotently on write-open
  **without a schema-version bump**, with reads guarded on table presence. Search-log capture
  is the same shape: owner-scoped operational data, not core ingested facts.

**Verdict:** net-new tables (`search_events` + the opened-signal linkage), but they *dissolve
into the existing `ensure_*_table` convention* — no new mechanism, no `SCHEMA_VERSION` bump,
no migration cost. This is the cheapest possible addition. What is genuinely net-new and
justified: the **capture call site** in the API search Operation, and the **fingerprint**
concept (there is no existing record of "which engine config produced this ranking").

---

## Schema (DDL sketch)

Two tables. Both created by a new `ensure_search_log_tables(conn)` added to the
`ensure_*` block in `storage/sqlite.py::create_database` (alongside `ensure_session_tables`).
No `SCHEMA_VERSION` bump — existing DBs get the tables on next write-open; reads guard on
presence via a `has_search_log_table(conn)` helper (mirrors `has_tag_pins_table`).

```sql
-- One row per executed search.
CREATE TABLE IF NOT EXISTS search_events (
    id            TEXT PRIMARY KEY,          -- ULID (sortable by time; no separate ts index needed)
    query         TEXT NOT NULL,             -- the raw query string
    issued_at     TEXT NOT NULL,             -- ISO timestamp (redundant with ULID; explicit for humans/filters)
    issuer        TEXT NOT NULL,             -- 'cli' | 'agent' | 'web'  (first-class column)
    owner         TEXT NOT NULL DEFAULT '',  -- serve-side tenant; '' = local/unscoped (matches tag_pins convention)

    -- Config fingerprint: which engine produced this ranking.
    fp_backend    TEXT,                      -- embed backend name  (e.g. 'fastembed', 'ollama'); NULL for fts-only
    fp_model      TEXT,                      -- embed model         (e.g. 'BAAI/bge-small-en-v1.5')
    fp_dimension  INTEGER,                   -- embed dimension
    fp_strategy   TEXT NOT NULL,             -- 'narrow' | 'rrf' | 'fts'  (the per-preset strategy field)
    fp_preset     TEXT,                      -- named preset that resolved these settings, if any
    fp_recall     INTEGER,                   -- FTS recall width
    fp_mmr_lambda REAL,                      -- MMR lambda (NULL if rerank != mmr)
    fp_mode       TEXT NOT NULL,             -- requested mode: 'hybrid' | 'semantic' | 'fts'
    executed_mode TEXT NOT NULL,             -- the mode that ACTUALLY ran (SearchView.executed_mode) — degrade-aware

    -- Result summary.
    result_ids    TEXT NOT NULL,             -- JSON array of conversation IDs in rank order (top-N, N<=50) — OJ-1
    result_count  INTEGER NOT NULL,          -- total results returned (may exceed len(result_ids) if truncated)

    -- Optional facet context (so "searched X within workspace Y" is reconstructable).
    workspace     TEXT,                      -- workspace filter, if any
    session_id    TEXT                       -- harness session id (from siftd register), if resolvable — links opens
);

CREATE INDEX IF NOT EXISTS idx_search_events_owner_time ON search_events(owner, issued_at);
CREATE INDEX IF NOT EXISTS idx_search_events_session   ON search_events(session_id) WHERE session_id IS NOT NULL;

-- One row per observed "open" following a search. Separate table because opens arrive
-- LATER than the search (web click, or a subsequent `siftd query`), and one search can
-- yield multiple opens.
CREATE TABLE IF NOT EXISTS search_opens (
    id               TEXT PRIMARY KEY,       -- ULID
    search_event_id  TEXT NOT NULL REFERENCES search_events(id) ON DELETE CASCADE,
    conversation_id  TEXT NOT NULL,          -- what was opened (NOT FK'd to conversations — may be a synced/foreign id)
    rank             INTEGER,                -- position of that conv in the search's result_ids (NULL if not in list)
    opened_at        TEXT NOT NULL,          -- ISO timestamp
    surface          TEXT NOT NULL           -- 'web-click' | 'cli-heuristic'
);

CREATE INDEX IF NOT EXISTS idx_search_opens_event ON search_opens(search_event_id);
```

Notes:
- **`result_ids` as JSON, not a child rows table.** A `search_result_ranks(search_event_id,
  conversation_id, rank)` child table is the "normalized" choice, but ranks are only ever
  read back as a whole ordered list per search — there is no query that filters *across*
  searches by rank. JSON in one column is the dissolution-correct shape (composition, not a
  new relation to join). If bench later needs rank-level SQL, promote then.
- **`conversation_id` is not FK-constrained** in `search_opens`/`result_ids`. A conversation
  can be re-ingested (new ULID) or, on a homelab aggregator, foreign. A dangling ID is a
  tolerable label-noise source, not a corruption. Enforcing the FK would delete history on
  re-ingest — worse.
- **ULID PKs** per the project convention (sortable by creation time, so `issued_at` needs no
  separate index for recency ordering — but we keep the `owner`-scoped composite index for
  serve tenanting).

---

## Capture point & write path

**Where.** The single capture site is **`api/search.py::search_view`** — the one Operation
that CLI (`cli/search.py::cmd_search` via `dispatch`), the REST route
(`serve/routes.py::search_route`), and the HTML Find view all funnel through. It already
returns a `SearchView` carrying `executed_mode` (the degrade-aware actual engine) and the
result chunks. Capturing here means:

- One implementation, three surfaces — no drift (same reason the recipe lives here).
- Respects the `api/` boundary rule: CLI and serve never touch storage directly; the capture
  write goes through a storage function (`storage/search_log.py::record_search`) called from
  the API layer, exactly like every other write.
- `executed_mode` and the resolved fingerprint are both in scope at this point.

The fingerprint is assembled from the resolved engine params already flowing into
`search_view` (`mode`, `rerank`, `lambda_`, `recall`) plus the resolved backend
(`name`/`model`/`dimension` from `_resolve_search_backend`) and the strategy/preset from the
new per-preset system (`_hybrid_strategy()` today; the preset name once that lands).

**Write path (OJ-5).** Inline, post-response, best-effort:

```
result = <build SearchView as today>
if q.strip() and config.search.log:            # OJ-6: skip empty-query facet searches
    try:
        record_search(<own write connection>, fingerprint, result, issuer, owner, session_id)
    except Exception:
        log.debug("search-log capture failed", exc_info=True)   # never fail the search
return result
```

- **Not a background thread.** SQLite connections aren't thread-safe to share, and our
  `commit=False` convention hands transaction control to the caller — a detached thread would
  need its own connection and its own open/close, adding latency and lock contention against
  the search's read connection. The write is tiny (one row + a JSON blob); inline cost is
  sub-millisecond and happens *after* results are computed, so perceived latency is unchanged.
- **Latency guard:** the capture is strictly after the `SearchView` is assembled and never
  blocks the return value's computation. Wrapped in try/except so a locked DB / disk-full
  degrades to "no capture," never to a failed search.
- **Read-only search connection caveat:** search opens the DB `read_only=True`. Capture needs
  a **separate short write connection** (open → insert → commit → close), which is why this is
  a distinct `record_search` call, not a piggyback on the search connection. This is the one
  real cost; measured it should be a single fast write on the local DB. If a benchmark shows
  regression, fall back to a bounded in-process queue flushed on process exit — deferred until
  measured.

---

## Opened signal

The join between a search and a later open is what turns capture into behavioral GT. Two
surfaces, two mechanisms:

**Web (easy, precise).** The result list is rendered server-side with the `search_event_id`
in scope. Each result link carries it (e.g. `hx-get="/conversation/{id}?from_search={sid}"`
or a small beacon). The conversation route records a `search_opens` row (`surface='web-click'`,
`rank` looked up from the originating search's `result_ids`). Precise: the click *is* the open.

**CLI (heuristic).** There is no click. The signal is "a `siftd query <id>` / `siftd show <id>`
whose `<id>` appears in a recent search's `result_ids`." Linkage rule (OJ-2):

1. On `siftd query <id>`, resolve the current `session_id` from the session-id file
   (`siftd register` wrote it; `session_id_file(workspace)`).
2. Find the most recent `search_events` row that (a) shares that `session_id`, OR (b) if no
   session, was issued within the last 30 minutes for the same `owner`, AND (c) has `<id>` in
   its `result_ids`.
3. If found, insert a `search_opens` row (`surface='cli-heuristic'`, `rank` = position in
   `result_ids`).

This is deliberately conservative: it only fires when the opened conversation was *in the
search's result set*, so an unrelated `query` never creates a spurious label. The capture site
is `cli/query.py` (and `show`), guarded on table presence, best-effort like the search write.

**Epistemics (must be stated wherever this data is consumed).** Opens are *noisy positives*:
- Opened ≠ relevant — the user may open a result, find it wrong, and move on.
- **Position bias** — top-ranked results get opened more regardless of true relevance.
- The CLI heuristic can mis-bind (two searches close in time) or miss (user pastes an ID from
  memory).
- Absence of an open is **not** a negative label — the user may have found the answer in the
  snippet, or given up.

Therefore: this data is **good for regression comparison** ("did config B get more/higher-ranked
opens than config A on the same queries?") and as **an additional GT class alongside** the
synthetic paraphrase set — never as absolute relevance truth. The bench must treat it as a
weak signal and weight accordingly. Documented here so the deferred bench consumer inherits the
caveat.

---

## Issuer taxonomy

`issuer` is a first-class column because **agents are the primary search consumers** in this
product (per the siftd agent-consumer pattern). Three values:

- **`web`** — precise. The serve route sets it; every `/api/v1/search` call is `web`.
- **`cli`** — a human at a terminal. Default for the CLI.
- **`agent`** — an agent driving the CLI. **Cannot be auto-detected** (the process is identical
  to a human CLI invocation), so it is set explicitly via `SIFTD_ISSUER=agent` in the env (the
  session-start/agent hook exports it) or a hidden `--issuer` flag. Default falls back to `cli`
  (OJ-7). Honest degradation: an un-instrumented agent logs as `cli`; the column never lies,
  it just under-counts `agent`. Since the session-start hook already calls `siftd register`,
  extending it to export `SIFTD_ISSUER=agent` is the natural instrumentation point.

---

## Fingerprint fields

The fingerprint answers "which engine config produced this ranking," so a later analysis can
group opens by config and validate defaults. Fields (all in `search_events`):
`fp_backend`, `fp_model`, `fp_dimension`, `fp_strategy` (**the per-preset strategy field** —
`narrow`/`rrf`/`fts`), `fp_preset` (named preset if the parallel preset system resolved one),
`fp_recall`, `fp_mmr_lambda`, `fp_mode` (requested), `executed_mode` (actual, degrade-aware).
The strategy + preset fields are the load-bearing ones for consumer #3 (validating per-preset
defaults); the rest let a future re-index / model change be excluded from cross-config
comparisons (a fingerprint mismatch means the rankings aren't comparable).

---

## Retention & volume

Agents search a lot. Estimate:
- Heavy agent day: ~50–200 searches/day. Human: ~5–20/day.
- Row size: query (~50 B) + fingerprint (~150 B) + `result_ids` JSON (10–50 IDs × 27 B ≈
  0.3–1.4 KB) + overhead ≈ **~1–2 KB/row**.
- **~200 searches/day × 2 KB ≈ 0.4 MB/day ≈ ~12 MB/month ≈ ~150 MB/year** at the heavy end;
  realistically a fraction of that. `search_opens` is far smaller (most searches yield 0–2
  opens).

Against a ~2.4 GB live DB this is negligible for years. **No pruning needed in 0.11.0.** Add a
`search.log_retention_days` config key (default: unlimited) and a `siftd search --prune` /
doctor-driven prune later *if* volume becomes a concern — deferred, flagged, not built.
The ULID PK makes time-window pruning a trivial prefix range delete when it's wanted.

---

## Sync decision

**Recommendation: local-only in 0.11.0.** Justification:

- **The sync slice is conversation-keyed.** `sync_push` → `slice_database()` selects
  *conversations* (and their events/content) filtered by workspace/tag/owner/date. Search
  history has no conversation. Including it would be net-new slice logic + a new merge path on
  the receive side — cost with no consumer yet.
- **It's personal operational telemetry, not shared knowledge.** The homelab aggregator's
  purpose is a unified *conversation* view across machines. My search queries are private
  workflow data; pushing them to a shared server changes the data's sensitivity class
  (queries can contain sensitive strings — secrets, names, intent). The `tag_pins`/
  `workspace_pins` precedent is instructive: those are also owner-scoped preference/operational
  tables and are **not synced**. Search-log follows the same rule.
- **The consumers don't need sync.** UX re-run is inherently per-machine ("my recent
  searches"). Bench GT runs against the local dev DB where the searches happened. Per-preset
  validation is per-user.

If a future homelab feature wants cross-machine search analytics, it's an explicit opt-in
addition (a `[sync] include_search_log = true` key + owner-scoped merge), justified then by a
real consumer. Flagged as future, not built. The `owner` column is already present so the data
is *ready* to be tenanted if that day comes — the table shape doesn't foreclose it.

---

## Minimal surfacing (0.11.0)

Capture is the deliverable; surfacing is intentionally thin:

- **`siftd search --history [N]`** — list the last N searches (query, when, result_count,
  issuer, executed_mode). Reads `search_events` ordered by ULID desc, owner-scoped.
- **Re-run** — `siftd search --history` output is copy-pasteable; optionally
  `siftd search --rerun <n>` re-issues search #n. (Rerun is a nice-to-have; the list is the
  floor.)
- **Web** — a "recent searches" affordance on the Find view (dropdown / below the search box),
  reading the same `search_events` via a small route. Reuses the owner scoping already in the
  serve layer.

No analytics dashboard, no per-preset validation UI — those wait for data + the deferred bench
consumer.

---

## Migration plan

- **No `SCHEMA_VERSION` bump.** Add `ensure_search_log_tables(conn)` to the `ensure_*` block in
  `create_database` (runs on every write-open, idempotent `CREATE TABLE IF NOT EXISTS`). This
  is the established pattern for operational side-tables (`tag_pins`, `workspace_pins`,
  `active_sessions`, `sync_inbox`) — they all landed without a version bump.
- **Reads guard on presence** (`has_search_log_table`) so a read-only open of an
  older-but-not-yet-write-opened DB degrades to "no history" instead of `no such table`.
- **Backfill:** none. History starts accruing from first write-open post-upgrade. There is no
  historical search data to migrate (searches were never recorded).
- **Rollback:** dropping the feature drops two tables; no core data touched. (Per the
  dissolution-residue rule, a real removal would also strip the capture call sites, config key,
  and CLI/web surfacing.)

---

## Test plan

- **Storage unit** (`tests/storage/test_search_log.py`): `ensure_*` idempotency;
  `record_search` round-trip incl. `result_ids` JSON encode/decode; `has_search_log_table`
  guard on a table-less DB; owner scoping.
- **Capture integration** (`tests/api/test_search_capture.py`): `search_view` writes exactly
  one `search_events` row with the correct fingerprint + `executed_mode`; a **degraded** search
  (embed backend unreachable → fts) records `executed_mode='fts'` while `fp_mode='hybrid'`;
  empty-query facet search records **nothing** (OJ-6); a capture-write failure (patched to
  raise) does **not** fail the search (OJ-5 best-effort).
- **Opened-signal** (`tests/api/test_search_opens.py`): web-click path records a
  `search_opens` row with correct `rank`; CLI heuristic binds a `query <id>` to the right
  recent search within the session/window and records `rank`; an unrelated `query <id>` (not in
  any `result_ids`) records **nothing**; two close searches bind to the most recent matching one.
- **CLI argparse** (per the argparse-test-gap rule): `search --history` / `--rerun` parse and
  dispatch; `--issuer` override honored; `SIFTD_ISSUER=agent` reflected in the row.
- **Serve E2E smoke** (`tests/test_serve_e2e_smoke.py`): `/api/v1/search` records `issuer='web'`;
  recent-searches route is owner-scoped (IDOR guard — a second owner sees none of the first's).
- **No latency regression:** an assertion-light timing sanity in the capture test (capture
  path adds a bounded single-row write); not a perf gate, just a guard that capture is off the
  result-computation path.

---

## Slice breakdown

1. **S1 — Storage substrate.** `storage/search_log.py` (`ensure_search_log_tables`,
   `record_search`, `has_search_log_table`, `recent_searches`, `record_open`, `find_open_link`);
   schema tables added to `create_database`. Storage unit tests. No wiring yet — pure
   substrate, independently green.
2. **S2 — Capture at the Operation.** Wire `record_search` into `api/search.py::search_view`
   (fingerprint assembly, issuer/owner/session resolution, best-effort write, `search.log`
   config key + OJ-6 empty-query skip). Capture integration + degrade tests.
3. **S3 — Opened signal.** Web-click linkage (serve route + result-link instrumentation) and
   CLI heuristic (`cli/query.py`/`show` binding). Opened-signal tests + serve smoke.
4. **S4 — Minimal surfacing.** `siftd search --history` (+ optional `--rerun`); web recent-
   searches affordance. CLI argparse + serve owner-scope tests.
5. **S5 — Docs + residue sweep.** Config-key docs (`search.log`, retention), privacy note
   (local-only, sensitive-query warning), epistemics note wherever GT is described. `./dev
   docs --check`, `./dev check`.

Issuer instrumentation of the session-start hook (`SIFTD_ISSUER=agent`) rides S2/S3. Bench
consumption of the accrued labels is **out of scope** — a separate arc once data exists.
