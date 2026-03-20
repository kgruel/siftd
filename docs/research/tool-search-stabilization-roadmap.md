# Roadmap: tool-search stabilization before exploratory analytics

## Why this exists

The initial tool-search implementation is now functional:

- tool-oriented query parsing exists
- a derived `tool_search` / `tool_search_fts` projection exists
- `siftd tool-search` is usable on the full DB
- common siftd filter concepts now work both as flags and inline fields

That is enough to begin exploratory use, but recent stress-testing against the full siftd database showed several rough edges that should be tightened before we rely on tool-search as an analytical instrument.

This roadmap is about **stabilization and semantic cleanup**, not adding a large new feature surface.

---

## Current state

Working now:

- fielded tool queries: `tool:`, `path:`, `cmd:`, `pattern:`, `status:`, etc.
- grouped tool-search presentation
- compact default display
- safer FTS quoting for bare terms
- tool alias normalization at query time
- shared filter concepts available as both:
  - CLI flags (`--since`, `--workspace`, `-l`, `-t`, etc.)
  - inline fields (`since:`, `workspace:`, `tag:`, `tool:`, etc.)

Observed from full-DB exploration:

- the projection already reveals real operational patterns
- the query surface is useful for file/command/error analysis
- some semantic and compatibility edges remain fuzzy
- some presentation heuristics still need refinement

---

## Problems to solve before deeper analytics

### 1. Compatibility and semantic duplication

There are still places where the same concept exists in more than one implementation path:

- date parsing behavior
- common filter naming
- field alias normalization
- merge semantics between flags and inline fields

Even if behavior currently lines up, we should reduce ambiguity so later analytical work is built on one coherent contract.

### 2. Tool normalization is still incomplete

Full-DB inspection showed real use of raw or harness-specific tool names such as:

- `bash`
- `read`
- `edit`
- `write`
- `run_experiment`
- `log_experiment`

This means query-time alias handling is useful, but we need a clearer policy for:

- raw tool name preservation
- canonical tool matching
- whether more normalization should happen in the projection itself

### 3. FTS robustness is better, but not settled

The current quoted-term approach fixed several real-world failures, especially shell-like inputs such as:

- `./dev`
- `pyproject.toml`

But we still need confidence that the FTS behavior is:

- predictable
- robust to punctuation-heavy terms
- sensible for command-like queries
- aligned with user expectations for ranking

### 4. Presentation is serviceable, not final

Compact grouped output is much better than the first pass, but full-DB browsing still shows areas to improve:

- path compaction can still be awkward for some repo-root files
- repeated matches within a conversation may need collapsing or summarization
- tool-specific subject/snippet heuristics are uneven
- `search.grep` output is less interpretable than file or shell output

### 5. We need a clearer readiness bar before analytics

Before we start adding analytical queries or reports, we should know that:

- query semantics are stable
- input forms are consistent
- edge cases don’t cause misleading results
- output is readable enough for interactive investigation

---

## Stabilization roadmap

## Phase A: Canonical filter semantics

Goal: one coherent filter model across flags and inline query fields.

### Tasks

- [ ] define canonical internal field names for common filters
- [ ] document accepted inline aliases
- [ ] document merge semantics explicitly:
  - repeated same field = OR
  - different fields = AND
  - flags + inline fields accumulate into the same field set
- [ ] add tests proving flag-only, inline-only, and mixed forms are equivalent

### Deliverable

A single documented filter contract that applies across tool-search inputs.

---

## Phase B: Shared date normalization cleanup

Goal: eliminate ambiguity around date parsing behavior.

### Tasks

- [ ] choose one canonical date parsing implementation
- [ ] keep compatibility wrappers only where necessary
- [ ] ensure inline `since:` / `before:` use the same semantics as flags
- [ ] decide behavior for invalid inline dates:
  - fail hard
  - or preserve as raw text
- [ ] add explicit tests for relative inline dates (`7d`, `1w`, `today`, `yesterday`)

### Deliverable

A stable date normalization contract used everywhere relevant.

---

## Phase C: Tool normalization policy

Goal: make tool-oriented retrieval resilient across harnesses without hiding raw data.

### Tasks

- [x] inventory raw vs canonical tool names from the full DB
- [x] expand alias coverage based on observed usage
- [ ] define policy for:
  - raw tool name matching
  - canonical tool matching
  - display of raw vs canonical names
- [ ] decide whether projection-time normalization should supplement query-time aliases
- [ ] add tests for mixed raw/canonical retrieval behavior

### Observed inventory snapshot

From the current full DB, the dominant canonical tool names in `tool_search` are still:

- `shell.execute`
- `file.read`
- `file.edit`
- `search.grep`
- `ui.todo`
- `file.glob`
- `file.write`

But there is also a meaningful long tail of raw or harness-specific names already present in the projection, including:

- `bash`
- `read`
- `edit`
- `write`
- `run_experiment`
- `log_experiment`
- `replace`
- `write_stdin`
- `google_web_search`
- `ToolSearch`
- `TaskCreate`
- `TaskUpdate`
- `TaskList`
- `SendMessage`

This supports the Phase C goal directly: canonical queries need to retrieve both canonical rows and known raw-name rows without forcing us to rewrite historical provenance.

### Current policy direction

- preserve raw tool names in storage and display
- let `tool:` queries expand to canonical name plus known raw aliases
- prefer projection-time provenance preservation over projection-time rewriting
- only add projection-time normalization later if query-time expansion proves insufficient

Important nuance: not every raw tool name should be force-fit into the canonical taxonomy.

Examples that appear to be worth preserving as raw names unless and until we define a better cross-harness concept:

- `log_experiment`
- `init_experiment`
- `ToolSearch`
- `TaskUpdate`
- `TaskCreate`
- `SendMessage`

These are meaningful harness-level operations, but they are not obviously interchangeable with current canonical tool buckets like `shell.execute` or `file.read`.

So the working Phase C rule is:

- alias names that are clearly equivalent across harnesses
- preserve names that represent genuinely distinct harness capabilities
- do not collapse provenance-rich orchestration actions into misleading canonical buckets just to increase match counts

### Working classification table

| Observed raw name | Current treatment | Rationale |
| --- | --- | --- |
| `bash` | alias → `shell.execute` | clear shell execution equivalent |
| `run_experiment` | alias → `shell.execute` | operationally a shell-command execution surface |
| `read` / `Read` | alias → `file.read` | clear file-read equivalent |
| `view` / `view_image` | alias → `file.read` | retrieval-equivalent read/view action |
| `write` / `Write` | alias → `file.write` | clear file-write equivalent |
| `edit` / `Edit` / `replace` / `apply_patch` | alias → `file.edit` | edit/update file content |
| `glob` / `list_directory` / `list_files` | alias → `file.glob` | file listing / discovery equivalent |
| `search_file_content` / `search_files` / `Grep` | alias → `search.grep` | lexical content search equivalent |
| `google_web_search` / `WebSearch` | alias → `search.web` | web search equivalent |
| `web_fetch` / `WebFetch` | alias → `web.fetch` | URL fetch equivalent |
| `ask_user` / `AskUserQuestion` | alias → `ui.ask` | user-interaction equivalent |
| `task` / `Task` | alias → `task.spawn` | generic spawned subtask |
| `write_stdin` | alias → `shell.stdin` | shell input stream control |
| `log_experiment` | preserve raw | orchestration/analytics action, not shell/file/search |
| `init_experiment` | preserve raw | orchestration/session setup action |
| `ToolSearch` | preserve raw | higher-level retrieval action, not a base tool primitive |
| `TaskUpdate` / `TaskCreate` / `TaskList` / `TaskGet` | preserve raw for now | task-lifecycle semantics are richer than current canonical buckets |
| `SendMessage` | preserve raw | agent/harness communication primitive |
| `ExitPlanMode` / `EnterPlanMode` | preserve raw | mode transition, not a retrieval/edit/exec primitive |
| `Agent` | preserve raw | unclear subagent semantics; needs more evidence |
| `BashOutput` | undecided | may be shell-adjacent, but not obviously an execution primitive |
| `mcp__happy__change_title` | preserve raw | provider-/tool-specific action; provenance matters |
| `write_todos` | undecided | maybe `ui.todo`, but needs evidence before collapsing |

This table should be treated as an explicit policy surface, not just an implementation accident.
Future alias additions should update this table and add retrieval tests.

### Deliverable

A clear normalization policy that improves retrieval without erasing provenance.

---

## Phase D: FTS robustness and ranking review

Goal: make operational search reliably usable on real-world inputs.

### Tasks

- [x] stress-test FTS handling for punctuation-heavy terms
- [x] review behavior for command-like strings and path-like strings
- [x] define fallback behavior for malformed or low-signal bare-term queries
- [x] verify ranking quality for:
  - commands
  - paths
  - basenames
  - grep patterns
- [x] identify any cases where structured filtering should absorb more of the intent than FTS

### Current Phase D notes

The current implementation builds FTS queries by quoting each bare term individually.
That appears to be a good baseline for real operational input shapes such as:

- `./dev`
- `pyproject.toml`
- `git status`
- `pytest -k tool_search`
- `rg "tool_name" src/siftd`

This does not make ranking “solved,” but it does avoid a more serious failure mode:
punctuation-heavy operational queries causing FTS syntax errors or becoming effectively unusable.

Current practical guidance:

- use bare terms for lexical command/path lookup
- use structured fields when intent is clearly field-specific:
  - `path:pyproject.toml`
  - `cmd:pytest`
  - `pattern:foo`
  - `tool:shell.execute`
- treat structured filters as the preferred narrowing mechanism when a query is operational rather than conceptual

Current fallback policy for malformed or low-signal bare-term queries:

- punctuation-only bare terms are dropped before building the FTS query
- if all bare terms are dropped and structured filters remain, execute the structured filters without FTS
- if all bare terms are dropped and no structured filters remain, fall back to the ordinary non-FTS ordering (`timestamp DESC`)

This is intentionally conservative. It avoids FTS parser edge cases without pretending punctuation-only input carries strong lexical intent.

Observed ranking behavior now covered by tests:

- command-like lexical queries can rank the expected exact command first
- path-like and basename-like queries can surface the expected file/path row first
- grep-pattern terms can surface the expected `search.grep` row first
- mixed structured + bare queries still keep the structured narrowing while using FTS for ranking inside that narrowed set

Where structured filtering should absorb intent more aggressively than FTS:

- exact file lookup → prefer `path:` or `basename:`
- command lookup with clear tool intent → prefer `tool:shell.execute` and/or `cmd:`
- grep-like pattern lookup → prefer `tool:search.grep pattern:`
- raw tool retrieval → prefer `tool:` over hoping a bare term ranks the right tool family

### Deliverable

A better-understood and more predictable lexical retrieval layer.

---

## Phase E: Presentation refinement

Goal: make interactive browsing good enough for exploratory analysis.

### Tasks

- [x] improve path compaction heuristics further
- [x] consider collapsing duplicate path matches within a conversation
- [x] refine compact subject selection by tool family:
  - file tools
  - shell tools
  - grep/search tools
- [x] improve optional snippet behavior per tool family
- [x] evaluate whether grouped output should summarize repeated identical matches

### Current Phase E notes

The grouped formatter now trends in the right direction for exploratory browsing:

- path compaction preserves a bit more repo-root context instead of always trimming to the last 3 segments
- repeated identical grouped matches can be summarized with a count marker (for example `×2`)
- compact subjects are more tool-family aware:
  - file tools emphasize compacted paths
  - shell tools emphasize the command text
  - grep tools emphasize the pattern and path together
- optional snippets remain compact and normalized for quick scanning

This is still not “final UI design,” but it is a materially better default for interactive investigation than a flat per-row dump.

### Deliverable

Readable, high-signal default output for exploratory workflows.

---

## Phase F: Readiness check for exploratory analytics

Goal: know when the tool-search substrate is stable enough for serious data exploration.

### Checklist

- [ ] common filters behave equivalently as flags and inline fields
- [ ] inline dates are settled and tested
- [ ] alias coverage is sufficient for observed real DB usage
- [ ] FTS does not break on common shell/path queries
- [ ] grouped output is readable enough to support browsing
- [ ] major edge cases discovered in the DB stress test are understood

### Deliverable

A clear “ready to explore” threshold rather than drifting into analytics prematurely.

---

## Initial findings from full-DB stress testing

These findings justify the stabilization work:

- tool-search already surfaces meaningful operational hotspots
- file-oriented retrieval is clearly valuable (`pyproject.toml`, `HANDOFF.md`, `CLAUDE.md`, etc.)
- command and error patterns are easy to surface lexically
- some repos dominate tool activity and will be good analytical targets
- raw tool aliases are common enough that normalization matters
- one-off pathological patterns are discoverable, which is a strong sign
- the weakest remaining area is not storage, but consistency and retrieval ergonomics

---

## Implementation notes from the current codebase

Current behavior worth preserving or tightening deliberately:

- inline field parsing lives in `src/siftd/tool_query.py`
- CLI/common filter merge happens in `src/siftd/api/tool_search.py::_merge_cli_filters()`
- inline date normalization currently uses `parse_date()` in `normalize_field_value()`
- invalid inline dates are currently preserved as raw text rather than failing
- tool alias normalization currently happens only at query/CLI merge time, not in the projection
- repeated values within the same field already behave as OR in SQL generation
- different fields already behave as AND because clauses are accumulated into one `WHERE`
- bare terms are passed through quoted FTS5 term construction via `build_fts5_query()`

This means the roadmap is not starting from zero. Most of the work is now about making those behaviors explicit, better tested, and easier to reason about.

---

## Suggested execution order

The phases above are logically separate, but the lowest-risk implementation order is:

1. **Phase A + B together**
   - settle canonical field names and date semantics first
   - this gives the search surface a stable contract before we adjust aliases or output
2. **Phase C**
   - expand observed tool aliases only after the filter contract is fixed
   - avoid mixing “what field means what” with “what names map to what”
3. **Phase D**
   - once parsing and normalization are stable, stress FTS and ranking on real query forms
4. **Phase E**
   - refine presentation after we trust result semantics
5. **Phase F**
   - do a deliberate readiness pass rather than letting “seems good enough” decide

---

## Concrete acceptance criteria by phase

### Phase A acceptance criteria

- a single table in docs defines canonical field names and accepted aliases
- tests show these are equivalent:
  - `-w siftd`
  - `workspace:siftd`
  - mixed `-w siftd workspace:other` producing OR within `workspace`
- tests show different fields remain ANDed
- unknown inline fields continue to be captured separately and do not silently affect SQL behavior

### Phase B acceptance criteria

- one documented answer exists for invalid inline dates
- inline and flag-based date inputs normalize through the same code path or equivalent wrapper
- tests cover at least:
  - absolute dates
  - `7d`
  - `1w`
  - `today`
  - `yesterday`
  - invalid values
- result filtering semantics are clear for multiple `since:` or `before:` values, even if we later decide to narrow that behavior

### Phase C acceptance criteria

- an observed inventory of raw tool names from the real DB is recorded in this doc or a follow-up note
- alias coverage includes currently observed high-frequency raw names
- tests verify retrieval across mixed naming forms, e.g. raw `bash`/canonical `shell.execute`
- raw provenance remains inspectable in the underlying data model even if retrieval uses canonical matching

### Phase D acceptance criteria

- no FTS syntax failures for common path-like or shell-like bare terms
- ranking examples have been spot-checked for:
  - `./dev`
  - `pyproject.toml`
  - `git status`
  - grep-like patterns
- at least one fallback policy is documented for malformed or low-signal bare-term queries
- we can explain when users should prefer structured fields over lexical bare terms

### Phase E acceptance criteria

- grouped output remains the default and is still legible on multi-hit conversations
- path compaction does not hide important repo-root distinctions
- `search.grep` results are interpretable without needing raw JSON inspection
- snippet display rules are predictable enough to describe in help text

### Phase F acceptance criteria

- the readiness checklist is evaluated explicitly after a full-DB stress pass
- any known unresolved edge cases are listed, not merely implied
- exploratory analytics can begin with a short note describing what is trusted and what is still provisional

---

## Immediate next actions

Recommended next implementation slice:

1. document the canonical filter contract in the tool-search help or reference docs
2. add equivalence tests for flag-only vs inline-only vs mixed common filters
3. decide and document invalid inline date handling
4. inventory actual raw tool names from the DB before changing normalization policy
5. run a focused FTS query corpus against punctuation-heavy operational terms

That sequence should reduce ambiguity quickly without prematurely changing the projection schema.

---

## Recommendation

Complete the stabilization phases above before committing to a larger analytical query surface.

Reason:

- the retrieval substrate is already promising
- the next highest-leverage work is semantic cleanup and robustness
- analytics built on inconsistent semantics will be harder to trust
- once the filter/query model is coherent, exploratory analytics will be much easier to evaluate honestly
