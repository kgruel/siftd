# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`siftd ingest` now imports every session in an aider chat history, not just
  the first.** Files already stuck on this recover on the next ingest without
  having to change again. ([#36](https://github.com/kgruel/siftd/issues/36))

- **`siftd db pull`/`push` no longer fails permanently once the other side
  re-ingests a conversation you already have.** The replaced conversation also
  stops answering searches from its deleted text.
  ([#20](https://github.com/kgruel/siftd/issues/20))

- **`siftd query`, `search`, and `show` no longer read a snapshot older than
  the database** while `ingest` or `serve` holds un-checkpointed commits, and
  no longer report corruption in a healthy database when a writer checkpoints
  mid-read. ([#42](https://github.com/kgruel/siftd/issues/42))

  **The trade, stated plainly:** the 0.8.0 note below promising that "read-only
  commands no longer create surprise WAL/SHM sidecars" is retired for
  `query`/`search`/`show`, and for `doctor` below. Seeing committed data
  requires a plain `mode=ro` reader, which takes WAL read marks and so creates
  `-wal`/`-shm` it cannot remove on close. Any write to the database creates
  them anyway; the change is that a read alone now does too. Reads on genuinely
  read-only media are unaffected.

- **`siftd doctor` no longer reports on a snapshot older than the database.**
  Same cause as #42 above, found first here.
  ([#38](https://github.com/kgruel/siftd/issues/38))

- **Every default `db pull` after the first one works again.** `--since` and
  `--before` now accept ISO 8601 timestamps — which is what sync's own stored
  cursor is — anywhere the date vocabulary is accepted.
  ([#21](https://github.com/kgruel/siftd/issues/21))

- **`siftd db send`/`push`/`pull` explain a bad `--since` again**, instead of
  printing `invalid parse_date value`.

- **Aider conversations no longer disappear from delta sync**, which skipped
  them silently and permanently on any host west of UTC. Existing aider rows
  keep their local-time `started_at` until their history file changes and is
  re-ingested. ([#31](https://github.com/kgruel/siftd/issues/31))

- **`siftd doctor` no longer reports health it did not measure** — roughly one
  run in seven called an empty FTS index healthy — and runs ~18% faster on a
  4.8 GB database. ([#34](https://github.com/kgruel/siftd/issues/34))

### Internal

- An architecture ratchet enumerates every read-only open that asserts
  `immutable=1` instead of deriving it, against a now-empty, shrink-only
  allowlist (`tests/architecture/test_readonly_opens.py`).
  ([#43](https://github.com/kgruel/siftd/issues/43))

- `storage.embeddings.EmbeddingsConnection`, its `siftd_immutable` flag, and
  the `PRAGMA wal_checkpoint(TRUNCATE)` after a read-only auto-upgrade are gone
  — all three existed only because an open could be blind to the WAL.
  ([#42](https://github.com/kgruel/siftd/issues/42))

- CI installs uv via `astral-sh/setup-uv@v7`; `v4` declares `node20`, which
  GitHub force-runs on Node 24, surfacing as an intermittent `self-signed
  certificate` failure. ([#34](https://github.com/kgruel/siftd/issues/34))

## [0.12.1] - 2026-08-11

### Changed

- **Ingest stops full-scanning `ingested_files` on every replace.** A new
  `idx_ingested_files_conversation` index backs the "does another path point
  at this conversation?" question ingest now asks before replacing anything,
  and the `ON DELETE CASCADE` that fires on every conversation delete — both
  were table scans. Existing databases pick the index up on open; there is no
  migration to run.

### Removed

- **`cleanup_stale_sessions`**, which deleted stale session registrations
  *and* discarded their queued tags. It was never part of `siftd.api`'s
  documented surface, and it lost its last caller when
  `doctor fix --pending-tags` stopped treating deletion as a repair.
  `prune_stale_sessions` does the registration half without the data loss.

### Fixed

- **Pending session tags are applied again.** `siftd tag --session <id>`
  queues under the bare harness session id, but ingest drained the queue
  by the adapter-namespaced `external_id` (`claude_code::<uuid>`), so for
  Claude Code the queue never drained and every queued tag was silently
  lost. Ingest now falls back to the bare id (adapter-name prefix
  stripped, parent uuid for `::agent::` subagent transcripts), which also
  rescues queues stranded by earlier versions. The same mismatch left
  live sessions registered forever; ingest now unregisters the bare key
  too. Both key forms drain in one pass rather than only the first with
  rows: an agent tagging via `--current` queues under the prefixed id the
  session-start hook registered, while `siftd tag --session <uuid>` queues
  under the bare one, and a session routinely has both. (kgruel/siftd#28)
- **Ingest no longer discards a queued tag it could not apply.** The drain
  deleted every row for the session up front and only then resolved
  targets, so a `--last-tool-call` queued before any tool ran, or an
  `--exchange N` past the end of a still-growing transcript, was destroyed
  with nothing left for `doctor` to see. Rows are now consumed only once
  they have actually been applied; the rest stay queued for the next
  ingest or for `siftd doctor fix --pending-tags`. Ingest and recovery now
  share one target resolver, so they can no longer disagree.
- **Session tags stay with the session, not a subagent.** A subagent
  transcript shares its session's queue keys with the parent, so an ingest
  that reached the subagent first — routinely, when the parent is
  byte-stable while the Agent is still writing — took the tag and left
  `siftd query -l` pointing at a sidecar transcript. The drain now agrees
  with the recovery path (which already skipped subagent rows) and leaves
  those rows queued for the parent — and leaves the parent's session
  *registration* in place while it does. Unregistering every key form on a
  subagent's ingest told the recovery path the parent's queued rows were
  orphaned, so `doctor fix --pending-tags` would apply a `--last-*` marker
  against a parent transcript that was still being written. Ingest now
  unregisters exactly the keys it drained.
- **Conversation *and* element tags survive re-ingest, on every adapter.**
  When a transcript changes, ingest replaces the conversation row, and the
  polymorphic cleanup triggers took its `tag_assignments` with it — so
  tagging a live session lost the tag on the next ingest. Assignments are
  now snapshotted before the delete and re-pointed at the replacement rows
  (`applied_at` preserved) inside the same transaction: the conversation
  by its `external_id`, and prompt / response / tool-call / exchange tags
  by their event's `(kind, external_id)`, so a `--last-response` or a
  hand-applied element tag stays on the same turn instead of being
  destroyed by the next ingest. This now covers the session-dedup
  strategy (`gemini_cli`, `opencode`, `antigravity_cli`) as well as the
  file-dedup one, where the previous release note over-claimed. A
  replacement whose transcript no longer parses to a conversation has
  nothing to carry the tags to; it now says what was dropped instead of
  dropping it silently, and names every kind of loss it is holding — a
  conversation tagged only at block level skipped the warning entirely,
  because assignments that could never be re-pointed were not counted as
  "anything to report". Remaining limitations, deferred to 0.13.0:
  block-level tags (the trace-block surface) are not re-pointed, and
  `doctor fix --pending-tags` still resolves late-bound markers against
  whatever the transcript holds at that moment — now only a risk for a
  still-running session whose registration has lapsed, since a registered
  one is out of the fix's scope entirely. (kgruel/siftd#29)
- **`siftd doctor fix --pending-tags` repairs instead of deleting.** It ran
  `cleanup_stale_sessions`, so the remedy doctor advertised for stranded
  queued tags destroyed exactly the data that was recoverable. It now
  resolves each queued tag to the conversation its session became
  (matching the bare session id against the adapter-prefixed `external_id`,
  skipping subagent transcripts), applies it there — reusing the ingest
  drain's own resolution for `--last-*` and `--exchange` targets — and
  consumes the queue row. Rows that match no conversation are reported
  with their session key and **kept**; deleting them now takes the explicit
  `--discard-unresolved`. Recovery is scoped to sessions that are no longer
  registered, and "registered" now allows for either key form: the
  session-start hook registers `claude_code::<uuid>` while `tag --session`
  queues the bare uuid, so an exact-key scope called a *live* session's rows
  abandoned and resolved their `--last-*` markers against a half-written
  transcript, pinning the tag to a non-final turn. Stale session
  registrations are still pruned, but their queued tags are no longer
  pruned with them. This is the only
  recovery path for tags queued before the drain was fixed: a settled
  session never re-ingests. The `--json` output changes shape accordingly
  (`applied` / `unresolved` / `discarded` / `stale_sessions_pruned`,
  replacing `sessions_deleted` / `tags_deleted`), and the doctor check's
  wording no longer describes deletion as a fix. `unresolved` and
  `discarded` partition what was not applied — a row appears in exactly one,
  and `discarded` carries each deleted row's session key and reason rather
  than a bare count, so neither channel can present a deleted row as one
  that was kept.
- **`siftd doctor --strict` can reach green again.** Queued rows that the
  fix cannot apply are kept by design, but they were still counted as an
  actionable warning — so `--strict` (documented for CI) exited 1 forever
  and `doctor fix` kept advertising a fix that changed nothing. The check
  now reports three buckets, classified by the same resolvers the fix runs:
  rows whose session *and* target resolve stay a warning; rows waiting on a
  target the transcript does not hold yet (a `--last-*` marker with no such
  event, an `--exchange` past the end) become an `info` finding that says a
  later ingest may still land them; and rows whose session was never
  ingested become an `info` finding naming `--discard-unresolved`. That flag
  is scoped to the last bucket — a row still waiting on a target is one
  ingest away from landing, so discarding it would lose a live tag. The
  check also resolves the whole queue in a single pass over `conversations`
  instead of one full scan per queued session, which is what it takes to
  belong in doctor's fast lane on a real-sized database.
- **Concurrent ingests no longer poison a file forever.** Two overlapping
  `siftd ingest` runs (a cron entry plus a wrapper script on the same
  minute, or an event-driven ingest crossing a scheduled one) both parse the
  same changed transcript and both insert the same conversation; the loser
  hit `UNIQUE constraint failed: conversations.harness_id,
  conversations.external_id` and *discarded the winner's pointer*, leaving
  `ingested_files.conversation_id` NULL beside an orphaned conversation.
  That state is a fixed point — the NULL made the next re-ingest skip its
  delete, so it collided again — and a single process then reproduced the
  failure indefinitely: the transcript froze at whatever the first run
  captured, and search silently returned a stub. One reporter's host
  accumulated 415 such rows over eight days. Three changes: ingest's
  *database phase* now runs under a per-database advisory lock, so a second
  invocation reports briefly and exits 0 instead of racing (skipping is
  correct when an ingest is already writing). The lock is released before
  the post-ingest auto-index: embedding a stale set against a rate-limited
  remote backend can run for minutes after every write has landed, and
  holding the lock across that turned the window into silently skipped
  ingests — a cron tick or a scoped `--path` request getting `skipped` long
  after the conflicting writes finished. A duplicate-conversation collision
  now re-points the bookkeeping row at the conversation that already exists
  rather than clearing it, turning a lost race into a no-op. And the
  re-ingest path resolves the conversation by `(harness_id, external_id)`
  instead of trusting a NULL pointer, so rows already poisoned in the field heal
  themselves on the next ingest — including the ones whose transcript has
  since gone quiet, which is most of them: a row carrying an error is now
  re-examined whatever its stat says, because the failure write stamped the
  file's own hash and mtime and so hid it behind the unchanged-file skip
  forever. A row whose file is gone cannot be re-derived and keeps its
  marker; the conversation it produced stays indexed and searchable. The
  self-heal snapshots the orphan's tags and re-points them at the
  replacement, using the same machinery as a normal replacement — without
  that, the first ingest after upgrading would have destroyed the tags on
  every affected conversation at once. Thanks to the reporter whose
  cross-host analysis isolated this.
- **Ingest bookkeeping never asserts content it did not ingest.** Follow-up
  hardening on the above, from an adversarial review of the fix itself.
  The collision repair recorded the file's *current* hash and mtime while
  linking the conversation some *other* read had produced, so the next run's
  unchanged-file skip matched and the delta was never indexed — with the
  error cleared, the only signal was gone too. It now leaves the row's
  hash/stat at whatever was actually ingested, so the next run re-hashes and
  converges. Three related repairs: a failure after a successful ingest no
  longer NULLs the conversation pointer (the rollback has resurrected that
  conversation, so it is live, not stale — a transient `database is locked`
  used to orphan it permanently); the re-ingest path no longer deletes a
  conversation another path's bookkeeping row points at, which cascaded that
  row away and replaced a live transcript with a stale copy's content; and
  two paths carrying one session (a restored backup, an overlapping
  `--path`) settle on one conversation with a warning naming the duplicate,
  instead of taking turns replacing each other every run. That settlement
  now holds on the ordinary replace path too, which was the one delete site
  with no such guard: a content change on either copy deleted the shared
  conversation and cascaded the other copy's bookkeeping row and events
  away. A changed duplicate is linked, named, and left settled — its change
  is not ingested, because only the path holding the session's slot can
  write that conversation.
- **A locked-out ingest no longer looks like a successful one.** The lock is
  per-database, so `siftd ingest --path … --adapter …` blocked by a
  concurrent run did none of the work it was asked for — and said nothing,
  because output auto-quiets whenever stdout is not a TTY (every script and
  cron chain). A scoped run now reports the skip unless `--quiet` was passed
  explicitly. `siftd doctor fix` reported the same lock-out as an applied
  fix, printed "All fixes applied.", and cleared the finding from its cache;
  it now marks the step not applied and keeps the finding pending. When the
  advisory lock cannot be taken at all (an NFS mount that refuses `flock`),
  ingest still runs — refusing would be the worse failure — but now logs a
  warning instead of degrading silently.

## [0.12.0] - 2026-07-18

> The stewardship release. Adapters gain an explicit support contract —
> tiers, a per-adapter disable knob, staleness detection — plus a new
> family member (Antigravity CLI) and a round of parse-drift fixes for the
> ones that already ship. Errors stop being accidents: a two-branch
> exception taxonomy gives every failure a deliberate exit code, message
> shape, and HTTP status, enforced by an AST ratchet. And the web trace
> view learns per-block identity: tag any block where you read it, copy
> any block or tool payload verbatim.

### Added

- **Antigravity CLI adapter** — ingests `~/.gemini/antigravity-cli`
  sessions, with model identity resolved from the sidecar DB and
  background-task stitching.
- **Adapter support tiers + disable knob** — every adapter declares a
  support tier (surfaced by `siftd adapters`), and
  `adapters.<name>.enabled = false` turns an adapter off across ingest,
  peek, and doctor. A new `adapter-stale` doctor check flags adapters
  whose logs are present but no longer being picked up.
- **Trace-mode block surface (web)** — every content block in the trace
  view carries identity: a per-block tag affordance (block tags were
  already writable via CLI colon-paths; now they're visible and editable
  where you read), and verbatim copy for blocks and tool payloads via
  `/raw/{kind}/{id}` (`block`, `tool_input`, `tool_result`).
- **Exception reference** — generated `docs/reference/exceptions.md`
  documents the full error taxonomy and its exit-code/HTTP mapping.

### Changed

- **Errors carry a contract** — the `SiftdError` taxonomy splits failures
  into `UserInputError` (exit 2 / HTTP 400) and `DriftError` (exit 1 /
  HTTP 503). The CLI backstop renders clean one-line messages with
  optional hints instead of tracebacks — e.g. a stale embeddings index
  during `siftd search` now explains itself and suggests the fix — and
  serve maps the same taxonomy to status codes. Membership is enforced by
  an architecture ratchet test.

### Fixed

- **claude_code adapter drift** — `gitBranch`, `toolUseResult`, and usage
  parsing track the current log format.
- **codex_cli dual-path discovery** — sessions are found under both
  layout generations.
- **peek** — adapter alias drift refresh.
- **cli.md reference generator** — repaired via lane-registry
  introspection (the lanes help format had silently truncated it to zero
  sections); byte-reproducibility is now explicitly pinned to the
  canonical interpreter (Python 3.14 changed argparse usage layout).

### Internal

- Per-folder README documentation system with generated spans and a
  strict drift gate in `./dev check` and CI; `.claude` skills/commands
  tracked in-repo; hooks moved to tracked `.githooks`.
- Non-src simplification sweep: test-lane dedup, dead residue removed.
- Prefix-resolution primitive dissolved six copies of the
  resolve/ambiguity dance; `list_tags` count arms folded.

## [0.11.0] - 2026-07-08

> The search-coherence release. Element tagging lands as a first-class read
> path, and search stops being a matter of taste: embedding providers were
> benchmarked offline against a cached-artifact replica of the live engine,
> two redesigns (typed chunking, aggregate rollup) were **rejected on
> CI-backed empirical evidence**, and the one measured win shipped —
> dedup-on-RRF fusion, selected **per preset**: strong embedding presets
> (e.g. voyage) use RRF, weaker ones stay on narrow-then-rank. The search
> log begins accruing (local-only, opt-out), and the `query` command takes
> a deliberate clean break (see Breaking Changes).

### Breaking Changes (migration)

- **`siftd query` is a list command only — detail-view args and `query sql`
  removed** — `query` no longer accepts a positional conversation ID, the
  anchor/window flags (`--around`, `--turns`, `--from-start`, `--from-end`,
  `--at-turn`), `--summary`, or `--neighbors`; those belong to `siftd show
  <id>`, which already ran the identical detail handler. The deprecated
  `query sql <name>` alias is also removed. Both removed forms exit 2 with a
  redirect hint. Migrate: `query <id> ...` → `show <id> ...`;
  `query sql <name>` → `report <name>`.

### Added

- **Element tagging read-path** — tags can target conversation elements
  (prompts, responses, tool calls, blocks) via `TargetRef`, with filter-only
  retrieval (`siftd search --tag NAME` enumerates tagged elements without a
  query), `tag list --on KIND`, and web affordances for element tags.
- **Per-preset hybrid strategy (dedup-on-RRF)** — hybrid search fuses FTS and
  vector rankings with reciprocal-rank fusion plus conversation dedup on
  strong embedding presets, and keeps narrow-then-rank (recall 40, λ=0.7
  revalidated) elsewhere. Promotion was gated per preset by the offline
  bench: voyage cleared every promote gate; gemini failed the tool-query
  gate and ships narrow.
- **Search-log capture** — every executed search records its query, config
  fingerprint (backend/model/strategy/preset/recall/λ/mode), ranked result
  IDs, issuer (`cli`/`agent`/`web`), and a later "opened" signal (web-click
  precise; CLI heuristic, session- or 30-minute-window bound). **Local-only:
  never syncs, never leaves the machine.** On by default; opt out with
  `siftd config set search.log false`. Surface: `siftd search --history [N]`.
- **Bench harness (dev tool)** — the stage-1/2 offline search benchmark
  (`bench/stage1/`): cached-artifact replicas, ground-truth query classes,
  and a fidelity gate proving the replica reproduces the live engine's
  top-10 exactly. Standing policy: no search-behavior change ships without a
  bench re-gate (see `bench/README.md`).

### Rejected (measured, not shipped)

- **Typed chunking (stage 2)** — S1 ≤ S0 on both provider arms; hypothesis
  falsified, reverted.
- **Aggregate rollup** — conversation-dedup matched or beat aggregation
  everywhere it mattered; dedup shipped instead.

## [0.10.2] - 2026-06-26

### Fixed

- **`siftd install embed`/`serve` no longer drops the other extra** — on
  force-reinstall install methods (`uv tool`, `pipx`), `--force` rebuilds the
  environment from scratch, so installing one extra while the other was already
  present silently removed it. The reinstall command now preserves every
  already-installed extra (e.g. installing `serve` when `embed` is present runs
  `siftd[embed,serve] --force`).

## [0.10.1] - 2026-06-25

### Fixed

- **Bundled `siftd` skill updated for the 0.10.0 search surface** — the skill that
  ships with the package (installed into Claude Code / Pi / Codex / Gemini via
  `siftd install skill`) still documented the pre-0.10.0 CLI and would hand agents
  broken commands. It now uses `--view` for the result shape and `--mode` for the
  engine (`auto`/`fts`/`semantic`/`hybrid`), drops the removed `--embeddings-only`
  (use `--mode=semantic`), documents `search --around/--turns` directly (the removed
  `--context` is gone) plus the new `--tool`/`--tool-tag` filters, and replaces the
  removed `siftd tools` command with `siftd tag list --on tool_call`.

## [0.10.0] - 2026-06-25

> A UI-focused release in two halves — both reading surfaces rebuilt.
>
> **The web half** turns `siftd serve` into a real reading and analysis surface. The
> web UI is rebuilt as a single-rail "Swiss" shell — one canonical content area,
> URL-addressable state, light/dark tone — hosting first-class views: a conversation
> **folio** that toggles between a prose *reading* view and an event-sequence
> *trace*; a live **Sessions** view with sub-agent nesting; a Stats **reckoning**
> dashboard; **Tags** and **Workspaces** index views (both with per-owner pins); and
> a **Find** surface that runs the *actual* search engine over your facet filters,
> with the whole query carried in a shareable URL.
>
> **The terminal half** rebuilds the CLI's output on [painted](https://pypi.org/project/painted/):
> a bespoke "warm obsidian" theme (amber metric thread, cream body), a typographic
> transcript feed that renders markdown and drops the old border boxes, a search
> surface on a relevance rail, live progress bars, and a brand wordmark with a
> DC-match help redesign — plus a config-swappable `ui.theme`.
>
> The search layer behind both is overhauled: the engine (`--mode`) is now cleanly
> split from the result shape (`--view`), and the REST `/api/v1/search` route runs
> the full post-processing recipe and returns a render-ready `SearchView`. Several
> search flag/output/wire changes are **agent/script-facing** — see Breaking Changes
> first.

### Breaking Changes (migration)

- **`siftd search`: `--mode` now selects the engine; result shape moves to `--view`** —
  `--mode` is `auto`|`fts`|`semantic`|`hybrid` (default `auto` = hybrid when
  embeddings are installed, else fts). The render shape (formerly `--mode
  chunks|thread|conversations`) is now a separate `--view chunks|thread|conversations`
  flag. The old `--fts`, `--semantic`, `--embeddings-only`, and `--debug-ids` flags
  are **removed outright with no aliases**, and the legacy `--mode thread`/`--mode
  conversations` values now error with a hint to use `--view`. Migrate:
  `search --fts` → `search --mode fts`; `search --mode thread` → `search --view thread`.
- **Search output reports the resolved engine in `mode`; render shape moves to `view`** —
  search JSON/output now carries a first-class `mode` field naming the engine that
  *actually ran* (`fts`/`semantic`/`hybrid`) plus a separate `view` field for the
  render shape, uniformly across terminal, JSON, markdown, and serve. Previously
  `mode` named the render shape — that meaning is now in `view`. Scripts reading the
  search `mode` field must update (shape readers move to `view`).
- **`/api/v1/search` runs the full recipe and returns a `SearchView` envelope** —
  the REST search route now runs the same post-processing the CLI does
  (engine chunks → threshold/select/trim/enrich/sort/view-shape/full/around) and
  returns a render-ready `SearchView` (chunks/thread/conversations views plus
  `tier1`/`tier2`/`n_skipped`/`empty_reason`) instead of flat chunks. The deprecated
  `embeddings_only` query param is **removed** in favor of `mode`
  (`auto`|`fts`|`semantic`|`hybrid`); new recipe params
  `view`/`sort`/`select`/`threshold`/`full`/`around`/`turns` and the additive
  `tool`/`tool_tag` filters (see Added) now travel on the wire. Defaults reproduce
  the prior flat-chunks payload, but the wire contract is expanded/changed.

> **UI route churn (not a machine contract).** The single-surface rewrite deletes
> or repoints several htmx-driven HTML routes — the dead `/search` HTML route, the
> standalone `/stats` and `/peek` pages (absorbed into `/dashboard` and the Sessions
> view), the `/query?id=` detail branch, and the `/follow?poll=` fragment mode. A
> direct hit to `/peek` or `/stats` now 404s. **None of this touches `/api/v1`** —
> no documented JSON API contract changes here.

### Added

#### Swiss single-surface web UI

- **Single-rail Swiss shell** — the served UI is one left-rail shell over a single
  canonical content area (`#main`), replacing the old two-pane list/detail layout. A
  CSS-variable skin supports a light/dark tone toggle; every view mounts into the one
  swap target over a shared fragment. Deep links remap (`?id=` → Transcript, `?q=` →
  Search, `?follow=` → Sessions), and conversation rows across the Find list and
  search results open the detail view in place.
- **URL-as-state navigation** — the active view and its state are encoded in a
  canonical `/?view=<v>&<state>` address, so back/forward, refresh, shareable links,
  and deep links are all refresh-safe. Direct GETs to internal fragment routes
  (folio, dashboard, find, workspace, …) redirect to their canonical shell URL, and
  every rail item, model-brush, and workspace-sort pushes a deep-linkable URL.

#### The folio (conversation detail)

- **Transcript folio** — the conversation detail view is a new editorial "folio": a
  turn rail (scroll-spy "you are here"), a prose body (assistant *and* user turns
  render as markdown, section headings read as editorial landmarks), and a tool
  ledger. A ledger foot shows tokens, tool count, and cost — an em dash when usage is
  unpriced, never a fabricated `$0` — and hosts the curation affordances: interactive
  tag pills with an add input, plus Markdown/JSON export chips. The whole folio
  reflows responsively (container-query driven) into a footer band on narrow surfaces.
- **Folio reading/trace toggle** — the folio defaults to a *reading* view (prose,
  tool I/O folded into the ledger) and toggles into a *trace* view that inlines tool
  I/O in event sequence — each tool call a distinct collapsed card (info-blue call
  rail into the result, explicit ▸/▾ caret), thinking shown the same way. Expanding a
  tool result reveals the full output (no longer a 120-char preview), and trace mode
  drops the reading line-length cap so code and tool output use the full width while
  prose stays readable.

#### Sessions (live + sub-agent nesting)

- **Live Sessions view** — opens with a live zone of in-flight session cards
  (workspace, branch, model, adapter, exchange count, age) above a day-grouped
  timeline of ingested conversations; each day head shows session count, tokens,
  cost, and a 24-hour activity histogram. Clicking a card or row opens the folio;
  clicking a live card mounts the `/follow` tail. The standalone `/peek` page is gone
  — its scan is absorbed here. On a public bind with live endpoints disabled, the
  live zone and `/follow` are not served and the `?follow=` deep link degrades to the
  Sessions view.
- **Sub-agent nesting** — sub-agent conversations nest under their parent session as
  collapsed, indented rows behind a rotating-chevron disclosure, with a per-parent
  agent-count chip; expanding a caret reveals children without opening the parent.
  Day totals fold each parent's sub-agents back in so token and cost numbers stay
  complete. The Sessions list pages by top-level session (50 roots, each carrying its
  full sub-agent set); a sub-agent whose parent is off-page still renders, flagged as
  a sub row. Child rows label by agent type (plugin namespace stripped) and spawn
  time, read from the Claude Code `agent-<id>.meta.json` sidecar at ingest and
  persisted as a conversation attribute (reusing the polymorphic `attributes` table —
  no new schema).
- **Live follow renders as the folio** — the live-session follow view is the same
  folio rendered from a live source: rail, ledger, and token foot advance together as
  the session updates, the body stays pinned to the tail unless the reader scrolls
  up, and curation (tags/export) is suppressed pre-ingest since it needs the DB.

#### Stats reckoning dashboard

- **Swiss Stats "reckoning" dashboard (`/dashboard`)** — a live, owner-scoped usage
  dashboard. It opens with a standing block (period; three holdings —
  conversations, tokens, cost — plus the in/out token ratio), an activity trend with
  hour-of-day and day-of-week rhythm charts, an input-economy strip
  (uncached / cache-read / cache-write split with a cache-hit headline, shown only
  when the corpus reports cache tokens), two ranked accounts (model mix and workspace
  mix with token-sized bars and honest per-row cost), and a colophon (coverage,
  counts, active-days and longest-streak, last ingest). A Tokens|Cost measure toggle
  redraws every chart with no round-trip. Replaces the old two-pane `/stats` fragment.
- **Model brushing of the activity charts** — click a row in the dashboard's
  Model-mix account and the activity trend, hour/weekday rhythm, and input-economy
  strip re-scope to that model (charts relabel to "Activity · <model>", with a "show
  all" reset); the model account itself stays the global ranking and acts as the
  picker. Honoured via `/dashboard?model=<name>` only when the value names a real
  model in the corpus — unknown values fall back to the unscoped view rather than an
  empty scoped chart. The brush is a transient lens and does not push a deep-linkable
  URL.

#### Tags & Workspaces views

- **Tags view (`/view/tags`)** — a pinned zone plus a "Most used" headline over a
  namespace tree (flat tag names split on `:`). Each tag is ranked by its dominant
  per-grain count shown with its true unit (e.g. `312 conv` vs `198 calls`, never a
  grain-mix total). Clicking a tag drills into Find pre-filtered by that tag (a
  `?tag=` deep link that prefills and selects the tag in the filter strip), so a
  click lands on a refinable search surface rather than a dead table. Auto-applied
  tags (`shell:*` categories + `siftd:derivative`) are demoted from the headline so
  hand-curated tags surface, but remain in the namespace tree below.
- **Workspaces view (`/view/workspaces`)** — a two-tier nav with a Pinned zone and a
  Recent strip of cards over a sortable, client-filterable master list. Each row
  drills into a per-workspace detail dashboard (stat grid + usage rows + session
  drill) keyed on `?ws=`, showing the rollup's tokens and honest cost. Sort via
  `?sort=` ∈ {sessions, recent, tokens, cost}, with the magnitude bar following the
  active measure (dropped for recency). Viewing locally, a count-only caveat surfaces
  duplicate workspaces and points at `siftd migrate --merge-workspaces`.
- **Workspace detail gains cadence and subject tags** — the workspace-detail view
  (HTML and the matching REST/wire payload) now carries a Cadence strip (a per-day
  activity sparkline scoped to the workspace, peak day marked) and a "What it's
  about" set of tag chips counting the workspace's conversation-level tags, each
  drilling into Find scoped to that tag.
- **Per-owner pins for tags and workspaces** — pin/unpin tags (`POST /tag/pin`, new
  `tag_pins(owner, tag_id, pinned_at)` table) and workspaces (`POST /workspace/pin`,
  new `workspace_pins` table); pinned items lift into the head of their view. Both
  tables are created lazily on the next write-open (no `SCHEMA_VERSION` bump → no
  forced backup of an existing DB) and reads guard on the table's presence. Pins are
  owner-scoped and audited (`tag.pin`/`tag.unpin`); pinning requires the owner to
  actually participate (use the tag / be in the workspace), and a workspace pin
  cascades when the workspace is merged or removed.

#### Find — the search engine over facets, with state in the URL

- **Unified Find view (`/find`)** — the Swiss shell's Search slot is a working Find
  view composing a content-search box with workspace/model/tag/date facet filters
  over the conversation list. A meaningful content query routes through the real
  search engine (hybrid when the server has embeddings, graceful fts fallback
  otherwise) and renders ranked excerpt hits; an empty query keeps the facet-filtered
  recency list. The control strip gains a result-shape toggle
  (Excerpts/Thread/Conversations) and an embeddings-gated engine toggle
  (Auto/Hybrid/Semantic/Keyword), with the running engine named honestly in the
  header. An embed-path failure degrades to keyword search rather than 500-ing the
  pane, and raw FTS5 punctuation in the box is sanitized so it can't trigger a query
  error.
- **Find opens as a search surface** — Find now opens on a search prompt rather than
  a recency table. Typing runs the engine; a facet-only state (tag/workspace/model/
  date, no term) still browses the filtered list so the Tags and Workspaces
  drill-downs keep landing on conversations. The control strip is a two-state
  builder↔bar — a full builder when nothing is engaged, collapsing to a compact
  refinement bar with active facets shown as chips once a search or browse is showing
  — all CSS-only, so the search box keeps focus while typing.
- **Search state retained across navigation** — the full query (term, shape, engine,
  workspace, model, tag, owner, and date facets) rides the shell URL, so clicking a
  hit into the folio and pressing Back restores the prior results, and a refresh or
  shared link reproduces the whole query and rebuilds every facet control.
  Re-clicking the Search rail item resumes the last query rather than resetting;
  clearing the box drops the memory for a fresh start.
- **In-place context unfold in search results** — a chunks hit unfolds a window of
  surrounding exchanges inline in the results list, so you can judge relevance
  without leaving the page. The unfold widens through stepped rings (±2 → ±5 → ±10
  turns) before deferring to the full folio, and multiple hits unfold independently.
  The new `GET /find/context` route runs the same owner-scoped, anchored, windowed
  read the CLI's `--at-turn`/`--around` drive; the current ring rides the control
  URLs (no JS, CSP-clean), and a bad anchor or corrupt DB degrades to the collapsed
  trigger rather than a 500.
- **Search-hit unfold is a reading preview** — expanding a hit in place shows a
  reading preview (prose and thinking via the folio's reading emitter, each turn
  char-capped to a scannable excerpt) instead of inlining full tool I/O; the complete
  trace is one event-precise "open in folio" jump away.
- **Event-precise "open in folio" jump from search** — a search hit's folio jump
  opens *trace* mode anchored at the matched event, so you land on the match instead
  of the folio top. The matched event's ULID rides `mode=trace&event=<id>` through
  the folio URL (chunks hit, thread-view tier-2 hit, and the unfold's last ring all
  jump event-precisely), the page scrolls to and highlights the landed event, and a
  hard reload re-lands. The entry-point rule: search opens trace, the list opens
  reading; a reading-mode URL ignores any stale `event=`.
- **Tool and tool-tag filters in search** — filter search by tool use. The CLI gains
  `-t/--tool NAME` (canonical tool name, e.g. `shell.execute`) and `--tool-tag NAME`
  (tool-call tag, e.g. `shell:vcs`) on `siftd search`; `/api/v1/search` accepts
  matching `tool`/`tool_tag` query params; and Find adds a usage-ordered tool
  dropdown that round-trips through the canonical URL. Matching is conversation-level
  (any tool call in the conversation matches), consistent with `query --tool`.
- **Sort, threshold, and full-text controls in Find** — Find surfaces three existing
  search controls: a score/time sort toggle (time clamps to score for
  thread/conversation shapes where it doesn't apply), a minimum-score threshold input
  (with a distinct "No matches above the score threshold" empty state), and a
  full-text checkbox that shows untruncated excerpts. All three ride the canonical
  URL, with only non-default values appearing.
- **Stats and Workspaces resume last selection** — re-clicking the Stats or
  Workspaces rail item resumes the last model-brush or sort (like re-clicking Search
  resumes the last query) instead of resetting to the bare view, while keeping
  back/forward history intact.

#### Output & wire

- **`cost` field on conversation detail JSON** — `query <id>` JSON now carries a
  `cost` field (from the rollup's canonical conversation stats at depth ≥ 3), and it
  round-trips through the delegated wire form. `null` means "no priced usage" and is
  preserved distinctly from a real `0.0` — additive (new key).

#### Terminal UI — the painted refresh

- **"Warm obsidian" terminal theme** — the CLI's output is rebuilt on
  [painted](https://pypi.org/project/painted/) with a bespoke warm-dark theme: a
  single **amber metric thread** that gilds every count and figure, a cream body
  foreground (`palette.text`) for readable prose, and weight rather than colour as
  the structural accent. Section headings, report surfaces, and entity listings all
  draw from one shared vocabulary of atoms, so styling is consistent command to
  command instead of hand-rolled per surface.
- **Typographic transcript feed** — conversation bodies render as a typographic feed
  rather than boxed panels: the thinking/tool border boxes are gone, replaced by
  blank-line block breaks and a thinking label that pops, and **assistant and user
  turns render as markdown**. A **grain gutter** runs a per-line rail down the left
  encoding each line's kind (prose, code, thinking, tool I/O), and tool I/O wraps to
  the content width so it aligns under the rail.
- **Search surface on a relevance rail** — terminal `siftd search` is rebuilt on
  painted spans with a relevance rank rail; the top hits are expanded to show their
  full, word-wrapped, untruncated snippet while the tail collapses to one line each,
  so the strongest matches are readable at a glance and the rest stay scannable.
- **Live progress across long operations** — `siftd ingest` shows live progress bars,
  `sync push`/`pull` show live transfer progress, and `doctor --fix` runs on the same
  shared live region. All ride one generic `ProgressEvent`/`ProgressConsumer`
  contract so every long operation reports progress the same way.
- **Brand wordmark + DC-match help redesign** — `siftd --version` and the root
  `--help` now carry a brand wordmark, and the whole help tree is rebuilt on one
  unified grammar (root/branch/leaf) with a terse root, regrouped flags, and coloured
  examples — rendered at the terminal's true colour depth.
- **Config-swappable terminal theme** — `ui.theme` (`siftd` | `nord`) selects the
  terminal colour theme; defaults to `siftd`. Terminal only — it does not affect the
  web UI. Validated by `siftd doctor`.

### Changed

- **`siftd search --sort=time` now orders newest-first** — leads with the most recent
  matching hit instead of the oldest, matching the intuitive reading of a time sort
  and the browse list's default recency order; flows through CLI, REST, and the Find
  UI. A script depending on the old oldest-first ordering will see different ordering
  (flags/params unchanged).
- **`GroupUsage.cost` is now `float | None`** — model-mix and workspace-mix rows with
  no priced usage sum to `None` rather than a fabricated `$0`, carrying the same
  NULL-means-unpriced invariant `ConversationDetail.cost` already uses. The dashboard
  renders an em dash for unpriced rows, and coverage percentages floor instead of
  rounding up (e.g. `99.8%`, never a false `100%`). This is a Python API type change
  only — it does not appear on any JSON/REST wire.
- **`TagInfo` gains `pinned` and `auto`** — the `TagInfo` dataclass (and thus the
  `list_tags` JSON via `dataclasses.asdict`) gains two booleans: `pinned` (pinned for
  the effective owner) and `auto` (name is in the closed auto-applied vocabulary —
  `shell:*` categories plus `siftd:derivative`). Additive, defaults `False`.
- **Dashboard reads through the stats cache** — repeat dashboard loads no longer
  recompute the full-table stats sweep (~10s cold). The materialized stats cache
  gains an owner dimension (so a tenant never reads cross-tenant totals) and an
  opt-in DB-mtime freshness check (so a push-ingest or tag write invalidates it).
  Repeat loads drop from ~10s to ~0.02s.
- **`painted` dependency bumped to 0.4.0** — the terminal-UI refresh rides
  [painted](https://pypi.org/project/painted/) 0.4.0 (from 0.1.7), adopted from PyPI,
  for the theming, table width-budgeting, and live-progress substrate above.

### Fixed

- **`siftd serve` no longer freezes the event loop under slow reads** — every serve
  read handler (JSON API and HTML UI) ran as an async coroutine doing synchronous
  SQLite/filesystem work, so a single slow read stalled the whole server and
  serialized concurrent requests. Read handlers now run in the threadpool
  (`sync_to_thread`); write handlers that read the request body stay async.
- **`/meta` model dropdown is fast** — the `/meta` endpoint called `get_stats()` —
  full-table counts, token coverage, top tools — just to populate a model filter
  dropdown (~17s cold on a large DB). It now uses a cheap `DISTINCT` projection (new
  `api.stats.list_models()`), dropping cold `/meta` from 17.3s to 0.5s.
- **Tool results stored under the `content` key now surface** — tool results saved as
  `{"content": …}` (Claude Code and others) are now rendered. Previously shell/file-
  read tools showed an empty result and the generic path truncated to a 200-char JSON
  fallback regardless of the chars budget; file reads now surface their content,
  non-string results render as clean JSON, and the last-resort fallback respects the
  chars limit (full in the trace, capped in compact views). Shared with the CLI
  presenters, so terminal `--tools` gains the same result visibility.
- **Unpriced workspace cost shows an em dash, not a fabricated `$0.00`** — a workspace
  with no priced usage renders its cost headline as `—` instead of a
  self-contradicting `$0.00`. Cost is `None` end to end (the model-mix SQL no longer
  COALESCEs cost to 0), so the headline can never disagree with the per-model rows
  beneath it.
- **Cache-bust served static assets** — siftd's own static assets (`siftd.css`,
  `enhance.js`, `auth.js`) are versioned with a `?v=<mtime>` query so the browser
  refetches when their bytes change — a CSS/JS edit lands on the next page load
  instead of a stale cache surviving. Vendored, version-pinned assets are left alone.
- **Stale per-owner stats cache under concurrent writes** — served-dashboard stats no
  longer certify pre-write totals when a push lands mid-recompute. The cache
  freshness stamp is captured *before* the multi-second stats sweep rather than at
  write time, so a concurrent write leaves the stamp stale and the next read
  recomputes instead of returning out-of-date per-owner figures.
- **Cross-tenant tag-pin existence oracle** — pinning a tag now requires that the
  owner actually uses it. Previously a tenant could pin another tenant's tag and
  surface its name; the pin branch is now gated on owner participation (mirroring
  workspace pins), while unpin stays unconditional and the unscoped see-everything
  view keeps its existence-only guard.
- **Unescaped id in the export not-found fragment** — the served `/export` not-found
  fragment now HTML-escapes the reflected id, closing the lone escaping holdout among
  the error fragments.
- **Help renders at the terminal's true colour depth** — help previously rendered
  through a buffer that reported no TTY, downsampling the warm palette to 16-colour
  (and emitting malformed bare escapes that showed as terminal-default grey). Help
  now defers to the terminal's real colour depth (`COLORTERM`/`TERM`), so it renders
  in truecolour like every other surface.
- **Non-ASCII content degrades instead of crashing** — content with non-ASCII bytes
  on a strict-ASCII output stream now degrades gracefully rather than raising
  `UnicodeEncodeError` mid-render.
- **`NO_COLOR` honoured across all CLI output** — the `NO_COLOR` environment variable
  is now respected at every `print_block` site, not just the table paths.
- **`doctor --fix` survives a zero-column terminal** — the progress region floors its
  width so a 0-column pty no longer crashes the fix run.

### Internal

- **Search post-processing recipe homed in `api.search.process_search_view`** — the
  recipe was lifted out of the two CLI handlers into one place operating on
  `SearchChunk` end-to-end with a single render-boundary conversion; both CLI paths
  collapse onto it and the cli-private wrappers are deleted. No user-visible behavior
  change on its own — the parity it enables surfaces through the serve and wire
  changes above. This is the substrate the unified Find view and the
  recipe-on-the-wire REST route consume.
- **Golden adapter fixtures collapse to non-default fields** — adapter test golden
  fixtures now encode only the fields a case exercises, omitting any field equal to
  its dataclass default, so adding a new optional domain field no longer ripples a
  mechanical edit across all 11 fixtures.

## [0.9.1] - 2026-06-11

> Security-hardening release for `siftd serve`: findings F2–F9 from a serve-layer
> security audit, plus a three-tier CSP regression suite (static fitness function,
> HTML↔CSP cross-check, real-browser smoke via `./dev browser-smoke`) guarding the
> new policy.

### Added

- **Push response reports the server-stamped ownership count** — `siftd sync push` now shows how many conversations the server attributed to your authenticated identity ("Pushed 2 conversations (1.0 KB, 2 owned)"). Absent when the server doesn't report it (older server, local/SSH transport) rather than fabricating 0.

### Security

- **Security headers + CSP on all serve responses; CDN assets vendored** — htmx and Prism are now served from `/static/vendor/` instead of unpkg.com (no external script origin; closes the supply-chain vector). Every response carries a `Content-Security-Policy` (notably `connect-src 'self'`, which prevents a bearer token in `sessionStorage` from being exfiltrated off-origin even after a hypothetical injection) plus `X-Content-Type-Options`, `X-Frame-Options: DENY`, and `Referrer-Policy`. When an OIDC issuer is configured, `connect-src` widens to that origin so the browser PKCE login flow (which fetches discovery + token endpoints) keeps working. HSTS remains the reverse proxy's job. (finding F3)
- **Per-client rate limiting on serve** — default 600 requests/minute, keyed by the real client IP (trusted-proxy `X-Forwarded-For` aware, so it's effective behind Caddy), exempting `/static` and the health check. Configurable via `serve.rate_limit_per_minute` (0 disables). (finding F4)
- **Audit log for tag mutations** — apply/remove/rename/delete and live-session tag queueing now write an `audit_log` row (actor, action, target, source IP, timestamp), mirroring `push_log`, so destructive changes on the shared DB are attributable. (finding F6)
- **`/peek` and `/follow` are gated off on public binds** — these endpoints read the server host's session files and bypass owner scoping. They are no longer registered when bound to a non-loopback address unless `serve.allow_live_endpoints` is explicitly enabled. (finding F7)
- **Error responses no longer leak internal filesystem paths** — not-found errors on the dispatch, event-detail, and session-queue routes return generic messages; the detail (including absolute DB paths) is logged server-side only. (finding F8a)
- **`push_log` records the real client IP** — `X-Forwarded-For` is honored only from a configured `serve.trusted_proxies` allowlist (otherwise the connection peer is used), so provenance behind a reverse proxy is accurate and a client cannot spoof its recorded IP. (finding F8b)
- **`siftd serve` creates the team DB at startup** — a server-owned empty schema DB is created when missing, so the first push *merges* into it rather than adopting an uploaded SQLite file wholesale. (finding F9)
- **`siftd serve` fails closed on a public bind without auth** — binding a non-loopback address with authentication disabled is now refused (exit 2) unless the new `--unsafe-public-no-auth` flag is passed. Previously such a server started silently, exposing the entire corpus for read **and** write. The production container (`--host 0.0.0.0`) now requires a mounted `[serve.auth]` config (or the explicit override). (finding F2)

## [0.9.0] - 2026-06-09

> Two headlines. **The numbers are real now**: cache tokens are counted and billed
> (schema v9→v11 — usage rollup substrate, cache-inclusive token facts, pricing as a
> version-controlled reference), correcting reported tokens and cost by an order of
> magnitude. And **a new operating mode**: networked `siftd serve` with transparent
> thin-client delegation, plus the auth subsystem (device-code + browser PKCE login,
> JWKS validation) and a read-surface overhaul. Several output-format and flag
> changes are **agent/script-facing**; see Breaking Changes first.

### Breaking Changes (migration)

- **Reported token and cost numbers change — by a lot** — Anthropic cache tokens
  (previously stranded in schemaless attributes) are now part of the usage fact and
  are billed: `input_tokens` on aggregates means the **true total** (incl. cache
  read/creation), and cost is computed from four components with cache rates derived
  from input rates (Anthropic ×0.1 read / ×1.25 creation; override-only in pricing).
  On a real corpus this moved totals from 433M → 46.8B tokens. Scripts comparing
  against pre-0.9 numbers must re-baseline.
- **Schema v8 → v11 (three migrations, run automatically on first open)** — v9 usage
  rollup, v10 cache-inclusive usage facts, v11 pricing-as-reference. Each migration
  takes the existing single-transaction + pre-migration-backup path; a v8-or-older
  siftd refuses to open a v9+ database. Large DBs: expect a one-time backup copy.
- **Client send-token namespace moved to `[auth]`** — `serve.auth.delegation_token`
  is **removed**; the static bearer the CLI *sends* now lives in `[auth].token`
  (`env:`/`file:`/literal). `serve.auth.static_token` remains *server* validation
  config only. Shared-secret deploy: set `[auth].token` (client) and
  `serve.auth.static_token` (server) to the same value.
- **List `turns` column shows prompt count** — the combined `Xp/Yr` (e.g. `15p/34r`)
  is replaced by a single integer = prompt count (with `-v`, a separate `responses`
  column gives the response count). Applies to terminal/markdown/HTML. Scripts
  parsing `Xp/Yr` must update. `--json` fields unchanged.
- **Turn headers use role labels** — `[prompt]`/`[response]` → `[user]`/`[assistant]`
  in all human-facing output; search chunk badges go role-first
  (`prompt → USER`, etc.). Machine-readable `kind`/`chunk_type` in `--json` unchanged.
- **Conversation ID display widened 8 → 12 chars** — zero collisions at 12 across an
  11,771-conversation DB (623 colliding prefixes at 8). Display-only; update grep
  patterns. (Supersedes an earlier in-cycle move to 8.)
- **Ambiguous prefix now exits 2, lists matches** — `siftd query`/`tag`/`id` no
  longer silently first-match an ambiguous prefix (was non-deterministic). Use a
  12-char or full ID.
- **`siftd search` flags refactored into orthogonal axes** — `--first`/`--by-time`/
  `--thread`/`--conversations` **removed** → `--select={all,first}` +
  `--sort={score,time}` + `--mode={chunks,thread,conversations}`.
- **`siftd search --context N` removed** — migrate to `--around "phrase" --turns -2:+2`.
- **`siftd query <id> --exchanges N` requires an anchor flag** — implicit tail
  behavior removed; use `--from-end --exchanges N`.
- **`siftd search --turns` without `--around` exits 2** (was 1) — consistent with
  other argparse-layer rejections.
- **`Fidelity` is required on `list_conversations`/`get_conversation`/`export_*`** —
  CLI/serve pass the cross-stage `Fidelity` contract into the fetch layer; the
  `include_thinking`/`include_tool_content` booleans are gone from those signatures.
  HTTP wire on `/api/v1/conversations/{id}` is unchanged (still accepts the booleans,
  translated internally).
- **`siftd tools` command + `/api/v1/tools*` routes removed** — express via the events
  substrate: `siftd tag list --on tool_call --prefix shell:` (add `--by-workspace`
  for the breakdown). `[tools]` config keys are now ignored; lost: percentage display.

### Added

- **Usage rollup substrate (`usage_by_conv_model`) — schema v8 → v9** — a new
  ingest-time fact table at grain `(conversation_id, model_id, provider_id)` that
  backs per-model/-provider/-workspace/-harness/global token+cost aggregates with a
  single cost definition. `conversation_stats` is now *re-derived from* this rollup
  (cost summed to conversation grain, rounded once); values are unchanged — verified
  byte-identical across the full corpus (0 cost/token/count diffs over 12,760
  conversations). The v9 migration backfills the table and re-derives stats inside
  the existing single-transaction + automatic pre-migration-backup path. Foundation
  for the rest of this release's cost-coherence work (S2/S3 read re-point, the
  `stats --by model` fan-out fix, v10 cache truth, v11 pricing reference — below).
- **Cache-inclusive usage facts — schema v9 → v10** — cache read/creation tokens move
  from schemaless attributes into the usage rollup as first-class columns; cost
  becomes a four-component sum (input, output, cache-read, cache-creation). Verified
  to the cent against a live corpus. Surfaced and closed the second gap: previously
  ~43% of input tokens were silently unpriced.
- **Pricing as a sourced reference — schema v10 → v11** — the pricing table is now a
  projection of a version-controlled reference (`siftd/data/pricing.toml` + user
  override at `~/.config/siftd/pricing.toml`), UPSERTed on every open: never
  born-frozen, never contaminated by sync (`_map_pricing` deleted). Rows carry
  provenance (`source` citation + `as_of` date). `siftd backfill --pricing`
  reprojects the reference and rebuilds the cost rollup after editing the TOML;
  a new doctor check audits provenance.
- **Browser PKCE login — the browser is a client** — `siftd serve` gains an
  auth-code+PKCE flow (`auth.js` SPA) so the web UI authenticates against the same
  OIDC issuer as the CLI; serve still only ever *validates* a bearer.
- **`GET /api/v1/workspaces/{id}` + workspace-detail Operation** — workspace detail
  through the dispatch pipeline (ULID-first identity), with fidelity-gated tag
  activity enrichment and a general `?visible=` field-selection mechanism.
- **`siftd auth login` — client-side OAuth device-code acquisition (RFC 8628)** —
  interactive token acquisition so remote-human clients stop hand-pasting bearers.
  Runs the device-authorization flow against `[auth].issuer` (browser URL + code),
  stores access+refresh `0600` under `~/.local/state/siftd/credentials/` keyed by
  issuer, and presents it automatically on delegated reads. Refreshes **proactively**
  near expiry and **reactively** on `401` (gated on a stored credential existing).
  Adds `siftd auth status`/`logout` and the `[auth]` namespace (`issuer`, `client_id`,
  `scope`, optional endpoint overrides) — distinct from `serve.auth.*`. serve still
  only ever *validates* a bearer.
- **`siftd serve` thin-client delegation** — `query`, `query <id>` (JSON + non-JSON),
  `search`, `tag`, `stats`, `workspaces`, `export` delegate transparently to a
  configured remote `serve.url`; local execute remains the fallback.
- **Production container image + homelab build pipeline** — multi-stage non-root root
  `Dockerfile` (~315 MB) with `/api/v1/health` + `ca-certificates`; GitLab CI builds
  and pushes `registry.gruel.network/gruel/siftd:{latest,<sha>}` (gruel.network mirror).
- **Windowed HTTP push for large-history seeds** — `siftd db push` splits DBs over the
  server's advertised `max_body_size` into resumable time-ordered date-window POSTs,
  with 413-triggered recursive bisection. Steady-state incremental pushes are unaffected.
- **`serve.request_max_body_size` knob** (default `500MB`, SI suffixes) — fixes
  Litestar's 10 MB cap rejecting realistic `siftd db push` payloads; matches the
  reference Caddyfile `max_size`.
- **`/api/v1/export?format=md|json`** — format-aware path returning a rendered
  `ExportArtifact`; legacy (no `format`) returns `{"conversations": […]}` unchanged.
- **`/api/v1/conversations/{id}` anchor + window query params** (`anchor`,
  `anchor_value`, `window_start`, `window_end`) — mirrors the CLI axes so delegated
  `siftd query <id> --json --at-turn N` anchors.
- **Anchor + window navigation axes on `siftd query <id>` and `siftd search`** —
  anchors (`--from-start`/`--from-end`/`--at-turn N`/`--around PHRASE`) compose with
  windows (`--exchanges N`/`--turns A:B`, spaced or `=` form). `--around` uses the FTS5
  index; multi-match and no-match emit disambiguation/locate hints.
- **`turn_index` + `event_id` in all search results** (FTS5/semantic/hybrid) — human
  output appends `→ siftd query <id> --at-turn N`; `--json` includes both.
- **Caveats substrate** — `Finding.channel` (`text`/`json`/`both`), `Finding.field`,
  `Literal` `severity` (`info`/`warning`/`error`/`hint`), per-collection caps, and
  `--no-hints` on `doctor` + `query`. Producers added: `ingest-status`, `fts-stale`,
  `embeddings-stale`, `workspace-identity`, `active-sessions`, `pending-tags`,
  `fresh-corpus`, `adapter-health` warnings, `search-mode-degraded`,
  `search-tagging-tip`, `query-empty-tip`.
- **`siftd id <ULID>`** — classifies a ULID as conversation or event with a one-line
  summary + view hint (`--json`; exits 0/1/2).
- **`siftd tag apply`/`remove` subcommands** + **`siftd tag list --by-workspace`** —
  explicit verb forms for agents/scripts; per-workspace event-tag counts.
- **`siftd db restore`/`receive --dry-run`** — preview destructive ops (paths, schema
  direction-of-change, per-table row counts) without writing.
- **`siftd query --stats` corpus-aware** — view count/tokens against corpus totals.
- **`siftd peek --follow --timeout SECONDS`**, **`--latest` alias for `--last`**
  (`tag`/`export`).
- **`docs/guides/delegation-contract.md`** — names the local/wire-forms pattern and the
  8-rule contract every delegated path follows; pinned by `tests/test_op_route_parity.py`.

### Changed

- **Usage read-sites re-pointed to the rollup (S2/S3)** — per-model/-workspace/
  -harness aggregates, slice/merge derived-tier rebuilds, and token coverage all
  read from `usage_by_conv_model` instead of recomputing from events; the 21%
  mispricing harness-source cost fallback is retired (slices carry fallback pricing
  forward instead). — the four scattered op↔wire workarounds
  (`_LOCAL_FN_EXCLUDE`, `_WIRE_EXCLUDE`, `_SERVE_PARAM_MAP`, `_expand_for_wire`) are
  replaced by a per-op `OpSpec` registry plus `Operation.to_local()`/`to_wire()`/
  `to_wire_body()`; `to_wire()` raises `MissingOpSpec` for unregistered paths instead
  of silently sending every key. Internal; no CLI/wire-visible change.
- **Caveat output capped per collection** — max 1 hint, max 3 infos (excess → one
  "+N more" notice); warnings/errors uncapped.
- **`siftd ingest` quiet when stdout isn't a TTY** — only the totals line prints when
  piped (one-time stderr hint; `-v`/`-q` honored).
- **`siftd db --help` epilog grouped** into Inspection / Maintenance / Sync / Sync remotes.

### Fixed

- **`stats --by model` cost fan-out (up to 290×)** — per-model cost was recomputed
  through a join that fanned out across providers/rows, inflating a real corpus's
  per-model totals to ~$913k against a ~$3.1k whole-corpus figure. Aggregates now
  read the rollup at its native grain; one cost definition everywhere.
- **Model identity at canonical grain across the stats surface** — the model ledger
  groups/displays by canonical `name` (raw spellings like `claude-haiku-4.5` /
  `claude-haiku-4-5` no longer split rows), model counts count canonical names
  (`fetch_table_count`), and the model-filter dropdown lists canonical names.
- **`fmt_tokens` rolls over through M/B** — 44.4B tokens rendered as `44443867k`;
  the formatter only had one rung. Now climbs k → M → B. — a non-empty table naming no
  recognized mode now fails loudly at boot (with a stale-`delegation_token` hint)
  instead of fail-closing `401` opaquely on every request.
- **OIDC: PyJWKSet → PEM key extraction** — `_validate_oidc` passed the whole
  `PyJWKSet` to `jwt.decode` (accepts one key) → `TypeError` → HTTP 500 on every
  OIDC-protected request. Now extracts the matching `PyJWK` by `kid`. (OIDC had never
  worked end-to-end; caught by the docker-compose smoke harness.)
- **OIDC: JWKS refetched on unknown `kid`** — a signing-key rotation previously rejected
  every fresh token for up to the 3600s TTL (failing closed). Now force-refetches once
  (rate-limited to 60s) before rejecting, mirroring PyJWT's `PyJWKClient`.
- **OIDC: `iss` mismatch logged at WARNING** — a client/server issuer misconfig
  surfaced only as a bare `401` + refresh loop; now logged loudly (both values non-secret).
- **Reactive refresh no longer re-sends a stale "winner" token** — `_locked_refresh`
  returned a differing stored token without a freshness check; now falls through to a
  real refresh unless that token is fresh.
- **`siftd search --fts` delegates when configured** — two silent short-circuits
  skipped delegation for the FTS path; `/api/v1/search` now accepts `mode`
  (`semantic|hybrid|fts`). (P7)
- **FTS5 index rebuilt after first-push** — `receive_database` `_create_from_source`
  returned without rebuilding `content_fts` even when `rebuild_fts=True`, so post-seed
  FTS queries were empty. Now rebuilds before returning.
- **Delegated reads reach the homelab** — when `serve.url` is explicitly set, the
  local-vs-server DB-path SHA256 check is skipped (it was structurally incompatible with
  the documented topology; every delegated call silently fell back to local). Loopback
  still enforces the check.
- **Delegated responses tolerate schema drift** — deserializers return `None` (→ local
  fallback) on mismatch rather than raising.
- **Caveats threaded across the delegation wire** — the typed deserializers extracted only
  result rows and dropped the envelope's `caveats` key, so every delegated read (`query`
  list + both `search` paths) emitted `caveats: []` — a thin client silently claimed a
  healthy index. The server's caveats (stale embeddings, degraded mode, truncation) are now
  preserved and reconstructed client-side (defensive: unknown keys dropped for newer-server
  tolerance).
- **Push errors return structured 4xx, not opaque 500** — the HTTP push route let
  `receive_database` exceptions fall through to a generic 500, and the client wrapped any
  non-413 as `"Push failed: HTTP 500"`, hiding the cause (a version-mismatched member failed
  every push unactionably). The route now maps a non-SQLite body → `400 invalid_source`,
  preflight failure → `422 preflight_failed`, schema-version mismatch → `409 schema_mismatch`,
  locked DB → `503 database_locked` (mirroring the SSH receive envelope); the client surfaces
  the `error_type` with an actionable message.
- **Orphan `content_blobs` GC'd on merge** — blobs were copied wholesale but their referencing
  `event_tool_call` rows were filtered to landed events, so a blob whose referrers were skipped
  (dedup'd duplicate, non-landing event) lingered at `ref_count=0` forever — one leaked blob per
  unreferenced source blob on every overlapping re-push (unbounded growth). The merge now deletes
  the src-introduced blobs the ref_count recompute proves unreferenced.
- **Server 4xx surfaced on delegated reads** — a 4xx now raises `ServeRequest4xx` and
  prints a named-server error + exit 1 (was swallowed → misleading local fallback).
  Unreachable/5xx still fall back. (P6)
- **Anchor errors on delegated `query <id>` return 400, not 500** — `AnchorOutOfRange`/
  `AnchorNotFound`/`AnchorPhraseInvalid` now caught explicitly.
- **Wire params no longer leak** — `wire_query` expands `Fidelity`, drops `None`, strips
  local-only keys (`db_path`/`embed_db`/`around`).
- **Silent prefix-collision resolvers converged** — three resolvers
  (`fetch_conversation_by_id_or_prefix`, `resolve_entity_id`, `get_conversation_metadata`)
  unified into `resolve_entity_id`, raising typed `AmbiguousPrefix` (exit 2 / HTTP 400);
  serve UI ambiguous paths no longer 500.
- **`--turns -2:+2` (spaced) parses** like the `=` form; **`embeddings-available` doctor
  finding is `warning`**; **`siftd query -s` hints toward `siftd search`**; **search
  caveats now render** across terminal/markdown/HTML/JSON.
- **CI green recovery** — three lanes red 9 days: `ingest-stale`+`ingest-errors` both
  fire on stale+error DBs; py312 snapshots regenerated; `TestSortAxisValidation` catches
  `SystemExit(2)`.

### Security

- **Workspace-detail cross-tenant read IDOR closed** — `GET /api/v1/workspaces/{id}`
  returned detail for workspaces the requester didn't own, and a path-substring
  filter could bleed sibling-workspace rows into scoped aggregates; both now
  owner-scoped end-to-end (with an IDOR regression test).
- **`/stats` usage breakdowns owner-scoped** — per-model/-workspace breakdowns on a
  multi-tenant serve previously aggregated the whole corpus regardless of requester.
- **Write-path multi-tenant IDOR closed — the merge is now owner-partitioned** — the
  merge threaded the pushing identity in only to *stamp* new conversations, never to
  *gate* writes/replaces. On a multi-tenant `serve` deployment one authenticated client
  could (a) push a slice whose child rows (events, content, tool calls, attributes, tags)
  reference another tenant's conversation/event IDs and have them grafted in, and (b) — the
  most reachable variant, needing no knowledge of the victim's server IDs — push a
  conversation sharing another tenant's natural key `(harness_id, external_id)` with a newer
  ULID, **deleting that tenant's conversation and all its children** and re-stamping the
  victim as owner of the attacker's content. `merge_database`/`receive_database` now take the
  pushing `user_id`; every cross-DB INSERT/DELETE and the stale-conversation replacement are
  confined to conversations the pusher owns or that are unowned. Single-tenant / SSH merges
  (no `user_id`) are unchanged.
- **Staged (deferred) merges carry the pusher identity** — `sync_inbox` gains `user_id`/
  `push_id`; `stage_payload` persists them and `process_inbox` replays them into the
  owner-partitioned merge, so a staged push is owner-scoped exactly like the synchronous
  path (closing the seam before the inbox is wired to multi-tenant HTTP push).
- **Caveat producers suppressed on owner-scoped requests** — producers query the whole DB
  (corpus counts, ingest/index health, pending-tag totals) with no owner predicate; on a
  multi-tenant request they would report whole-server aggregates to a scoped tenant. They are
  now skipped whenever an owner scope is active (single-tenant/local keeps them).
- **OIDC: `iss` required and validated** — `_validate_oidc` previously checked `aud` +
  signature but not `iss`; now `iss`/`aud`/`exp` are all required and `iss` is compared
  to the configured issuer.
- **OIDC: `identity_claim` must be present and non-empty** — was falling back to a
  synthetic `"unknown"` `sub`, collapsing distinct subjects under one owner.
- **OIDC: discovered `jwks_uri` must share the issuer origin** (scheme+host+port) — a
  compromised discovery endpoint can no longer redirect to an attacker-controlled JWKS.
- **Delegation: a rejected `env`/static token is never swapped for a device-code
  credential** — tokens are source-tagged; reactive refresh is gated to the
  `device-code` source, preventing silent mis-attribution of server-side writes.
- **Introspection cache bounded + `sha256(token)`-keyed** — no plaintext-token keys,
  1024-entry cap (expired-first eviction); was a soft memory-DoS.

### Removed

- **`siftd search --context N`** (no alias — `--around` anchors differently; would
  silently shift meaning). Migrate to `--around "phrase" --turns -2:+2`.
- **`siftd tools` command + `siftd.api.tools` + `/api/v1/tools*` + `?tools=` UI knob** —
  superseded by the events substrate (`siftd tag list --on tool_call`).
- **FTS5-fallback inline prints + empty-result inline tips** — now routed through the
  caveats channel (`search-mode-degraded`, `query-empty-tip`; suppressible via
  `--no-hints`, excluded from `--json`).
- **`ambiguous-id` caveat producer** — superseded by the `AmbiguousPrefix` exception
  (prevents silent first-match at the source rather than patching post-render).

### Docs

- **Authentik→siftd auth-migration runbook re-grounded to current code**
  (`docs/ops/authentik-auth-migration-runbook.md`) — verified claim-by-claim against
  current `serve/auth.py` + Authentik 2025.x: removed the false trailing-slash `iss`
  "code-level blocker" (handled by the normalized compare at `serve/auth.py:275`), added
  a Phase-0 deployed-build gate, clarified the `iss`-lenient/`aud`-strict asymmetry,
  corrected drifted `auth.py:NNN` → `serve/auth.py:NNN` refs, and fixed the
  Encryption-Key field-move attribution (2025.10.1, not 2025.12).

## [0.8.1] - 2026-05-07

### Added

- **Event IDs in JSON (default-on)** — `Turn` gains `prompt_id` / `response_ids[]` / `tool_call_ids[]`; `NarrativeBlock` gains `event_id`; `ToolCallDetail` gains `tool_call_id`. Search chunks emit `chunk_id` / `source_ids` by default. Enables agents to round-trip event IDs through query → tag / detail surfaces without secondary lookups.
- **Late-bound `--last-*` pending tags** — `siftd tag --session <id> --last-{prompt,response,exchange,tool-call}` queues tag intent against a live session; resolution to the most-recent matching event happens on next `siftd ingest`. Schema-additive `last_marker` column on `pending_tags` (in-place rebuild on first open). New `POST /api/v1/sessions/{id}/tags` HTTP route.
- **Event detail surface** — `EventDetail` dataclass + `get_event(id, *, include_neighbors=False)` API. `siftd query <event_id>` smart-routes via prefix-match across event kinds. `GET /api/v1/events/{id}` HTTP route.
- **Tag-prefix conventions table** — `[tag_prefixes]` config section with built-in defaults (`decision:`, `research:`, `useful:`, `rationale:`, `genesis:`). `siftd config tag-prefixes [--json]` dumps the resolved table. Groundwork for future skill/hook consumers; no runtime consumer in this release.

### Changed

- **CI matrix expanded to Python 3.12, 3.13, and 3.14.** Previously only 3.12 was tested; argparse formatting differences in later versions had silently slipped past CI. Help-snapshot tests now run on every matrix version via per-version snapshot directories at `tests/snapshots/__snapshots__/py{ver}/`. Snapshot policy: `docs/guides/snapshot-policy.md`.

### Deprecated

- **`--debug-ids` flag and `debug_ids` kwarg** — Now a hidden no-op (chunk_id and source_ids ship by default in JSON). Accepted on `siftd search`, `to_render_dict()`, `render_search()`, and the serve render context through v0.9.x; removed in v0.10.0.

### Removed

- **`siftd tool-search` command and its denormalized projection table.** The `tool_search` table and `tool_search_fts` virtual table are dropped in schema migration v8 (validated against a 2.7 GB production DB: 200k+ tool_search rows reclaimed in <10 s, ~11% DB size recoverable via `siftd db vacuum`). Capability lost: bare-text FTS over a 280-char tool-call result snippet. Tool-call queries now go through the events substrate via `siftd query --tool` and structured tag filters. The `/api/v1/tool-search` HTTP route, the serve HTML `/tools` page, and the `tools.limit` config key are also removed.
- **`siftd tags` command.** Deprecated command removed. Use `siftd tag list`, `siftd tag rename`, or `siftd tag delete` instead.

### Fixed

- **Polymorphic tag filter** — `siftd query -l <tag>`, `--all-tags`, and `--no-tag` now match tags applied at any conversation-bearing target_kind (`conversation`, `prompt`, `response`, `tool_call`, `exchange`). Previously, after the polymorphic storage refactor (v0.8.0), only conversation-scoped tags were visible to these filters; tags applied at event granularity were silently invisible. New `--on KIND` flag opts into legacy single-kind filtering.
- **Test suite stability under restricted environments** — 5 git tests now pass under sandboxes without git user config (subprocess passes `-c user.email/-c user.name`); 5 chmod-based readonly tests skip cleanly under root via `@pytest.mark.skipif(os.getuid() == 0)`.
- **Stale `siftd tags` references** purged from `README.md`, `docs/concepts/tags.md`, and `plugin/skills/siftd/reference/tags.md`. Stale docstring on `build_tags_parser` and stray blank-line residue in `cli/__init__.py` and `output/json_fmt.py` after the tool_search and tags-shim removals.

## [0.8.0] - 2026-05-06

> **Upgrade note.** This release ships a one-way schema migration (v3 → v7).
> Run any siftd command after upgrading to trigger it; a pre-migration backup
> is written next to your database as `<name>.bak.YYYYMMDD.db` automatically.
> On a 3 GB database the migration takes ~40 seconds and emits per-phase
> progress to stderr. Read-only commands (`query`, `doctor`, `peek`, `search`)
> auto-upgrade transparently if the file is writable, or raise a clear
> `SchemaUpgradeRequiredError` if not.

### Added

- **Polymorphic storage refactor (schema v3 → v7)** — Four parallel storage forks (events: `prompts`/`responses`/`tool_calls`; content: `prompt_content`/`response_content`; four `*_attributes` tables; four `*_tags` tables) dissolved into a unified polymorphic schema: `events` + sparse `event_response`/`event_tool_call`/`event_content` extensions + polymorphic `attributes` (target_kind, target_id) + `tag_assignments` (target_kind, target_id). Aggregations (exchange, turn) are query-time `parent_id` walks, never tables.
  - **Migration runs once on first open after upgrade.** Pre-migration backup via SQLite online backup API. Versions v4 (events schema), v5 (FTS5 simplification), v6 (legacy table drops + blob preservation), v7 (pending_tags exchange_index alignment).
  - **Granular tagging via colon-paths** — `siftd tag <conv>:<kind>:<n>` targets a specific prompt, response, tool_call, or exchange (1-indexed, deterministic ordering by `timestamp, id`). `<kind>` ∈ `{prompt, response, tool_call, exchange}`.
  - **Thinking blocks now FTS-searchable** — Live-write + migration + rebuild use uniform `$.text IS NOT NULL` filter; thinking content surfaces in `siftd query -s` results.
  - **`list_tags` returns `prompt_count` + `response_count` + `exchange_count`** alongside aggregate `usage_count`. Per-target-kind breakdown for tag inspection.
  - **Polymorphic cleanup triggers** — Cascade-orphan triggers on `events`, `workspaces`, `conversations` automatically clean orphaned `attributes` and `tag_assignments` rows. Replaces explicit cleanup calls.
  - **`siftd slice` opens source read-only** — Refuses with clear error if source `user_version < SCHEMA_VERSION` (no auto-upgrade, no backup file leakage).

- **Migrations as a first-class subsystem.** A `MIGRATIONS[v]` registry replaces the previous ad-hoc per-version code; each migration owns one transactional phase of work. Supporting tooling:
  - **`siftd db schema-version`** — Triage command. Reports current vs target version, lists applied / pending migrations, returns non-zero on schema-newer-than-binary (telling the user to upgrade siftd, not the DB).
  - **`siftd doctor` deep checks** — `db-fk-integrity`, `db-trigger-presence`, `db-blob-refcount-drift` audits. Run via `siftd doctor --deep`.
  - **Deep preflight gate on `db merge` / `db receive`** — Source databases are integrity-checked before merge so corruption isn't propagated; `PreflightError` carries the failing finding messages and source path so inbox failures stay traceable.
  - **Schema fixtures + parametrized upgrade tests** — `tests/fixtures/schemas/v{0..7}.sql` snapshot the schema at each version; tests walk every adjacent upgrade pair so migrations can't silently drift from the schema they target.
  - **Adapter golden fixtures** — Tiny canonical input/output pairs for each ingest adapter (`tests/fixtures/adapters/`) catch parser regressions when upstream CLIs change their log formats.

- **Auto-upgrade for read-only commands on stale-schema DBs** — `open_database(read_only=True)` peeks `user_version` and runs the migration in a transient write-mode open if the file is writable. If not writable, raises `SchemaUpgradeRequiredError` (re-exported via `siftd.api`) with a clear message instead of crashing later with `OperationalError("no such table: events")`. `auto_upgrade=False` opt-out for diagnostic callers (`db schema-version`, `db info`, `slice` source pre-check) that need to inspect the on-disk version without mutating it.

- **Schema v3: `content_blobs.ref_count` integrity** — Column now carries `NOT NULL DEFAULT 1 CHECK (ref_count >= 0)`. `release_content()` clamps via `MAX(ref_count - 1, 0)` and the delete trigger uses `<= 0` consistently. Migration garbage-collects any legacy `ref_count <= 0` rows (nulling dangling `tool_calls.result_hash` references first) before recreating the table with the new constraint; also patches the old delete trigger in-place for existing databases. Schema version bumped to 3.
- **Hash-collision detection (fail-closed)** — `store_content()` and `migrate_existing_results()` now verify existing blob content before reusing a hash. If two distinct content values produce the same SHA256 digest, a `BlobCollisionError` is raised instead of silently corrupting the stored blob.
- **`verify_migration` integrity report** — Two new keys: `ref_count_mismatches` (blobs where stored `ref_count` diverges from actual `tool_calls` reference count) and `negative_ref_counts` (pre-migration legacy corruption diagnostic; reports 0 on fully migrated databases).

### Changed

- **`siftd doctor fix` no longer auto-merges duplicate workspaces** — The duplicate-workspace finding is now informational-only. To merge, run `siftd migrate --merge-workspaces` manually.
- **Search-chunk JSON output omits `chunk_id` and `source_ids` by default** — These are storage-internal identifiers that were leaking through search results. Use `--debug-ids` (CLI) or `?debug_ids=1` (serve) to restore them. Conversation summaries/details are unchanged — `conversation_id` remains visible as the public addressable handle.
- **CLI logs to stderr at INFO** — `cli/__init__.py` `main()` configures the `siftd.*` logger with a `%(message)s` stderr handler so auto-upgrade and migration-progress events surface to users. Idempotent so test re-entry doesn't pile up duplicate handlers.

### Fixed

- **Migration v6 ref_count heal was O(M·N) and pinned a CPU for 44+ minutes** on a real 2.9 GB database before being killed. The naive correlated-subquery form scanned `event_tool_call` once per `content_blobs` row against an unindexed FK column. Rewritten as a single set-based `UPDATE` driven by a `WITH counts AS (… GROUP BY result_hash)` CTE, with a partial index on `event_tool_call(result_hash)` added in M6 (and to fresh schemas). Migration v3 → v7 against the same database now completes in ~40 seconds. Contract regression test asserts `EXPLAIN QUERY PLAN` of the heal query consults the index.
- **Schema migrations no longer run silently** — Each `MIGRATIONS[v]` phase emits an INFO log line with the row counts driving the work (e.g. `Migration v4: copying 34494 prompts, 454733 responses, 287176 tool_calls into events`). Plus two lines from the `open_database` runner: `Migrating schema vX → vY` and `Creating pre-migration backup: <name>`. Catches the previous failure mode where users assumed silent migrations were stuck and Ctrl-C'd them.
- **Doctor and similar read-only commands no longer create surprise WAL/SHM sidecars** under the new auto-upgrade path — the `_peek_user_version` helper opens the DB with `mode=ro&immutable=1`, mirroring the main RO connection, and the auto-upgrader runs `PRAGMA wal_checkpoint(TRUNCATE)` before closing so the upgraded `user_version` lands in the main DB file.
- **Doctor `CheckContext` lazy-init race under thread pool** — Concurrent doctor checks could double-initialize the per-context connection. Lock added.
- **Embeddings indexer connection lifetimes** — `try/finally` wrapping ensures connections are always closed on indexing failures.
- **Timestamp writes are UTC-aware** — Storage writers use `datetime.now(UTC).isoformat()` consistently; previously some paths emitted naive timestamps that compared incorrectly against ISO-Z reads.
- **Blob triggers dropped before `content_blobs` recreate in v3 migration** — Without dropping first, SQLite refused to recreate the table while triggers referenced it.

### Removed

- **Aider `analytics.jsonl` no longer discovered for ingest** — The file was yielded by discovery but produced zero conversations (parse was a no-op). Removed to eliminate a misleading no-op: the file appears in discovery output but nothing is ingested. Analytics ingestion is deferred until Aider publishes a stable schema for the file.

## [0.7.0] - 2026-04-24

### Changed

- **Search pipeline unified** — All search post-processing (metadata enrichment, file refs, context windows, conversation aggregation, thread tiering) moved from CLI to composable API primitives. CLI no longer contains direct SQL. `SearchChunk` and `ConversationSearchSummary` dataclasses in `domain/search_types.py` replace ad-hoc dicts as canonical result types. `--fts` path unified through same Operation IR as hybrid/semantic
- **Search API surface formalized** — Canonical search types and primitives (`SearchChunk`, `ConversationSearchSummary`, `search_chunks`, enrichment helpers, filtering, sorting, aggregation, and thread tiering) are exported through `siftd.api` and `siftd` while preserving lazy imports for optional embedding dependencies
- **Tag mutation extracted to API** — Three focused API functions (`apply_tags`, `rename_tag_safe`, `delete_tag_safe`) replace duplicated orchestration in CLI and serve. API owns connection lifecycle and transaction boundaries. Cross-owner protection SQL moved from serve route to `storage.tags.tag_used_by_other_owners` helper
- **Serve serializers made lossless** — Tags, tool search, and stats serializers now include all API dataclass fields. `dataclasses.asdict()` used as baseline in serialization layer. CLI rehydrate-with-defaults pattern replaced by strict API deserializers (`tag_info_from_dict`, `tool_search_payload_from_dict`, `dict_to_stats`)
- **`ScoreBreakdown` relocated to `domain/search_types`** — Breaks `search ↔ storage.embeddings` cycle and `output → search → storage` transitive coupling
- **`api → serialization` cycle broken** — `_stats_to_dict` inlined in `api.stats`, `serialization.stats.serialize_stats` delegates to it (correct one-way direction)
- **Ingest/backfill extracted to API** — `api.ingest.run_ingest` and `api.backfill.run_backfill` wrap ingestion pipeline with `db_path` lifecycle ownership. CLI no longer imports `siftd.ingestion` or `siftd.backfill` directly
- **Serve health and push logging moved behind API** — Health endpoint and push-log writer now go through `api.serve_status`. Health response shaped by `serialization.serialize_health_status` like every other route
- **Embedding availability moved behind API** — CLI status/search paths now use `siftd.api.embeddings_available` and API-exported index compatibility exceptions instead of importing optional embedding internals directly
- **Package root re-exports through API** — `siftd.apply_tag`, `siftd.list_tags`, `siftd.get_or_create_tag` now resolve via `siftd.api.tags` instead of `siftd.storage.tags`, plus new `apply_tags`, `rename_tag_safe`, `delete_tag_safe`. External `import siftd` consumers get connection-lifecycle-managed entry points
- **Config `↔` paths cycle broken** — `paths.db_path()` reads config.toml via stdlib `tomllib` instead of importing `siftd.config`
- **Sync config extracted** — 450 lines of sync-specific accessors (remotes, timeouts, SSH options, cursor mutations) moved to `config_sync.py`. `config.py` re-exports for backward compatibility

### Added

- **Anti-drift serializer tests** — Compare serializer output keys against `dataclasses.fields()` for `TagInfo`, `ToolSearchResult`, `DatabaseStats`, `SearchChunk`, `ConversationSearchSummary`. Prevents silent field omission when dataclasses change
- **Local/delegated JSON parity test** — Tool search `--json` output is schema-identical whether executed locally or via serve delegation
- **Tag mutation API tests** — Apply/remove/rename/delete with ownership protection, entity resolution, and edge cases
- **Tag mutation serialization** — `serialization/tags.py` with typed payload dataclasses and anti-drift tests
- **Ingest/backfill API tests and serializer drift tests** — Coverage for `IngestRunResult`, `BackfillRunResult` types
- **Dependency direction arch tests** — `api/` must not import `serialization/`, `storage/` must not import `api/`, `domain/` must be pure. Known `api↔serialization` cycle tracked as strict xfail (now resolved)
- **Boundary xfail cleanup** — Serve direct-storage and CLI direct-embeddings architecture tests now run as normal passing tests
- **Package-root storage-boundary arch test** — `siftd/__init__.py` is now scanned for direct `siftd.storage.*` imports, with `# arch: allow-storage` waiver
- **`asdict` matcher tightened** — `_find_dataclasses_asdict_calls` now catches both `dataclasses.asdict(x)` and bare `asdict(x)` (after `from dataclasses import asdict`); regression test pins both forms

### Fixed

- **VSCode empty-window sessions** — VSCode/Cursor/Windsurf chat discovery now includes `globalStorage/emptyWindowChatSessions`, so no-workspace chats are ingested instead of ignored
- **Codex tool-call preservation** — Codex CLI logs with tool calls before the first user prompt now get a synthetic prompt so those tool calls are attached to the conversation instead of dropped
- **Malformed JSONL tolerance** — Shared JSONL adapter loading skips malformed or non-object lines, which makes live/truncated logs from JSONL-backed adapters non-fatal during ingest
- **Optional embeddings imports** — Search and embedding helpers avoid importing optional embedding modules from broad package re-exports where possible, improving graceful behavior without the `[embed]` extra

## [0.6.4] - 2026-03-28

### Fixed

- **Sync: silent fallback to blocking merge** — When preflight capability negotiation failed (remote too old, SSH hiccup, missing `sync-status` command), push silently fell back to a blocking `receive_database()` merge over SSH. With large payloads this hangs until the 600s command timeout. Push now requires staged receive for SSH remotes and surfaces a clear error on version mismatch
- **Sync: zero-copy staging, race-safe inbox, HTTP staged mismatch** — `stage_payload` avoids unnecessary copy; inbox `processing` claim is atomic; HTTP push correctly routes through staged path when negotiated
- **Sync: cursor advancement and inbox recovery** — `last_sent` cursor tracks filter signature so filter changes invalidate stale cursors; stale `processing` rows are reclaimed after timeout; `last_sent` preferred over `last_push` for incremental slicing
- **Blob ref_count triggers and transaction atomicity** — `content_blobs` ref_count maintained by triggers; merge and ingest wrap related writes in explicit transactions
- **Storage lifecycle** — WAL-aware backup, sidecar cleanup on restore, migration column preservation, merge schema validation, workspace path-fallback
- **Ingest contract** — Explicit parse failures, race-safe multi-conversation rejection, session-dedup hash check, scoped adapter overrides
- **Tag lifecycle** — Cache invalidation on rename/delete, pending tag propagation, duplicate collapse
- **Search pipelines** — Retry, recency re-sort, candidate cap, and score writeback ported to API path; score propagation fix and render crash fix
- **SQL correctness** — Query-layer hardening across owner-scoped paths, boundary sanitization
- **Doctor** — False positive and negative fixes in health check modules

### Changed

- **Owner scoping unified** — SQL helpers for owner-scoped queries; htmx search, stats, tools, tags, and conversation routes all consistently scope by owner
- **Serve auth hardened** — Loopback bypass removed, owner scoping enforced on all write paths, delegation tokens, OIDC error redaction, fail-closed writes
- **Config permissions** — Config file permissions validated, cache TTL bounded

### Added

- **Architecture test** — CLI and serve must not import `siftd.search` directly (enforces API boundary)

## [0.6.3] - 2026-03-25

### Added

- **Sync protocol v2 — staged receive and capability negotiation**
  - `siftd db receive --stage` writes payload to inbox for deferred merge (fast ACK)
  - `siftd db process` merges all staged inbox payloads
  - `siftd db sync-status` reports receiver capabilities and inbox state as JSON
  - Pre-flight capability negotiation: push auto-detects staged support on the remote and adapts; falls back to blocking receive for old receivers
  - `SYNC_CAPABILITIES` replaces version-based negotiation — new features are capability strings, not version bumps
- **Split sync timeouts** — separate `connect_timeout_s` (TCP/SSH handshake) from `command_timeout_s` (total operation) at sync global, per-transport, and per-remote config levels
- **Per-remote sync filters** — `[sync.remotes.*.filters]` for workspace, tag, no_tag, owner scoping; CLI flags override config
- **Sync strategy config** — `strategy = "incremental" | "full"` at global and per-remote level; `--strategy` CLI flag on push/pull
- **`db send` filter flags** — `--tag`, `--no-tag`, `--owner` flags for filtered slice export over SSH
- **`GET /api/v1/sync/status`** — serve endpoint for HTTP capability negotiation
- **`no_tag` on pull endpoint** — `/api/v1/pull` now accepts `no_tag` query parameter
- **`sync_inbox` table** — tracks staged payload lifecycle (staged → processing → done/error)

### Fixed

- **Push timeout doom loop** — failed pushes now record `last_sent` before remote processing, so subsequent pushes are incremental even if merge times out
- **HTTP timeout not configurable** — `httpx.Client(timeout=300)` replaced with configurable `httpx.Timeout` using split connect/command values
- **`_build_ssh_options` return type** — annotation corrected from `dict` to `tuple[str, dict]`

## [0.6.2] - 2026-03-24

### Fixed

- **Homebrew install still broken** — `cryptography` can't reliably build from source even with `rust` + `openssl@3`. Formula now installs `cryptography`/`cffi`/`pycparser` via pip binary wheels before building remaining resources from source

## [0.6.1] - 2026-03-24

### Fixed

- **Homebrew install broken** — `cryptography` (transitive dep via asyncssh) failed to build from source. Formula now includes `rust` and `openssl@3` as build dependencies
- **Write routes crash without auth** — `require_write()` crashed when no auth middleware installed (Litestar `Request.user` raises instead of returning None)
- **`dev check` hid serve test failures** — Test scope widened from `not embeddings and not serve` to `not slow`

## [0.6.0] - 2026-03-24

### Added

- **htmx web UI** — Browse, search, and analyze conversations in the browser at `/`:
  - Conversation list with workspace/model/tag/date filters
  - Full detail view with collapsible turns, tool cards, and sticky header
  - Markdown rendering (mistune) and syntax highlighting (Prism.js)
  - Live search — semantic + FTS5 hybrid when embeddings available, FTS5 fallback
  - Search modes — chunks/conversations toggle with `aggregate_by_conversation()` API
  - Follow mode — live session tailing via `/follow` with 2s polling
  - Stats dashboard — summary cards, by-model token breakdown, by-workspace cost, top tools
  - Deep links — bookmarkable `?id=`, `?q=`, `?follow=` URLs via `hx-push-url`
  - Resizable panes — draggable divider between list and detail (JS, 15%-85% clamp)
  - Inline tagging — add/remove tags from conversation detail view
  - Export as document artifact from detail view
  - "The Instrument" design system with dedicated CSS (`siftd.css`)
  - Architecture tests enforcing route boundary separation
- **Authentication** — Three auth modes for `siftd serve`:
  - Static password (`serve.auth.static_token`) for local dev/testing
  - OIDC JWT validation against configurable issuer JWKS
  - RFC 7662 token introspection for OAuth2 deployments
  - Scope-based authorization: `required_scopes` gates all access (all-of), `write_scopes` gates tag/push operations (any-of)
  - Browser login form via htmx — 401 triggers token input, stored in sessionStorage
  - Loopback API bypass — CLI delegation on same machine works transparently with auth enabled
  - `env:VAR_NAME` syntax for secrets in config
- **Multi-tenancy** — Conversation ownership for shared databases:
  - `conversation_owners` table with push-time identity stamping
  - Owner-scoped queries across list, search, tool-search, and export
  - `--owner` CLI filter
  - `owner` promoted to first-class attribute on `ConversationSummary`
- **Operation IR** — `dispatch()` pattern for normalize→execute→render:
  - All commands migrated (Tier 1: stats/workspaces/tools/tags, Tier 2: detail/export/tool-search, Tier 3: tag writes, search)
  - Unified parameter names across CLI/HTTP/API — dissolves `_SERVE_PARAM_MAP`
  - HTML output format as fourth peer to terminal/markdown/JSON
- **Unified exception handling** — `safecall` module with codebase-wide migration
- **Serve as general daemon** — Stats cache, read-path delegation for query/workspaces/tools/tags/tool-search/export/detail, tag write delegation via `POST /api/v1/tag`
- **Serialization layer** — Extracted JSON output unification across CLI and API
- **Tool presenters** — Format-neutral extraction layer with 7 tool-specific extractors (file.read, file.edit, file.write, shell.execute, search.grep, file.glob, ui.todo) plus generic fallback. Consumed by both painted bridge and HTML formatter
- **Narrative emitter protocol** — `PaintedEmitter` and `HtmlEmitter` share `walk_narrative()` as single source of truth for fidelity gating
- **Configuration reference docs** — Auto-generated from config schema via `./dev docs`. All config keys documented with types, defaults, and descriptions
- **`get_config_table()`** — New API for reading TOML sections as dicts (e.g., `serve.auth`)

### Changed

- **URL restructure** — UI serves from `/` (was `/ui`), JSON API at `/api/v1/` (was `/v1/`). Health endpoint at `/api/v1/health`
- **Adapter SDK: record normalizer pattern** — Adapters that implement `normalize_record()` get `peek_scan`, `peek_exchanges`, and `peek_tail` for free via `make_peek_hooks()`. Replaces per-adapter custom peek implementations with a single SDK code path
- **Peek coverage: 3/8 → 7/8 adapters** — Pi Agent, Copilot CLI, and VSCode gain peek support. Claude Code, Codex CLI, and Gemini CLI migrated from custom peek to normalizer-derived
- **Adapter boilerplate reduction** — All adapters now use `build_harness()`, `flush_pending_calls()`, and `discover_files()` from the SDK. Net ~580 lines removed from adapters
- **Subagent detection promoted to SDK** — `SUBAGENT_PATH_MARKER` and `extra["agent_id"]` in `NormalizedRecord` enable any adapter to support session hierarchy, not just Claude Code
- **painted bridge simplified** — 7 `_render_*_lines` functions and duplicated JSON parsing replaced by single `_presentation_to_lines` consuming `ToolPresentation`. Net ~350 lines removed
- **Search findability** — Porter stemmer and tool descriptions in FTS5 index (+21% FTS5 recall). Tool summary embeddings (+44% semantic recall@10). AND→OR priority FTS5 query logic. Hybrid search trusts FTS5 ranking when it finds sufficient candidates
- **`siftd query` is ~50× faster** — Covering index on `response_attributes`, two-phase query, `WhereBuilder` JOIN tracking, `EXISTS` subquery for model filter, materialized `conversation_stats` table. Default query from ~3.5s to ~70ms
- **Storage test coverage** — 18.4% → 100% via 27 autoresearch runs. All 10 storage modules at 100% coverage
- **Adapter test coverage** — Per-adapter test files split from monolith. Claude Code 99.3%, Codex CLI 99.5%, VSCode 100%, OpenCode 99.4%, cross-format normalizer validation (50 tests)
- **CLI refactored to package** — `cli/` is now a proper package with focused submodules per command
- **Dead config removed** — `search.formatter` and `search.serve_delegate` config keys removed (superseded by Operation IR and `serve.delegate`)
- **Structured error responses** — `_dispatch()` catches exceptions and returns JSON errors instead of raw tracebacks
- **`~110 lines removed`** — Dead `_delegate_search_via_serve` code path removed

### Fixed

- **Cost coverage** — Pricing JOIN routed through harness source as fallback. NULL-safe cost expression (missing pricing → NULL, not 0.0). Bundled pricing seed for 10 models. Estimated 50% → 75% cost coverage
- **Editable install detection** — `siftd upgrade` now detects editable `uv tool` installs
- **HTML route escaping** — XSS-relevant escaping bugs fixed in html_routes
- **Connection leak** — Fixed in html_routes detail endpoint
- **`embed_installed()` / `_serve_installed()`** — Use `importlib.util.find_spec` instead of try/import to avoid side effects
- **`rename_tag` signature** — `conn` moved to keyword-only, consistent with other tag functions
- **Serve auth config loading** — `get_config()` returns None for dicts; fixed to use `get_config_table()`

## [0.5.5] - 2026-03-20

### Changed

- **Ingest is ~3× faster** — Full fresh ingest dropped from ~115s to ~38s (67% reduction) across ~6,400 files. Optimizations:
  - Cache workspace identity lookups to avoid repeated `git remote` subprocess calls
  - SQLite WAL mode with tuned pragmas (`synchronous=OFF` during bulk ingest, 64MB cache, 256MB mmap, deferred foreign keys)
  - In-process vocabulary caches for harness/provider/model/tool/tag lookups
  - Batched `os.urandom()` and unrolled encoding for ULID generation
  - `hashlib.file_digest()` for file hashing (Python 3.11+)
  - `INSERT OR IGNORE` for tag application instead of SELECT+INSERT
  - Early `len()` check in binary content filter to skip regex on short strings

## [0.5.4] - 2026-03-20

### Added

- **Multi-harness skill install** — `siftd install skill` now supports `--harness` to install the siftd skill/instructions for different agents: Claude Code (default), Pi Agent, Codex CLI, Gemini CLI, Copilot CLI, Aider. Claude Code and Pi get the structured skill (SKILL.md + reference/); other harnesses get a rendered plain-markdown instructions file.
- **`siftd install skill`** — Lightweight alternative to the full plugin. Installs just the /siftd decision tree and reference docs without hooks or commands.
- **`/siftd:query` and `/siftd:peek` commands** — New slash commands for browsing conversations and viewing live sessions.
- **Stop hook** — Auto-runs `siftd ingest -a claude_code` on session exit (~0.7s) to apply pending tags queued during the session.
- **Per-session hint dedup** — PostToolUse hints fire once per subcommand per session via marker files, reset on SessionStart.
- **Bare `siftd install`** — Shows available components and supported harnesses instead of an argparse error.

### Fixed

- **Live tagging bug** — `session-start.sh` now registers sessions unconditionally (not gated on `reason` field detection, which was fragile across Claude Code versions). Added DB fallback in `--current` session detection with stderr feedback when falling back to `--last 1`.
- **Subagent pending tags** — Tags queued against a parent session ID now apply to subagent conversations. When a subagent conversation is ingested, `_apply_pending_tags` falls back to the parent session ID (strips `::agent::` suffix) if no tags match the subagent's own external_id.
- **Single-scope harness defaulting** — Harnesses with exactly one supported scope (copilot_cli, aider) auto-default to it instead of failing when `--scope user` is the implicit default.
- **Symlink cleanup on plugin install** — Plugin install now removes symlinked standalone skills (was skipping them, causing duplicate /siftd entries).
- **`conversation_stats` commit convention** — `ensure_conversation_stats_table` and `rebuild_conversation_stats` now follow the project `commit=False` convention.

### Changed

- **Slimmed SKILL.md** — Reduced from 305 to 70 lines. SKILL.md is now a decision tree; exhaustive flag lists live in `reference/*.md`.
- **Tightened hook sensitivity** — Removed 16 generic patterns from UserPromptSubmit (false-positive-prone phrases like "what did we", "last time"). Kept only explicit "siftd" mentions and "past/earlier/previous session/conversation".
- **Commands stripped of static hints** — No more "Next steps" boilerplate in command output; PostToolUse hook provides contextual, deduplicated hints.
- **Plugin version** — Bumped to 1.1.0.

## [0.5.3] - 2026-03-20

### Fixed

- **`siftd query` is ~50× faster** — `siftd query` dropped from ~3.5s to ~70ms. Several compounding issues fixed:
  - Added covering index on `response_attributes(key, response_id, value)`, eliminating a full 479K-row table scan on every query
  - Rewrote `list_conversations` as a two-phase query: Phase 1 identifies conversation IDs cheaply; Phase 2 computes stats only for matched rows
  - `WhereBuilder` now tracks which JOINs each filter actually needs — the default query (no `--model`) no longer scans 363K response rows
  - `--model` filter rewritten from a JOIN to an `EXISTS` subquery that stops at first match
  - Added `conversation_stats` materialized table, rebuilt at the end of each `siftd ingest`. Query reads precomputed counts, tokens, model, and cost from a single row per conversation instead of aggregating the responses table on the fly. `siftd query --limit 0 --since 30d` (1600+ conversations) dropped from ~3s to ~46ms.

## [0.5.2] - 2026-03-19

### Fixed

- **`siftd upgrade` on Homebrew** — Runs `brew update` before `brew upgrade` so the tap formula is current. Suppresses stale "update available" notice after successful upgrade.

## [0.5.1] - 2026-03-19

### Changed

- **Export rewrite** — `siftd export` now renders full conversation exchanges as markdown by default (both user and assistant sides). Previous default showed only user prompts.
- **Narrative-aware rendering** — Export walks the full NarrativeBlock structure from the DB instead of collapsing to flat text. Thinking blocks show as `*[thinking]*` placeholders, tool calls as consolidated summaries like `*[file.read ×6, shell.execute ×2]*`
- **New export flags** — `--thinking` expands thinking blocks, `--tools` expands tool inputs/results, `--full` enables both, `--brief` truncates long text, `--json` for structured output. Timestamps included per turn.
- **Breaking:** Removed `--format`, `--prompts-only` flags. Old `prompts` and `exchanges` formats replaced by single markdown format.

### Fixed

- **Homebrew formula missing transitive deps** — Formula generator now walks the full dependency tree (BFS), fixing `wcwidth` missing from painted

## [0.5.0] - 2026-03-19

### Added

- **`siftd upgrade`** — Check for and install updates. Detects install method (uv tool, pipx, Homebrew, pip) and runs the right upgrade command. `--check` flag for check-only mode
- **Passive update check** — After any command, a background thread checks PyPI once every 24 hours. If a newer version exists, a one-line notice prints to stderr on the next invocation. Disable with `siftd config set update.check false` or `SIFTD_NO_UPDATE_CHECK=1`
- **`siftd tool-search`** — Search tool usage across conversations
- **`siftd tag --current`** — Auto-detect the active session and queue tags, falling back to `--last` when no session is registered
- **`siftd serve`** — HTTP team sync server (`siftd[serve]` optional extra):
  - 5 endpoints: `POST /v1/push`, `GET /v1/pull`, `GET /v1/query`, `GET /v1/search`, `GET /v1/health`
  - Auth middleware: OIDC JWT validation and RFC 7662 token introspection
  - Client-side token acquisition: `token_command` > `env:VAR` > `file:path` resolution
  - Push attribution: `push_log` table records identity/IP/timestamp, conversations tagged `pushed_by:<identity>`
  - FTS rebuild strategies: `on_push` (default), `scheduled`, `off`
  - Built on Litestar; HTTP transport auto-detected from remote URL prefix
- **`db pull`** — Pull conversations from a remote database (inverse of `db push`):
  - SSH and HTTP transport (auto-detected from remote URL)
  - Local-path transport slices remote DB directly
  - `--since`, `--all`, `--dry-run`, `-w` filters mirror push
  - `last_pull` delta tracking — repeated pulls transfer only new conversations
- **`db send`** — Slice database to stdout as binary SQLite (inverse of `db receive`)
- **Porter stemmer for FTS5** — Improves keyword recall for morphological variants
- **Scoped FTS5 passthrough** — Field-scoped queries (e.g. `tool:read`) pass through to FTS5 directly
- **Tool summary embeddings** — Tool call patterns embedded alongside text for semantic search over tool usage
- **In-memory embedding cache** — Cached embeddings, backend resolution, and active-session exclusion sets with TTL-based invalidation
- **CLI→serve delegation** — Search commands delegate to serve endpoint when configured
- **`acquire_token()`** — Public API function for token acquisition from auth config

### Changed

- **Output rendering migrated to painted** — Peek and query detail views render through painted's block/line primitives with the three-axis Fidelity model (visibility × depth × density), replacing the single-axis zoom system
- **`--brief` / `-b` and `--full` / `-F` flags** — Aliases for compact and full-depth rendering on peek and query detail
- **Tool-specific presenters** — file.read, file.edit, file.write, shell.execute, search.grep, file.glob, and ui.todo render structured hints instead of raw input dumps
- **Unified `tag` command** — `siftd tags` merged into `siftd tag` with subcommands (`list`, `rename`, `delete`). `siftd tags` still works as deprecated bridge
- **FTS5 tokenizer upgrade** — Content keyword search now uses the Porter stemmer; opening an existing DB in write mode will rebuild the FTS index once to apply stemming
- **Embeddings index tool summaries** — `siftd search --index` now adds a per-conversation `tool_summary` chunk and will backfill missing summaries for already-indexed conversations that have tool calls
- **Search defaults to embeddings-only** — Skips FTS5 recall pass for lower latency; hybrid mode still available
- **Connection tracking** — Read-only connections reopened on cache reload to escape stale snapshots

### Fixed

- **Search caching correctness** — Fixed -inf score leak, stale cache detection, active-session exclusion underfill, and cache TTL regression
- **Tool-only conversations skipped** — Conversations with only tool calls (no text) were silently dropped during ingestion
- **`event_to_json` missing narrative** — `--follow --json --thinking` no longer silently drops thinking content
- **Trailing whitespace in block rendering** — painted 0.1.4 strips trailing space cells, fixing terminal line-wrap on wide blocks

### Removed

- **Deprecated top-level commands** — `siftd status`, `siftd workspaces`, `siftd path` removed (deprecated since v0.4.4)
- **Zoom module** — Replaced by painted's Fidelity model

## [0.4.7] - 2026-02-18

### Added

- **4 new adapters** — Expanding tool coverage beyond the original 4:
  - **VSCode Chat** — `~/.config/Code/User/History/chat/` (JSON and JSONL formats)
  - **Pi Coding Agent** — `~/.pi/agent/sessions/` (JSONL with thinking blocks, tool calls, usage/cost)
  - **OpenCode** — `~/.local/share/opencode/opencode.db` (SQLite adapter using `open_external_db()`)
  - **Copilot CLI** — `~/.local/state/.copilot/session-state/` (JSONL with subagent tracking)
- **Configuration surface** — Expanded `siftd config` with schema validation:
  - `db.path` — Override default database location
  - `query.limit`, `query.format`, `query.workspace` — Query defaults
  - `tools.limit` — Default tool listing limit
  - `adapter.locations.<name>` — Override adapter search paths
  - `config set --append` / `--remove` for list-valued keys
  - Known-keys registry with schema validation on `config set`
- **Friendlier ingest output** — Progress reporting with per-adapter counts, timing, and `--json` flag for structured output
- **`session-tools` bundled query** — Per-tool-call character counts for a session (`:session` named parameter)
- **`open_external_db()`** — SDK helper for adapters that read external SQLite databases (read-only URI mode)

### Changed

- Unified display formatting with shared output helpers across CLI modules
- Adapter exclusion markers prevent cross-adapter file mismatches (Pi Agent, Copilot CLI paths excluded from Claude Code adapter)

### Fixed

- SQL validation in architecture tests now handles `:var` named parameters (was only normalizing `$var`)
- Prysk acceptance tests use `python3` instead of bare `python` (macOS compatibility)
- Homebrew tap name and upgrade instructions corrected

## [0.4.6] - 2026-02-12

### Added

- **`siftd install plugin`** — Install the bundled Claude Code plugin to user or project scope:
  - Bundles plugin into wheel via hatch force-include
  - `--scope user` (default) installs to `~/.claude/plugins/siftd/`
  - `--scope project` installs to `.claude/plugins/siftd/`
  - Symlink-safe cleanup replaces dev-mode symlinks with real directories
  - `--dry-run` shows source/target without writing
- **`db merge`** — Import an external database (slice) into the main database:
  - Vocabulary ID remapping — same harness/model/workspace with different ULIDs across machines are matched by natural key
  - Workspace matching by `git_remote` (priority) with `path` fallback
  - Replace-by-default — re-ingested conversations (newer ULID, same external_id) replace stale target versions with full cascade
  - `--no-replace` flag to keep existing versions (first-version-wins)
  - `--dry-run` previews merge counts without modifying the target
  - Schema version guard rejects cross-version merges
  - Content blob dedup via SHA256, ref_count recomputation, FK integrity validation
- **`db push`** — Push conversations to a shared remote database
- **Tags temporal filtering** — `siftd tags` accepts `--since`/`--until` for time-scoped tag views
- **Installation guide** — New `docs/guides/install.md` covering siftd and plugin setup

### Changed

- Plugin consolidated to single skill with slash commands
- Concept docs rewritten; example outputs updated to match actual CLI formats
- CLI help text reorganized by functional group

## [0.4.5] - 2026-02-10

### Added

- **`peek --follow` mode** — Real-time session tailing for monitoring live agents:
  - Streams turns as they arrive with text and tool call summaries
  - Tool hints: file paths, commands, search patterns extracted from tool inputs
  - `--json` output produces NDJSON for piping to jq
  - `--exchanges N` controls initial context window (default 3)
  - Auto-selects most recent active session when no ID given
  - Respects `--workspace` and `--branch` filters for session auto-selection
- **Tool hint extraction** — `extract_tool_hint()` summarizes tool_use inputs (file paths truncated to last 2 components, commands, patterns, queries)
- **`TOOL_HINT_KEYS`** — Adapter-specific mapping for hint extraction from Claude Code tool schemas
- **`db slice` filter args for tags drill-down** — `siftd tags` now accepts filter pipeline args

### Fixed

- **Tool accumulation in peek exchanges** — Multi-turn assistant exchanges now show all tools used across turns, not just the last turn's tools
- **Placeholder-only response text** — Assistant turns containing only `[tool: X]` placeholders no longer latch as the exchange response text
- **Assistant-first exchanges** — Sessions starting with an assistant turn (no preceding user record) now create a proper exchange instead of being silently dropped
- **Follow loop robustness** — Inode-aware file reopening for log rotation, truncation recovery seeks to start of file, proper file handle cleanup in finally block
- **`db slice` column order** — ALTER TABLE column ordering bug in slice export

### Changed

- Homebrew formula generation uses PyPI JSON API directly (replaces `homebrew-pypi-poet` dependency)

## [0.4.4] - 2026-02-10

### Added

- **`siftd db` namespace** — Container-level database operations:
  - `db info` — file metadata, page size, journal mode, schema version, FTS5 status
  - `db stats` — database statistics (absorbs `siftd status`)
  - `db workspaces` — list workspaces (absorbs `siftd workspaces`)
  - `db path` — show XDG paths (absorbs `siftd path`)
  - `db vacuum` — compact database and optimize indexes, reports size savings
  - `db backup <file>` — consistent online backup via `sqlite3.Connection.backup()`
  - `db restore <file>` — restore from backup with SQLite magic-byte validation
  - `db slice <file>` — export filtered conversation subset into standalone SQLite database
- **`db slice` filter pipeline** — Full filter vocabulary available: `-w`, `-m`, `--since`, `--before`, `-l`, `--exclude-tag`, `--tool`, `--tool-tag`, `-s`
- **Shared filter args** — `cli_filters.py` with `FilterArgs` dataclass, `add_filter_args()`, `extract_filter_args()` replacing 3 copy-pasted filter blocks
- **Codex CLI token extraction** — Token usage parsing from Codex CLI sessions
- **Token coverage metrics** — Track token extraction completeness across adapters
- **Cache-aware cost calculation** — Cost queries account for cache read tokens
- **CLI display ergonomics** — Status enrichment, query cost display, peek adapter info

### Changed

- `siftd status`, `siftd workspaces`, `siftd path` are deprecated with stderr warnings; use `siftd db stats`, `siftd db workspaces`, `siftd db path`
- Shared filter pipeline reduces CLI argument duplication across query, search, export, slice
- Architecture tests hardened: CLI SQL hygiene checks, `TYPE_CHECKING` import handling, peek types moved to domain

### Fixed

- Cache JOIN duplication in cost queries producing inflated token counts
- Turn narrative: ID-based tool matching, `tool_result` rendering, Gemini thinking block handling
- Token filter relaxed to avoid dropping valid zero-token responses
- `workflow_dispatch` added to publish workflow for manual re-trigger

## [0.4.3] - 2026-02-09

### Added

- **Narrative detail view** — `siftd query <id>` renders response content as interleaved narrative blocks (text, tool calls, thinking) instead of flat prompt/response pairs:
  - `--thinking` flag to include model reasoning blocks
  - `--tools [FILTER]` to show tool inputs/results (optional filter: tool name prefix or `errors`)
  - `--tool-chars N` to control tool content truncation
- API wrappers: `list_workspaces`, `resolve_entity_id`, `get_recent_conversation_ids`
- `resolve_db` helper — centralizes database path resolution across CLI modules
- Declarative dependency manifest for architecture enforcement with violation ratchet

### Changed

- **`Turn` is now the primary conversation detail structure** — `ConversationDetail.turns` is the source of truth; `.exchanges` is a backward-compatible derived property (one per prompt, not per response). Consumers using `.exchanges` continue to work unchanged.
- Detail view summary line says `Turns:` instead of `Exchanges:`
- CLI fully decomposed — `cli.py` is now a 59-line dispatcher; logic extracted to `cli_common`, `cli_meta`, `cli_sessions`, `cli_tags`, `cli_query`, `cli_data`, `cli_peek`, `cli_export`
- `tag --last` defaults to 1 when count omitted
- Lazy imports in `cli_data.py` for adapters, backfill, and ingestion modules

### Fixed

- `search --json` no longer errors on empty result sets
- Connection leak in `_search_fts_only` (try/finally)
- `open_database` import consistency in `api/search.py`

## [0.4.0] - 2026-02-05

### Added

- **Unified `search` command** — Replaces `siftd ask` with auto-selection:
  - Semantic search when embeddings available, FTS5 fallback when not
  - `--semantic` flag to force semantic mode (errors if embeddings missing)
  - `--by-time` flag for chronological ordering
- **Live session tagging** — Tag active sessions before they're ingested:
  - `/siftd:tag` Claude Code skill for tagging from within sessions
  - `active_sessions` and `pending_tags` tables for deferred tag application
  - Tags applied automatically at next ingest
  - `siftd doctor fix --pending-tags` to clean up orphaned/stale pending tags
- **Binary content filtering** — Binary blobs filtered during ingest; metadata placeholder preserves type/size info
- **Workspace identity** — Git remote URL as primary identifier, resolved path fallback for non-git dirs
- **Git worktree resolution** — Worktrees resolve to main repo workspace; branch tracked separately
- **Peek improvements**:
  - Subagent detection and grouping
  - Worktree branch identity: `[branch]` suffix in display, `--branch` filter
  - `--last-response` / `--last-prompt` flags for quick extraction
- **Unified output formatting** — `--brief` / `--summary` modes for `query`; `--exchanges N` for `peek`
- **Skill interface versioning** — `skill-interface-version: 1` in skill frontmatter for stability promises
- **Index compatibility validation** — Embedding index now tracks schema version, backend, model, and dimension:
  - Actionable error messages when backend/model mismatch detected
  - `EmbeddingsCompatCheck` doctor check for configuration drift
  - Incremental indexing blocked when it would mix incompatible embeddings
- **Score explainability** — `--json` output includes `breakdown` with component scores:
  - `embedding_sim`, `recency_boost`, `pre_mmr_score`, `mmr_penalty`, `mmr_rank`, `final_score`
  - `fts5_matched` and `fts5_mode` for hybrid search transparency
- **Deterministic search results** — Chunk ID (ULID) used as tie-breaker throughout scoring pipeline
- **3 new doctor checks**:
  - `fts-stale` — Detects FTS5 index out of sync with content tables
  - `fts-integrity` — Checks FTS5 table integrity for corruption
  - `config-valid` — Validates config file syntax and formatter names
- CLI help argument groups for organized `--help` output
- Helpful hints when `query` returns empty results
- MMR safety cap to prevent unbounded memory on large result sets
- `siftd ingest --rebuild-fts` — Rebuild FTS index from existing data without re-ingesting

### Changed

- **Breaking:** `siftd ask` renamed to `siftd search`
- **Breaking:** Removed deprecated `query -s/--search` flag — use `siftd search --fts` instead
- **Breaking:** Removed deprecated `query --count` flag — use `-n/--limit` instead
- **Breaking:** Removed deprecated `peek --last` flag — use `-n/--limit` instead
- `siftd peek` defaults to 10 sessions (was unbounded 2-hour window); use `-n/--limit` to control
- `siftd status` query performance optimized
- `--exclude-tag` renamed to `--no-tag` in export command (consistency with other filters)
- Narrowed `siftd.api` public exports — internal search primitives moved to `siftd.api.search`
- Removed phantom dependencies: `httpx`, `tqdm`, `pyyaml`, `loguru`
- Architectural tests moved to `tests/architecture/` for clearer separation

### Fixed

- Schema version tracking via `PRAGMA user_version` — prevents older siftd from opening newer databases
- `siftd query --since invalid` now shows clear error instead of silently returning empty results
- `siftd` with no args now shows help instead of terse argparse error
- Empty-filter query tip now suggests broadening filters instead of re-running ingest
- Connection leak safety: all `search.py` database connections wrapped in try/finally

- **P0**: Session ID mismatch in live tagging — hooks now use namespaced `claude_code::sessionId`
- **P1**: Active session staleness detection — added `last_seen_at` timestamp
- Peek session lookup: O(n) scan → O(1) path-based filtering
- Workspace resolution for git worktrees (worktrees assigned to correct workspace)
- Peek session resolution prefers parent session over subagents
- `siftd peek` Ctrl+C now exits cleanly (exit code 130) instead of stacktrace
- `--by-time` warns when it has no effect (no temporal data)
- Test isolation issues with XDG_CONFIG_HOME in ask tests

## [0.3.0] - 2026-01-30

### Added

- `--since`/`--before` accept relative dates: `7d`, `1w`, `yesterday`, `today`
- `--recency` flag for temporal weighting in semantic search (with `--recency-half-life`, `--recency-max-boost`)
- Automatic batching for large IN() lists (avoids SQLite 999-variable limit)
- Help examples in `siftd ingest` and `siftd backfill` epilogs

### Changed

- Vector search uses numpy batch operations (14-21x faster); numpy now a core dependency
- `exclude_conversation_ids` filter pushed to SQL for incremental indexing
- Unknown `--format` values error with available options (was silent fallback)

### Removed

- `--role` flag from `siftd ask` (exchange chunks always matched; not worth fixing)

## [0.2.0] - 2026-01-30

### Added

- **Hard rules enforcement tests** — Automated CI checks for architectural invariants:
  - `sqlite3.connect()` outside storage/ (AST-based)
  - stderr hygiene (tips/warnings must use stderr)
  - Built-in query SQL validation
  - Built-in adapter compliance
  - Formatter registration validity
  - JSON output purity
- **Privacy warnings** — `--full` and `--refs` flags now print warning to stderr about sensitive content

### Changed

- `--thread --json` now warns and ignores `--thread` (JSON formatter doesn't support thread grouping)
- FTS5 error handling improved — "no such table" gives "run ingest first" hint, other errors suggest `siftd doctor`
- Date examples in docs/help now use ISO format (`2024-01-01`) instead of unsupported relative dates

### Removed

- `--latest` flag from `siftd query` — was a no-op (newest-first is the default)

### Fixed

- `--thread` mode no longer trims widened candidate pool to `--limit`
- `--first` now respects `--threshold` (was hardcoded to 0.65)
- `--first` now sorts by prompt timestamp, not conversation start time
- `--json --refs` combination now errors instead of producing invalid JSON
- All search paths use `open_database(read_only=True)` — no WAL/SHM files on read-only media
- `first_mention()` docstring: `source_ids` is required, not optional
- `fts5_recall_conversations()` docstring: mode is "and/or/none", not "prefix/exact/none"
- Multiple stderr hygiene fixes in CLI (tips/warnings now correctly go to stderr)

## [0.1.1] - 2026-01-29

### Added

- `siftd install embed` — Convenience command to auto-detect installation method and install embedding dependencies
- `:var` parameterized syntax for query files — safe quoting via sqlite3, alongside existing `$var` text substitution
- `ADAPTER_INTERFACE_VERSION = 1` — Required attribute for all adapters, enables future interface migrations
- `ON DELETE CASCADE` on schema foreign keys — Child records now cascade on parent delete

### Changed

- Adapter `discover()` function now requires `locations` keyword argument (fallback removed)
- Error messages for missing `[embed]` extra now reference `siftd install embed` and suggest FTS5 alternative

### Removed

- `Conversation.default_model` field — Was defined but never populated or used

### Fixed

- Type checker (`ty`) configuration for optional dependencies — No longer blocks commits
- `bench/corpus_analysis.py` type annotation bug

## [0.1.0] - 2026-01-28

Initial public release.

### Added

#### Core Features
- **Ingestion** — Aggregate conversation logs from multiple CLI coding tools
- **FTS5 Search** — Full-text search across all conversations via `siftd query -s`
- **Semantic Search** — Vector similarity search via `siftd ask` (requires `[embed]` extra)
- **Tagging** — Apply tags to conversations, workspaces, and tool calls for organization

#### Adapters
- Claude Code (Anthropic) — `~/.claude/projects`
- Aider — `~/.aider`
- Gemini CLI (Google) — `~/.gemini/tmp`
- Codex CLI (OpenAI) — `~/.codex/sessions`
- Drop-in adapter support via `~/.config/siftd/adapters/`
- Entry-point adapter registration for pip-installable adapters

#### CLI Commands
- `siftd ingest` — Ingest logs from all discovered sources
- `siftd status` — Show database statistics
- `siftd query` — List/filter conversations with flexible filters
- `siftd ask` — Semantic search over conversations (optional `[embed]` extra)
- `siftd tag` — Apply or remove tags on entities
- `siftd tags` — List, rename, or delete tags
- `siftd tools` — Summarize tool usage by category
- `siftd export` — Export conversations for PR review workflows
- `siftd doctor` — Run health checks and maintenance
- `siftd peek` — Inspect live sessions from disk (bypasses SQLite)
- `siftd path` — Show XDG paths
- `siftd config` — View or modify configuration
- `siftd adapters` — List discovered adapters
- `siftd copy` — Copy built-in resources for customization
- `siftd backfill` — Backfill derived data from existing records

#### Query System
- User-defined SQL queries via `~/.config/siftd/queries/*.sql`
- `$var` syntax for text substitution
- Built-in queries: `cost.sql`, `shell-analysis.sql`

#### Python API
- `siftd.api.list_conversations()` — Query conversations with filters
- `siftd.api.get_conversation()` — Get full conversation detail
- `siftd.api.export_conversations()` — Export for external tools
- `siftd.api.hybrid_search()` — Combined FTS5 + semantic search

#### Storage
- SQLite with FTS5 for full-text search
- ULID primary keys throughout
- Normalized schema with proper foreign key constraints
- Extensible `*_attributes` tables for variable metadata

#### Developer Experience
- XDG Base Directory compliance for paths
- `--db PATH` override for all commands
- JSON output mode for scripting (`--json`)

---

[Unreleased]: https://github.com/kgruel/siftd/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/kgruel/siftd/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/kgruel/siftd/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/kgruel/siftd/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/kgruel/siftd/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/kgruel/siftd/compare/v0.6.4...v0.7.0
[0.5.5]: https://github.com/kgruel/siftd/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/kgruel/siftd/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/kgruel/siftd/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/kgruel/siftd/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/kgruel/siftd/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kgruel/siftd/compare/v0.4.7...v0.5.0
[0.4.7]: https://github.com/kgruel/siftd/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/kgruel/siftd/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/kgruel/siftd/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/kgruel/siftd/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/kgruel/siftd/compare/v0.4.2...v0.4.3
[0.4.0]: https://github.com/kgruel/siftd/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kgruel/siftd/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kgruel/siftd/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/kgruel/siftd/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kgruel/siftd/releases/tag/v0.1.0
