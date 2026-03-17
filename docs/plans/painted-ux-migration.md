# Painted UX Migration Plan

## Status

Active migration plan for the current UX-focused branch.

Current branch name: `feat/painted-ux-migration`

Current state as of 2026-03-17:
- Stage 0 is complete: `painted` is now a real siftd dependency and the bridge/seam exists
- Stage 1 is complete for `query <id>` / `query <id> --full`
- Stage 2 is complete for `peek <id>` / `peek <id> --full`
- `query` and `peek` detail views now share the same painted-backed projection family
- human-readable timestamps now display in local time instead of raw stored UTC strings
- `--thinking` and `--tools` now share the same painted-backed detail family, but remain distinct feature reveals: `--thinking` exposes thinking, `--tools` exposes tool payloads, and `--full` shows both
- an upstream ANSI spacing bug was found in `painted`, fixed, and released as `painted 0.1.2`
- siftd now depends on `painted>=0.1.2` and no longer needs a local rendering workaround
- next target is now Stage 3: `peek --follow` painted migration

## Why this exists

`siftd` has crossed the threshold where output quality is no longer a small formatting concern.

We now have:
- richer narrative data for conversations, peek sessions, and follow events
- inline thinking support
- fuller tool input/result display
- growing expectations that `query`, `peek`, and `peek --follow` behave like the same product at different levels of detail

At the same time, the current renderer is still mostly string assembly plus `print()`.

`painted` is a strong fit for the next phase because it already provides:
- semantic zoom/fidelity concepts
- styled and plain terminal output paths
- immutable rendering primitives (`Span`, `Line`, `Block`)
- compositional layout primitives (`join_vertical`, `pad`, `border`, etc.)
- a semantic palette model
- an eventual path to live/in-place and TUI delivery

This plan proposes a staged migration of siftd's human-readable UX onto `painted`, while keeping JSON and API output cleanly separate.

## Goals

1. Move human-readable output rendering onto `painted`
2. Keep `query`, `peek`, and `peek --follow` visually and semantically aligned
3. Replace ad hoc verbosity/truncation behavior with a cleaner zoom-based model
4. Preserve a strict separation between:
   - data normalization
   - narrative projection
   - terminal delivery
5. Make the system easy to remix later for alternate lenses, zoom levels, and eventual TUI/live views

## Non-goals

For this migration, we will **not**:
- change JSON output semantics
- push `painted` primitives into storage/API layers
- adopt in-place live rendering yet
- build a full TUI yet

Those can come later after the static migration is clean.

## Retrospective update — 2026-03-17

A detailed retrospective for the first implementation slice lives in:

- `docs/plans/painted-ux-migration-retrospective-2026-03-17.md`

Short version:
- starting with `query <id>` was the right move
- the bridge layer is a good seam and kept painted out of core data/storage paths
- real TTY validation turned out to be mandatory
- the most painful bug in this slice was not a siftd data/rendering bug, but a `painted` ANSI delivery bug
- fixing that upstream and requiring `painted>=0.1.2` was the right long-term trade

Implication for the rest of this plan:
- keep the staged migration order
- validate every stage in a real terminal, not just captured output
- prefer upstream renderer fixes over local bridge hacks when the bug clearly belongs in `painted`

## Design principles

### 1. One narrative, many projections

Every siftd view should be the same underlying story at different zoom levels.

Examples:
- list mode = far zoom
- default detail = summary zoom
- `--thinking` / `--tools` = feature-expanded summary within the same detail family
- `--full` = full zoom
- follow mode = live projection of the same narrative model

### 2. Data first, rendering second

Narrative data remains a siftd concern.

`painted` should only enter once we already have normalized renderable data such as:
- turns
- narrative blocks
- tool calls
- follow events

### 3. Zoom is semantic, width is physical

Borrowing directly from `painted`'s zoom philosophy:
- zoom controls how much detail the user asked to see
- terminal width controls how that detail is laid out or wrapped

Narrow width should not silently reduce semantic detail.

### 4. Human output and machine output are separate products

- human-readable output uses `painted`
- machine-readable output remains JSON

We should not blur those paths.

### 5. Incremental migration beats rewrite

Each stage should be:
- shippable
- testable
- visually inspectable
- reversible if needed

## Current siftd rendering state

Today, siftd effectively has these layers:

- **Source/data layers**
  - DB-backed conversations
  - live peek adapters
  - follow-mode parsed events
- **Narrative normalization**
  - `ConversationDetail` / `Turn` / `NarrativeBlock`
  - `PeekExchange` / peek narrative blocks
  - `FollowEvent`
- **Rendering**
  - CLI-specific print logic
  - `src/siftd/output/narrative.py` shared text-line renderer

This migration will replace the last layer while strengthening the middle one.

## Proposed target architecture

### Layer 1: Source backends

No painted dependency.

Examples:
- `query` API
- `peek` reader
- `follow` parser

### Layer 2: Renderable narrative model

No painted dependency.

Possible types:
- `RenderableConversation`
- `RenderableTurn`
- `RenderableBlock`
- `RenderableToolCall`
- `RenderableEvent`

These are normalized siftd-side data structures.

### Layer 3: Lenses / projections

This is where semantic view selection happens.

Conceptually:
- `(data, zoom, width) -> painted.Block`

Examples:
- conversation detail lens
- peek exchange lens
- follow event lens
- later: tool-trace lens, workspace-summary lens

### Layer 4: Delivery

- ANSI TTY -> `painted.print_block(...)`
- plain text -> `painted.print_block(..., use_ansi=False)` or equivalent plain output strategy
- JSON -> separate path, no painted dependency

## Dependency strategy

Decision made during implementation:
- `painted` is now a real dependency for siftd's human-readable UX
- siftd should depend on a released PyPI version, not a local path
- the minimum safe version for this migration is `painted>=0.1.2`

Why `0.1.2` matters:
- Stage 1 exposed an ANSI `print_block()` bug where trailing rectangular row padding could trigger terminal auto-wrap and visually appear as extra blank lines
- that bug was fixed upstream in `painted 0.1.2`
- siftd should rely on the upstream fix rather than carry a local workaround

We should continue to avoid long-term local-path coupling in project config.

## Proposed zoom model

Borrow the `painted` vocabulary directly.

### Internal zoom levels

- `MINIMAL`
- `SUMMARY`
- `DETAILED`
- `FULL`

### Initial CLI mapping

This mapping can evolve, but the first pass should be simple:

- list modes -> `MINIMAL`
- default detail -> `SUMMARY`
- `--thinking` -> `DETAILED`
- `--tools` -> `DETAILED`
- `--full` -> `FULL`

### Notes

- `--full` should imply thinking visibility and tool content
- width should affect wrapping/reflow, not zoom selection
- later, we may expose zoom more explicitly, but this migration does not require new public CLI flags yet

## Palette and semantic roles

We should adopt a semantic style vocabulary instead of hardcoded color decisions.

### Base roles from painted

Leverage the existing palette concepts:
- `accent`
- `muted`
- `success`
- `warning`
- `error`

### siftd-specific roles

Define a small role layer on top:
- `heading`
- `meta`
- `prompt`
- `assistant`
- `thinking`
- `tool`
- `tool_input`
- `tool_result`
- `tool_error`
- `summary_hint`

### Default behavior

- ANSI output should be restrained and readability-first
- plain output should preserve hierarchy through prefixes, spacing, and indentation
- JSON output ignores style entirely

### First-pass visual defaults

Suggested behavior:
- headings -> accent + bold
- metadata -> muted
- thinking -> muted/accent-adjacent, visually distinct from assistant text
- tool names -> accent
- tool inputs -> muted secondary text
- success results -> plain or success-toned
- error results -> error-toned

## Narrative block model and remixability

We have already started building narrative blocks in siftd. The next step is to make them more intentionally reusable.

### Requirement

The same narrative blocks should be easy to:
- render compactly
- render fully
- regroup by tool phase or semantic category
- feed into alternate lenses later
- eventually project into TUI panels or panes

### Proposed approach

Define stable siftd-side renderable block types first.

Candidate block categories:
- `text`
- `thinking`
- `tool_calls`
- `tool_result`
- `tool_output`
- `meta`
- `warning`
- `error`

Tool calls should remain structured, not flattened into strings too early.

Candidate tool fields:
- `tool_name`
- `status`
- `count`
- `input_summary`
- `result_summary`
- `raw_input` (optional)
- `raw_result` (optional)
- `tags` / `kind` (future)

### Why this matters

If we keep renderable blocks structured until the lens layer, then:
- `query --full` and `peek --full` can use the same lens
- `peek --follow` can render event blocks consistently
- future tool-specific presenters can compose into richer painted blocks

## Tool rendering strategy

Tool output is one of the biggest UX pain points and one of the highest-ROI migration targets.

### First-pass generic presenter

All tools should render through a generic structured presenter:
- tool header line
- input line(s)
- result line(s)
- status treatment

### Tool-specific presenters (staged)

Priority order:
1. `shell.execute`
2. `file.read`
3. `file.edit`
4. `file.write`
5. `search.grep`
6. `ui.todo`

Examples:

#### `shell.execute`
- command
- cwd if available
- exit code
- concise stdout/stderr preview
- de-emphasize chunk metadata

#### `ui.todo`
- title
- checklist of steps
- status transitions where available

#### `file.read`
- path
- line range if available
- concise result summary

These presenters should produce structured painted blocks, not raw strings.

## Command migration plan

### Stage 0 — Dependency + seam ✅ complete

Add `painted` and create the bridge layer.

Delivered:
- dependency update
- `siftd.output.painted_bridge`
- internal zoom/fidelity module
- initial semantic role mapping
- upstream dependency floor set to `painted>=0.1.2`

### Stage 1 — Query detail migration ✅ complete

Migrate `query <id>` and `query <id> --full` first.

Why first:
- static
- easiest to snapshot-test
- richest normalized data

Delivered:
- `query <id>` prompt/response rendering through painted blocks
- `--full` mapped onto true full zoom semantics
- improved narrative hierarchy
- upstream fix in `painted 0.1.2` to prevent ANSI trailing-padding auto-wrap artifacts

### Stage 2 — Peek detail migration ✅ complete

Migrate `peek <id>` and `peek <id> --full` onto the same painted-backed projection family.

Delivered:
- visual alignment with query
- same semantics for thinking/tools/fullness
- local-time human timestamps in the painted detail views
- backend differences kept to available data only

### Stage 3 — Follow static painted migration

Migrate `peek --follow` to render painted blocks statically in the stream.

Important: do **not** adopt in-place live rendering yet.

Deliverables:
- event-by-event painted output
- same zoom semantics as static detail views
- significantly improved trace readability

### Stage 4 — Tool-specific components

Layer richer tool presenters on top of the migration.

Deliverables:
- shell/file/todo-specific visuals
- much better `--tools` and `--full`

### Stage 5 — Lens architecture cleanup

Once the major commands are migrated, formalize the lens/projection layer.

Deliverables:
- stable siftd-side renderable types
- explicit lens functions
- CLI becomes thinner and mostly selects source + zoom + format + lens

## UX testing plan

Each stage should include snapshot and manual review.

### Snapshot coverage

We should add/expand snapshots for:
- `query <id>`
- `query <id> --full`
- `peek <id>`
- `peek <id> --full`
- representative follow event sequences

### Manual review fixtures

Maintain a small set of representative sessions:
1. tool-heavy orchestration
2. long prose-heavy conversation
3. thinking-heavy session
4. file-edit-heavy coding session
5. shell-heavy debug trace
6. mixed adapters

### Review dimensions

For each stage, evaluate:
- ANSI TTY output
- plain/piped output
- narrow terminal
- wide terminal
- compact/default/full
- ingest-backed vs live-peek-backed consistency

### Required manual gate

One lesson from Stage 1: captured output alone is not enough.

For every future migration stage, we should do at least one real TTY pass that specifically checks for:
- last-column wrapping artifacts
- unexpected blank lines from rectangular padding
- ANSI/plain divergence
- narrow-width hierarchy regressions

## Risks and mitigations

### Risk: startup overhead

Mitigation:
- keep JSON path independent
- rely on painted's lazy import behavior
- measure startup before and after Stage 1

### Risk: renderer overreach

Mitigation:
- migrate static output before live/TUI features
- keep stages small and shippable

### Risk: view inconsistency during migration

Mitigation:
- start with shared semantic roles and zoom mapping
- prefer one painted bridge layer over per-command experiments

### Risk: painted concerns leaking into core models

Mitigation:
- keep painted imports in output/delivery layers only
- retain siftd-native renderable models

### Risk: terminal-behavior bugs hiding in apparently-correct output

Stage 1 exposed a class of bug where the text content was technically correct, but ANSI delivery at the terminal boundary was not.

Mitigation:
- validate at least one real TTY path per stage
- prefer upstream fixes when the issue belongs in `painted`
- avoid carrying bridge-specific delivery hacks longer than necessary

## Open questions

Resolved so far:
1. `painted` should be a hard dependency immediately, via PyPI, with `painted>=0.1.2`

Still open:
2. Do we want to preserve current CLI flags as the long-term public interface, or eventually expose zoom more directly?
3. How much ANSI styling should default output use before it feels noisy?
4. Should plain output use the same structural hierarchy with simpler glyphs, or a separately tuned compact plain renderer?
5. When we reach live mode later, should follow stay scroll-based by default, with in-place as an opt-in mode?
6. Should `--thinking` imply less truncation by default, since hidden reasoning is often the whole point of that flag?
7. Should detail verbosity eventually map onto explicit zoom controls, or should `-v` / `-vv` gain meaning in detail views instead of remaining primarily list-output flags?

## Immediate next steps

1. Migrate `peek --follow` onto painted-backed static event rendering without adopting in-place live updates yet
2. Reuse the current bridge/zoom/semantic role work instead of forking a separate follow renderer
3. Add stronger regression coverage and a repeatable real-TTY review checklist for future stages
4. Keep refining tool presenters now that `query` and `peek` detail share the same projection family
5. Decide how explicitly we want to surface zoom semantics in the public CLI once follow lands
6. Revisit detail truncation/verbosity semantics after Stage 3, especially:
   - whether `--thinking` should default to less truncation or no truncation
   - whether detail views should get a first-class verbosity/zoom control instead of today’s list-focused `-v` behavior

## Quick manual verification commands

Use a known ingested conversation ID and a known live session ID.

Replace:
- `<conv_id>` with a real `siftd query` conversation ID
- `<session_id>` with a real `siftd peek` session ID

### Query detail

```bash
uv run siftd query <conv_id>
uv run siftd query <conv_id> --thinking
uv run siftd query <conv_id> --tools
uv run siftd query <conv_id> --full
```

What you should see:
- the detail header rendered in the painted-backed style
- timestamps shown in **local time**, not raw stored UTC text
- default detail shows the core prompt/response narrative
- `--thinking` shows thinking blocks inline without automatically expanding tool payloads
- `--tools` shows tool inputs/results inline
- `--full` shows the fullest version: no truncation, with thinking + tool detail

### Peek detail

```bash
uv run siftd peek <session_id>
uv run siftd peek <session_id> --thinking
uv run siftd peek <session_id> --tools
uv run siftd peek <session_id> --full
```

What you should see:
- the same overall visual family as `query <conv_id>`
- a painted-backed session header (`Session`, `Workspace`, `Started`, `Model`, `Adapter`, `File`)
- local-time timestamps in the header and per-exchange labels
- default detail shows prompt/response with tool summary only
- `--thinking` shows thinking blocks inline without automatically expanding tool inputs/results
- `--tools` shows tool inputs/results inline when available
- `--full` shows the fullest available session detail without truncation

### Follow behavior check

```bash
uv run siftd peek <session_id> --follow
uv run siftd peek <session_id> --follow --thinking
uv run siftd peek <session_id> --follow --tools
```

What you should see right now:
- follow is **not** painted yet; that is the next stage
- but the detailed/detail-ish behavior should still be more aligned with the new zoom semantics
- `--thinking` should surface thinking blocks when available
- `--tools` should push follow into the richer narrative rendering path when tool narrative data exists

### TTY-specific checks

Run at least one of the above directly in a real terminal, not only piped/captured.

What to specifically check:
- no spurious blank lines
- no right-edge wrap artifacts
- the same semantic hierarchy in ANSI and plain output
- local timestamps that match your machine timezone expectations

## Success criteria

We should consider this migration successful when:
- `query`, `peek`, and `peek --follow` feel like the same UX family
- `--full` has a clear and consistent meaning everywhere
- tool-heavy sessions are readable without raw JSON noise
- thinking/tool/result hierarchy is visually obvious
- the rendering architecture is lens-friendly and future-TUI-friendly
- JSON output remains clean and unaffected
