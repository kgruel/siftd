# Extension Points and Formatter Architecture

## Status

In progress. Branch: `feat/extension-points` (6 commits ahead of main at v0.5.2).

All 9 stages complete. Ready for 0.6.0 release.

## Why this exists

siftd has two discoverable module systems — adapters (input) and formatters (output) — that share the same structural pattern: versioned Python modules in a known directory, discovered at runtime, copyable from builtins for customization. Today the adapter machinery is bespoke. Building formatters would duplicate it.

This plan extracts the shared pattern into a general extension point system, migrates adapters onto it, and builds the formatter interface on top. The formatter work then enables the output unification: a narrative walker with shared decision logic, format-agnostic rendering, and universal Fidelity controls.

## Design principles

- **One rendering decision, multiple targets.** The question "should thinking show?" is answered once. How it looks in a terminal vs markdown vs JSON is the formatter's job.
- **Formatters are extension points.** Built-in terminal, markdown, JSON formatters ship with siftd. Users can copy and customize them, or write new ones (HTML, Rich, etc.).
- **Adapters and formatters share infrastructure.** Discovery, registration, versioning, and `siftd copy` work identically for both.
- **Fidelity is universal.** `--brief`/`--full`/`--thinking`/`--tools` work the same across all commands. The formatter interprets what "brief" means for its output medium.
- **One formatter system.** All command output (detail, list, search) routes through the same output format modules. A custom HTML formatter can handle everything, not just detail views.

## Architecture

### Extension point system

An extension point is a category of discoverable Python modules with a versioned interface.

```
~/.config/siftd/
  adapters/          # input extension point
    my_tool.py       # ADAPTER_INTERFACE_VERSION = 1
  formatters/        # output extension point
    my_format.py     # FORMATTER_INTERFACE_VERSION = 1
```

Shared infrastructure handles:
- **Discovery**: scan directory, import modules, check version constant
- **Registration**: register by name, detect conflicts
- **Copy**: `siftd copy {type} {name}` copies builtin to user directory
- **Versioning**: `{TYPE}_INTERFACE_VERSION = N`, skip incompatible modules with warning

The type-specific parts (what methods a module must implement, how it's selected) stay in the adapter/formatter layer.

### Formatter interface

A formatter handles rendering for a specific output medium. It declares what it produces and implements rendering methods for each content type it supports.

```python
FORMATTER_INTERFACE_VERSION = 1

name = "markdown"
media_type = "text/markdown"  # "terminal", "text/markdown", "application/json", "text/html"
brief_chars = 300             # what --brief means for this format

def render_detail(turns: list, fidelity: Fidelity, **context) -> str:
    """Render conversation detail (narrative blocks)."""
    ...

def render_list(summaries: list, fidelity: Fidelity, **context) -> str:
    """Render conversation/session list rows."""
    ...

def render_search(results: SearchResults, fidelity: Fidelity, **context) -> str:
    """Render search results."""
    ...
```

Methods are optional — a formatter that only handles detail rendering simply omits `render_list` and `render_search`. The system falls back to the default formatter for content types the selected formatter doesn't handle.

Built-in formatters:
- **terminal** — painted Block/Line/Span output. Default when stdout is a TTY.
- **markdown** — GFM-compatible string output. Default for `siftd export` and file output.
- **json** — structured data dump. Selected via `--json` flag.

### Narrative walker

The walker is the shared decision engine. It iterates NarrativeBlock lists and calls format-agnostic callbacks based on Fidelity settings.

```python
class NarrativeEmitter(Protocol):
    def text(self, content: str) -> None: ...
    def thinking(self, content: str) -> None: ...
    def thinking_placeholder(self) -> None: ...
    def tool_summary(self, tools: list[tuple[str, int]]) -> None: ...
    def tool_content(self, name: str, input: str | None, result: str | None, status: str | None) -> None: ...
    def tool_output(self, content: str) -> None: ...

def walk_narrative(blocks: list, emitter: NarrativeEmitter, *, fidelity: Fidelity) -> None:
    """Walk narrative blocks, calling emitter methods based on fidelity.

    Owns all decisions: visibility gating, tool collapsing, truncation.
    Handles both NarrativeBlock and PeekNarrativeBlock via duck typing.
    """
```

Each built-in formatter provides its own emitter implementation:
- **TerminalEmitter** → `list[Line]` (uses existing tool presenters)
- **MarkdownEmitter** → `list[str]`
- **JsonEmitter** → `list[dict]`

### Fidelity unification

`fidelity_from_args(args)` replaces duplicated construction in cli_query and cli_peek. The formatter provides context-appropriate defaults (brief_chars).

ExportOptions dissolves — replaced by Fidelity + format selection.

### Conversation list component

One rendering function per formatter for list views. Parameterized by Fidelity for density (depth 0 = brief, depth 1 = default, depth 3 = full table with tags).

The peek list stays separate — SessionInfo is a genuinely different shape from ConversationSummary.

### Search result architecture

Search output separates two orthogonal concerns:

**Search views** (data processing — what to show):
- **chunks** (default): flat list of scored chunks
- **conversations**: aggregate by conversation, rank by max score
- **thread**: two-tier — high-scoring expanded, rest compact
- **context(n)**: ±N exchanges around each match
- **full-exchange**: fetch complete prompt+response from DB

**Output formats** (rendering — how to show it):
- terminal, markdown, json (and user-defined)

The flags `--conversations`, `--thread`, `--context N`, `--full` select a **view**. The output medium is selected by TTY detection / `--json` / `--format`. Each view produces a structured `SearchResults` object; the formatter's `render_search()` renders it.

```python
@dataclass
class SearchResults:
    """Processed search results ready for rendering."""
    query: str
    mode: str                           # "chunks", "conversations", "thread"
    chunks: list[SearchChunk]           # for chunks/verbose mode
    conversations: list[SearchConv]     # for conversations mode
    tiers: tuple[list, list] | None     # for thread mode (tier1, tier2)
```

This replaces the current six formatter classes (ChunkListFormatter, VerboseFormatter, FullExchangeFormatter, ContextFormatter, ThreadFormatter, ConversationFormatter) which each mix data processing with terminal-specific rendering.

## Dissolution inventory

| What dissolves | Into what |
|---|---|
| `ExportOptions` | `Fidelity` + format selection |
| Adapter-specific discovery/registration/copy | Shared extension point infrastructure |
| `_render_narrative_md` (export) | MarkdownEmitter via narrative walker |
| `render_narrative_lines` (painted_bridge) | TerminalEmitter via narrative walker |
| `_narrative_to_json` (export) | JsonEmitter via narrative walker |
| 3x tool summary functions (painted_bridge) | Single function, uniform `(name, count, status)` input |
| Fidelity construction in cli_query + cli_peek | `fidelity_from_args()` |
| Tag drill-down list duplication | Shared `render_list` in formatter |
| `formatters.py` (6 search formatter classes) | Search view processors + `render_search` on output formats |
| `registry.py` (search FormatterRegistry) | `format_registry.py` (unified) |
| `select_formatter()` | `select_format()` + view selection |
| `FormatterContext` | `SearchResults` + Fidelity |
| `_get_conversation_metadata()` in formatters.py | Moves to search view processors |

## Migration sequence

### Stage 1: Extension point infrastructure [DONE]

- Added `load_all_extensions()` to `plugin_discovery.py` — generic three-source discovery with dedup
- Refactored `adapters/registry.py` to delegate to `load_all_extensions()`
- Formatter validation updated: `FORMATTER_INTERFACE_VERSION`, `name`, `media_type`, `render_detail`

### Stage 2: Formatter interface + built-in formatters (stubs) [DONE]

- `output/format_registry.py` — `select_format()` with context-aware defaults
- `output/terminal_fmt.py` — stub wrapping painted_bridge
- `output/markdown_fmt.py` — stub (delegate pattern)
- `output/json_fmt.py` — stub (delegate pattern)
- Search formatter registry updated to accept new-style drop-in modules

### Stage 3: Narrative walker + emitters [DONE]

- `output/narrative.py` — `NarrativeEmitter` protocol, `walk_narrative()`, `MarkdownEmitter`, `JsonEmitter`
- Export's `_render_narrative_md` and `_narrative_to_json` delegate to walker
- ~150 lines of duplicated rendering logic removed from api/export.py
- `_options_to_fidelity` bridges ExportOptions → Fidelity temporarily

### Stage 4: Fidelity unification [DONE]

- `fidelity_from_args()` and `tool_chars_from_args()` in cli_common.py
- cli_query, cli_peek, cli_export all use shared construction
- ExportOptions still exists as bridge (dissolves when formatters are load-bearing)

### Stage 5: List views [DONE]

- `render_list()` added to terminal_fmt, markdown_fmt, json_fmt
- Fidelity depth controls column density: 0=brief (id/time/workspace), 1=default (adds model/turns/tokens/cost), 3=full (aligned table with tags)
- `--brief` now sets depth=0 (was depth=1); safe since depth is only checked as >=3 elsewhere
- `--verbose` in cli_query bumps depth to 3 (table output via formatter)
- cli_query and cli_tags drill-down both dispatch to formatter via `select_format()`
- Cost column unified: always shown at depth>=1 (tag drill-down previously omitted it)
- `format_table()` added to common.py (string-returning); `print_table()` delegates to it
- First case where formatters are load-bearing (for list rendering)

### Stage 6: Unify tool summary functions [DONE]

- Single `_tool_summary_lines(tools: list[tuple[str, int, str | None]])` replaces 3 variants
- Call sites normalize input: ToolCallSummary → tuple, peek (name, count) → (name, count, None), follow (name, count, hints) → (name, count, None)
- Status shown only when non-None; error status gets error styling

### Stage 7: Detail views through formatters [DONE]

- markdown_fmt.render_detail: real implementation with session header, turn loop, narrative walker
- json_fmt.render_detail: returns dict (caller serializes), uses JsonEmitter for narrative
- cli_query detail dispatches through `select_format()` → `render_detail()` — non-TTY gets markdown, TTY gets terminal
- cli_export dispatches through `select_format()` per conversation, joins results
- Dissolved: `ExportOptions`, `_options_to_fidelity`, `_render_narrative_md`, `_narrative_to_json`, `format_markdown`, `format_json`, `format_export` — all from api/export.py
- cli_peek detail stays as-is (peek-specific metadata, different detail view)

### Stage 8: Search result unification [DONE]

- Data processing extracted from search formatter classes into cli_search helpers:
  - `_fetch_search_metadata()` — enriches results with `_workspace`, `_started_at`
  - `_aggregate_conversations()` — groups by conversation, computes max/mean/chunk_count
  - `_compute_thread_tiers()` — splits into tier1 (above mean) and tier2
  - `_enrich_exchanges()` — fetches full prompt+response for --full mode
  - `_enrich_context()` — fetches +/-N surrounding exchanges for --context N
- `render_search(results, fidelity, **context)` added to terminal_fmt, markdown_fmt, json_fmt
  - All three handle chunks, conversations, and thread modes
  - Terminal: text output with score/metadata headers, truncation controlled by fidelity
  - Markdown: headers + sections for chunks, tables for conversations, sections for thread
  - JSON: returns dict (caller serializes), includes breakdown/file_refs
- cli_search dispatch refactored: fidelity_from_args + select_format → render_search
- FTS5-only path also unified: normalizes `side` → `chunk_type`, uses render_search
- Old formatter classes (`formatters.py`) and `registry.py` retained for backward compat
- `format_refs_annotation` and `print_refs_content` stay (post-processing, not formatter concern)

### Stage 9: Cleanup and copy command [DONE]

- `format_refs_annotation` and `print_refs_content` moved from formatters.py to common.py
- All 6 search formatter classes dissolved (ChunkListFormatter, VerboseFormatter, FullExchangeFormatter, ContextFormatter, ThreadFormatter, ConversationFormatter)
- `FormatterContext`, `select_formatter`, `OutputFormatter` protocol dissolved
- `FormatterRegistry` and `registry.py` dissolved
- `siftd copy formatter <name>` wired with `copy_formatter` API + `list_builtin_formatters`
- `doctor/checks.py` updated to use `format_registry.list_format_names()`
- Architecture tests updated to validate new format system
- `output/__init__.py` cleaned: only common.py utilities exported

## What stays as-is

- **Peek list rows** — SessionInfo is a different shape. Not a false unification target.
- **`print_table`** — used for verbose/SQL output. Plain text table is fine.
- **Ingest renderer** — streaming progress, different concern entirely.
- **db/config/install output** — single-line confirmations. Not worth abstracting.
- **`format_refs_annotation` / `print_refs_content`** — post-processing after search, not a formatter concern.

## Risks

- **Walker must handle two block types** — NarrativeBlock (DB) and PeekNarrativeBlock (disk). Duck typing is intentional; don't try to unify the types.
- **Tool presenters are format-specific** — The terminal emitter calls painted presenters; markdown/json emitters use simpler representations. The walker calls the emitter, the emitter picks the presenter.
- **Formatter discovery at import time** — Must be lazy to avoid import overhead for simple commands. Follow the adapter pattern: discover on first use.
- **Search view complexity** — ThreadFormatter's tiering and ContextFormatter's ±N window are non-trivial. The view processor extraction must preserve this logic intact. Test coverage for search formatters should be verified before refactoring.
- **Search imports are gated** — Search/embeddings is an optional extra. `render_search` must not pull in embeddings at import time.

## Version

This is a 0.6.0 release. Scope: extension point system, one unified formatter system for all output (detail, list, search), universal Fidelity controls.
