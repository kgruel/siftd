# Roadmap: tool-oriented retrieval and queryability

## Summary

siftd already stores rich tool-call data (`tool_calls`, `tools`, `content_blobs`, conversation/workspace metadata), but it does not yet expose a strong retrieval surface for tool-oriented questions.

Recent findability experiments showed:

- Embedding tool summaries improved semantic recall for tool-usage queries.
- FTS5 over tool metadata performed substantially better than semantic search for exact-ish operational queries.
- Semantic reranking did **not** help when FTS5 already had strong candidates; for tool-oriented queries, FTS5 ranking was the right ranking.

This shifts the problem from "how do we embed tool content?" to:

1. **How do we let users express tool-oriented queries well?**
2. **How do we project existing stored data into a good searchable/indexed surface?**

## Problem statement

Today, users can often answer conceptual questions with text search or embeddings, but operational questions are awkward or impossible to express reliably:

- Which conversations read `pyproject.toml`?
- Which sessions had failed shell commands?
- Where did we grep for `journal_mode`?
- Which conversations edited `config.py`?
- Which sessions used `file.write` repeatedly?

These are not purely semantic questions. They are a mix of:

- structured filters
- exact-ish text lookup
- ranking within a narrowed tool-specific candidate set

## Conclusion from experiments

For tool-oriented retrieval, the right default is:

- **FTS5-first** for lexical/tool/path/command lookup
- **structured filters** for precise intent
- **semantic search only as fallback/augmentation**, not as the primary ranking path

This suggests two concrete product areas to build:

1. a **tool query language**
2. a **tool-search projection/index** derived from existing DB state

---

## Part 1: Tool query language

### Goals

The query language should:

- be small and easy to type
- support exact filters and fuzzy text together
- degrade gracefully into plain search
- avoid requiring users to know SQL or schema details
- be composable with existing siftd filters (workspace, dates, tags, model, etc.)

### Proposed syntax

Use a light fielded-query format:

```text
field:value bare terms here
```

Where:

- **fielded terms** become hard filters or targeted matches
- **bare terms** become FTS ranking terms

### Initial fields

#### Tool identity
- `tool:` tool name, e.g. `tool:file.read`, `tool:shell.execute`
- `tool_family:` optional normalized family, e.g. `tool_family:file`, `tool_family:shell`

#### Tool outcome
- `status:` `success`, `error`, `timeout`, etc.

#### File-oriented inputs
- `path:` full path or partial path
- `basename:` basename only, e.g. `basename:pyproject.toml`
- `ext:` file extension if derived, e.g. `ext:py`

#### Command/search inputs
- `cmd:` shell command text or command token
- `pattern:` grep/search pattern
- `arg:` generic raw input text fallback

#### Output/result
- `result:` result snippet text
- `result_status:` optional alias for tool outcome if useful

#### Existing siftd dimensions
- `workspace:`
- `tag:`
- `model:`
- `since:`
- `before:`
- `provider:`
- `harness:`

### Example queries

```text
tool:file.read path:pyproject.toml
status:error tool:shell.execute
cmd:git tool:shell.execute
pattern:journal_mode tool:search.grep
workspace:siftd tool:file.edit basename:config.py
status:error docker compose
```

### Semantics

Recommended initial semantics:

- repeated different fields → AND
- repeated same field → OR (at least initially)
- bare terms → FTS ranking terms

Example:

```text
tool:shell.execute status:error git
```

Means:

- filter to `tool_name = shell.execute`
- filter to `status = error`
- rank the remaining candidates using FTS against `git`

### Fallback behavior

Plain free-text queries with no fields should still work.

Examples:

- `git commands`
- `pyproject.toml`
- `bash errors`
- `grep journal_mode`

These can use the same FTS5 infrastructure without requiring structured syntax.

### Non-goals for v1

- full boolean grammar
- nested expressions
- user-visible SQL
- semantic query rewriting

A tiny and reliable query language is better than a clever but ambiguous one.

---

## Part 2: Tool-search projection/index

### Why a projection is needed

The raw data already exists, but querying it directly is inconvenient because a search path would otherwise need to:

- join `tool_calls` to `tools`
- parse JSON inputs live
- derive basenames/command tokens on demand
- optionally chase `result_hash` into `content_blobs`
- normalize statuses/names every query

That pushes too much work into query-time logic.

Instead, build a **derived projection** for tool-oriented retrieval.

### Proposed logical schema

One projected row per tool call:

- `tool_call_id`
- `conversation_id`
- `response_id`
- `timestamp`
- `tool_name`
- `tool_family`
- `tool_description`
- `status`
- `path`
- `basename`
- `ext`
- `command`
- `command_verb`
- `pattern`
- `description`
- `result_snippet`
- `workspace_id` / `workspace_path`
- `search_text`

Not every field needs to be materialized on day one, but this is the general shape.

### `search_text`

Build an FTS-oriented text field from normalized pieces, for example:

```text
shell execute Execute shell commands
command git status
description Check repo state
status success
workspace /Users/kaygee/Code/siftd
```

For file tools:

```text
file read Read file contents
path /Users/kaygee/Code/siftd/pyproject.toml
basename pyproject.toml
ext toml
status success
```

### Indexing guidance

Use FTS5 over normalized textual fields such as:

- tool name tokens (`file.read` → `file read`)
- tool description
- command text
- file path
- basename
- pattern text
- status text
- selected result snippet text

Likely helpful normalizations:

- Porter stemming
- tokenization of dotted tool names
- path basename extraction
- command verb extraction (`git`, `grep`, `docker`, etc.)
- optional lowercased duplicate tokens for easier matching

### Result snippets

Result content should be used carefully.

Good candidates:

- short error snippets
- first line / short prefix of outputs
- normalized stderr-ish content where available

Avoid indexing arbitrarily huge raw outputs in the main tool FTS surface.

### Grouping model

Start with **one row per tool call**.

Why:

- easier to explain
- finer-grained matches
- easier to debug ranking
- avoids over-concatenated BM25 documents

Grouping to conversation level can be done at presentation/ranking time.

---

## Retrieval architecture

### Recommended pipeline

1. **Parse query** into:
   - structured filters
   - free-text terms
2. **Apply SQL filters** over the projected tool rows
3. **Run FTS5** within that filtered set (or globally if no filters)
4. **Group results** by conversation/tool call for display
5. **Optionally fall back to semantic** only when:
   - FTS5 returns too few results
   - the query is conceptual rather than operational

### Routing heuristics

Use FTS5-first when the query is dominated by:

- tool names
- file names/paths
- commands
- grep patterns
- statuses/errors
- identifiers

Use semantic search when the query is dominated by:

- concepts
- design intent
- tradeoffs
- abstract reasoning

Mixed queries can use:

- structured filters + FTS first
- semantic only as fallback

---

## Implementation roadmap

### Phase 1: Query language MVP

- [x] define field list and syntax rules
- [x] parse `field:value` tokens + bare terms
- [x] produce a structured intermediate representation
- [x] document query behavior and examples

Status:
- Implemented in `src/siftd/tool_query.py` as a small execution-agnostic parser.
- Covered by `tests/test_tool_query.py`.
- Current scope is parsing/IR only; query execution still belongs to Phase 3.

Deliverable:
- parser output usable by CLI/API search paths

### Phase 2: Tool-search projection MVP

- [x] define derived schema from existing DB tables
- [x] materialize normalized fields from `tool_calls` + `tools`
- [x] include command/path/basename/pattern extraction
- [x] create FTS5 index for tool-oriented search text

Status:
- Implemented in `src/siftd/storage/tool_search.py`.
- Creates `tool_search` and `tool_search_fts` derived surfaces.
- Current extracted fields: tool name/family, status, path/basename/ext, command/verb, pattern, arg, result snippet, workspace path, and normalized `search_text`.
- Rebuild entrypoint is `rebuild_tool_search_index(conn)`; execution/routing is still pending in Phase 3.

Deliverable:
- a fast searchable tool-oriented index/projection

### Phase 3: Query execution

- [ ] map structured fields to SQL filters
- [ ] map bare terms to FTS5
- [ ] support grouped presentation by conversation and tool call
- [ ] expose through CLI/API

Deliverable:
- working user-facing tool query/search behavior

### Phase 4: Fallback / augmentation

- [ ] add semantic fallback only for underfilled or conceptual queries
- [ ] add heuristics for routing between operational vs conceptual retrieval
- [ ] benchmark against current findability suite

Deliverable:
- unified search behavior without forcing embeddings onto lexical tasks

---

## Open questions

- Should the projection be persisted in the main DB or generated into a sidecar index?
- How much result text should be indexed before noise outweighs value?
- Should `tool:` support aliases (`read` → `file.read`, `bash` → `shell.execute`)?
- Should grouped ranking happen at tool-call level first, then collapse to conversation level?
- How should tool-oriented queries interact with existing conversation text FTS?
- Do we want a single search command with routing, or distinct `search` vs `tool-search` entry points?

---

## Recommendation

Build the **query language** and the **tool-search projection** first.

That is the highest-leverage path because:

- the data already exists
- the experiments show FTS5 is the right core mechanism
- the missing capability is not storage, but queryability and access
- semantic search should augment this later, not define it
