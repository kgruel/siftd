# Extension Points and Formatter Architecture

## Status

Planning. Branch: `feat/extension-points` (not yet created).

## Why this exists

siftd has two discoverable module systems — adapters (input) and formatters (output, about to be built) — that share the same structural pattern: versioned Python modules in a known directory, discovered at runtime, copyable from builtins for customization. Today the adapter machinery is bespoke. Building formatters would duplicate it.

This plan extracts the shared pattern into a general extension point system, migrates adapters onto it, and builds the formatter interface on top. The formatter work then enables the output unification: a narrative walker with shared decision logic, format-agnostic rendering, and universal Fidelity controls.

## Design principles

- **One rendering decision, multiple targets.** The question "should thinking show?" is answered once. How it looks in a terminal vs markdown vs JSON is the formatter's job.
- **Formatters are extension points.** Built-in terminal, markdown, JSON formatters ship with siftd. Users can copy and customize them, or write new ones (HTML, Rich, etc.).
- **Adapters and formatters share infrastructure.** Discovery, registration, versioning, and `siftd copy` work identically for both.
- **Fidelity is universal.** `--brief`/`--full`/`--thinking`/`--tools` work the same across all commands. The formatter interprets what "brief" means for its output medium.

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

def render_search(results: list, fidelity: Fidelity, **context) -> str:
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

```python
def fidelity_from_args(args, formatter=None) -> Fidelity:
    """Build Fidelity from standard CLI flags.

    The formatter provides defaults (brief_chars) for its output medium.
    CLI flags override formatter defaults.
    """
```

ExportOptions dissolves — replaced by Fidelity + format selection.

### Conversation list component

One rendering function per formatter for list views. Parameterized by:
- Context label ("Query results", "Active sessions", "Tagged: review")
- Palette/style for differentiation
- Fidelity for density (brief = ID + workspace + age, default = adds model/tokens, full = adds tags)

The peek list stays separate — SessionInfo is a genuinely different shape from ConversationSummary.

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
| `query --summary` bypass | Formatter handles metadata header via render_detail |
| Tag drill-down list duplication | Shared `render_list` in formatter |
| Search `select_formatter()` | General formatter selection (but search formatters may stay as-is initially) |

## Migration sequence

### Stage 1: Extension point infrastructure

Extract shared discovery/registration/copy/versioning from existing adapter code into a general module.

- New: `src/siftd/extensions.py` — discovery, registration, copy base
- Refactor: `src/siftd/api/adapters.py` — delegate to extensions
- Refactor: `src/siftd/api/resources.py` — `siftd copy` gains formatter support
- Tests: adapter discovery/copy behavior preserved

### Stage 2: Formatter interface + built-in formatters (stubs)

Define the formatter protocol and register the three built-ins.

- New: `src/siftd/output/formatter.py` — interface definition, selection logic
- New: `src/siftd/output/terminal.py` — stub wrapping current painted_bridge
- New: `src/siftd/output/markdown.py` — stub wrapping current export markdown
- New: `src/siftd/output/json_fmt.py` — stub wrapping current export JSON
- Formatter discovery wired through extension point system

### Stage 3: Narrative walker + emitters

Build the shared decision engine and wire it into formatters.

- New: `src/siftd/output/narrative.py` — `NarrativeEmitter` protocol, `walk_narrative()`
- Refactor: terminal formatter implements `TerminalEmitter`
- Refactor: markdown formatter implements `MarkdownEmitter`
- Refactor: json formatter implements `JsonEmitter`
- `render_narrative_lines` and `_render_narrative_md` delegate to walker
- Tests: existing test_tool_presenters tests pass unchanged

### Stage 4: Fidelity unification

- New: `fidelity_from_args()` in `src/siftd/cli_common.py`
- Refactor: cli_query, cli_peek, cli_export use `fidelity_from_args()`
- Dissolve: `ExportOptions` replaced by `Fidelity` + format selection
- `--brief`/`--full`/`--thinking`/`--tools` work uniformly across commands

### Stage 5: List views

- Add `render_list` to each formatter
- Refactor: `cli_query` list mode, `cli_tags` drill-down use formatter
- Parameterize by context (header, palette)
- Fidelity controls density

### Stage 6: Unify tool summary functions

- Single function replacing 3 variants in painted_bridge
- Input normalized to `(name, count, status)` tuples

### Stage 7: Search formatter bridge (optional)

- Evaluate whether existing search formatters can be expressed as formatters
- If natural: migrate. If forced: leave as-is with a note.

## What stays as-is

- **Peek list rows** — SessionInfo is a different shape. Not a false unification target.
- **`print_table`** — used for verbose/SQL output. Plain text table is fine.
- **Ingest renderer** — streaming progress, different concern entirely. Tier 3.
- **db/config/install output** — single-line confirmations. Not worth abstracting.

## Risks

- **Walker must handle two block types** — NarrativeBlock (DB) and PeekNarrativeBlock (disk). Duck typing is intentional; don't try to unify the types.
- **Tool presenters are format-specific** — The terminal emitter calls painted presenters; markdown/json emitters use simpler representations. The walker calls the emitter, the emitter picks the presenter.
- **Formatter discovery at import time** — Must be lazy to avoid import overhead for simple commands. Follow the adapter pattern: discover on first use.

## Version

This is a 0.6.0 release. Scope: extension point system, formatter infrastructure, output unification, universal Fidelity controls.
