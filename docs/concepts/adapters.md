# Adapters

Every AI coding tool writes logs differently. Claude Code uses JSONL with one structure, Aider uses Markdown, Antigravity CLI uses a different JSONL schema. Adapters are the translation layer — they read these different formats and produce the common data model siftd understands.

## The problem adapters solve

Without adapters, you'd need separate tools for each coding assistant. Want to search across Claude Code and Aider sessions? You'd need to understand both log formats and write custom queries for each.

Adapters let siftd treat all your conversations uniformly. Once ingested, a conversation from Claude Code looks the same as one from Antigravity CLI — same fields, same query interface, same search.

## How adapters work

An adapter does three things:

1. **Discover** — find log files that belong to this tool
2. **Check** — determine if a specific file is parseable by this adapter
3. **Parse** — read the file and produce `Conversation` objects

```
~/.claude/projects/*/sessions/*.jsonl
         │
         ▼
    ┌─────────────┐
    │  discover   │  "I found 312 files"
    └─────────────┘
         │
         ▼
    ┌─────────────┐
    │  can_handle │  "Yes, this is my format"
    └─────────────┘
         │
         ▼
    ┌─────────────┐
    │    parse    │  Log file → Conversation object
    └─────────────┘
         │
         ▼
    Storage layer writes to SQLite
```

The parse step does the heavy lifting: it reads the raw log format, extracts prompts and responses, links tool calls to their results, determines timestamps, and builds the nested `Conversation → Prompt → Response → ToolCall` structure.

## What adapters normalize

Different tools call things different names. Adapters map these to canonical forms:

**Tool names:**
| Raw (varies by tool) | Canonical |
|---------------------|-----------|
| `Read`, `read_file`, `file_read` | `file.read` |
| `Bash`, `run_shell`, `execute` | `shell.execute` |
| `Write`, `write_file`, `create_file` | `file.write` |
| `Edit`, `apply_diff`, `str_replace` | `file.edit` |

**Timestamps:**
All converted to ISO 8601 format, sorted correctly regardless of source timezone.

**Content structure:**
Different tools represent prompts and responses differently — some as plain text, some as arrays of typed blocks (text, images, tool calls). Adapters normalize this into a consistent block structure.

This normalization is what makes cross-tool queries work. "Show me all shell commands" finds results whether the session was Claude Code, Aider, or Codex.

## Built-in adapters

siftd ships with adapters for:

| Adapter | Tool | Log location | Format |
|---------|------|--------------|--------|
| `claude_code` | Claude Code | `~/.claude/projects/`, `~/.config/claude/projects/` | JSONL |
| `aider` | Aider | `~/.aider/` | Markdown |
| `antigravity_cli` | Antigravity CLI | `~/.gemini/antigravity-cli/` | JSONL |
| `codex_cli` | Codex CLI | `~/.codex/sessions/` | JSONL |

Each adapter knows where its tool writes logs by default. When you run `siftd ingest`, all adapters scan their default locations.

### Support tiers

Every adapter carries a support tier that sets expectations for how actively its
log format is tracked:

- **core** — maintained by siftd; ingest is expected to work and upstream format
  changes are tracked (`claude_code`, `codex_cli`, `antigravity_cli`).
- **contrib** — best-effort; parse errors are possible when the tool's format
  drifts. This is the default for drop-in adapters.
- **frozen** — kept working as-is; upstream format changes may not be tracked
  (`aider`).

Tiers show up in the `siftd adapters` listing, and ingest tags file-error
warnings from non-core adapters with their tier.

```bash
siftd adapters    # list discovered adapters
```

```
claude_code      builtin  core    ~/.claude/projects, ~/.config/claude/projects
aider            builtin  frozen  ~/.aider
antigravity_cli  builtin  core    ~/.gemini/antigravity-cli
codex_cli        builtin  core    ~/.codex/sessions
```

The `gemini_cli` adapter was removed in 0.13.0: Gemini CLI became Antigravity
CLI, which `antigravity_cli` reads. Conversations it already stored stay in the
database, searchable as before; nothing new is read from `~/.gemini/tmp`.

### Re-parsing stored sources

A fix to an adapter reaches only the sources that change: ingest skips a source
whose stat and content hash match what was stored. To apply a parser fix to
conversations already in the database, re-parse them:

```sh
siftd ingest --reparse                 # every adapter
siftd ingest --reparse -a codex_cli    # one adapter (-a can repeat)
```

`--reparse` parses every stored source again, changed or not. Each conversation
goes through the same replacement as a changed file: it is deleted and stored
again under a new ID, and what a person attached to it moves across with it.
Conversation tags and ownership carry over whenever the new parse produces a
conversation. An event tag moves when the new parse produces an event with the
same kind and external ID.

A container that holds several copies of one session (a backup next to the live
file, say) keeps latest-wins: a reparse never replaces a stored session with an
older copy of it, whichever copy is processed first.

A replacement that would lose a user-applied tag is **skipped**, and the skip is
reported by conversation ID; the conversation stays as it was. Before deciding,
ingest counts what the replacement would not carry. The rule applies to every
replacement a reparse makes, including a legacy session marker, an orphaned
conversation whose bookkeeping pointer was lost, and a new file that adopts a
conversation stored under an older key. It also applies when a conversation that
another path shares gives up the tags its transcript owns to a new conversation:
those tags are counted the same way, and declining leaves them where they are.

- A conversation tag is never lost while the new parse produces a conversation.
  A parse that produces no conversation stores nothing to carry it onto, so its
  conversation tags count as lost.
- An event tag is lost when its event has no external ID, or the new parse no
  longer produces an event with that ID. Prompts and responses from Aider, Codex,
  Antigravity, pi and Copilot have no external ID today, so a tag on one cannot carry.
- A block tag is always lost: re-pointing block tags is not implemented yet.
- A tag the parser derives is not counted. `shell:<category>` on a `shell.execute`
  call is applied at ingest from the command, so the re-parse reproduces it. The
  name alone does not identify a derived tag, since a person can apply one by hand,
  so ingest re-runs the categorizer on the call: a `shell:` tag the command would
  not produce counts as user data.

`--force` replaces the skipped conversations anyway, and the tags counted above
are lost. The totals at the end of the run report how many sources were re-parsed,
how many conversations were replaced, and how many conversations were skipped for
tags. They print even under `--quiet`.

`--force` requires `--reparse`.

### Disabling an adapter

Any adapter can be turned off in config — useful when a frozen-tier adapter's
parse warnings are noise for a tool you no longer use:

```toml
# ~/.config/siftd/config.toml
[adapters.aider]
enabled = false
```

Or via the CLI:

```bash
siftd config set adapters.aider.enabled false
```

A disabled adapter is skipped everywhere the registry is consulted — ingest,
peek, and doctor checks. `siftd ingest` prints a skip notice so the omission
stays visible; already-ingested conversations remain in the database.

## Custom adapters

If you use a tool siftd doesn't support, you can write an adapter. Copy the template or an existing adapter:

```bash
siftd copy adapter template       # blank starting point
siftd copy adapter claude_code    # copy a built-in to modify
```

This creates a file in `~/.config/siftd/adapters/`. Edit it to:

1. Set `NAME` and `DEFAULT_LOCATIONS` for your tool
2. Implement `read()` (yield each raw unit of the log) and a `decoder` (turn each unit into canonical records); siftd derives `parse()` and peek from them
3. Map tool names to canonical forms in `TOOL_ALIASES`

Drop-in adapters are auto-discovered — just save the file and run `siftd ingest`.

See [Writing Adapters](../guides/writing-adapters.md) for the complete implementation guide.

## Deduplication

Adapters declare how they handle re-ingesting the same content:

**File-based deduplication** (`DEDUP_STRATEGY = "file"`):
One conversation per file. If the file hasn't changed, skip it; if it has, the conversation is replaced. Most tools work this way — each session gets its own log file. A parse that yields more than one conversation fails the source.

**Session-based deduplication** (`DEDUP_STRATEGY = "session"`):
Conversations are deduped independently by `external_id` rather than by the file that produced them. Use this when a source can hold several sessions or grow new ones over its life — a SQLite database of chats (OpenCode), or a markdown history file every session is appended to (aider).

The deduplication strategy ensures `siftd ingest` is idempotent — run it as often as you want without creating duplicate conversations.

## Adapter lifecycle

When you run `siftd ingest`:

1. All adapters are loaded (built-in + drop-in)
2. Each adapter's `discover()` finds candidate files
3. Files are checked against `ingested_files` table (skip if already processed and unchanged; `siftd ingest --reparse` re-parses them anyway, see [Re-parsing stored sources](#re-parsing-stored-sources))
4. For new/changed files, `can_handle()` confirms the adapter can parse it
5. `read` and `decoder` produce records, and the shared assembler turns them into `Conversation` objects (a version-1 adapter's `parse()` does this itself)
6. A decoding adapter's ledger counts every record; records it did not recognize or could not place are reported at the end of `siftd ingest`
7. Storage layer writes to SQLite, normalizes tool names, builds FTS index

```bash
siftd ingest -v    # verbose output shows per-adapter skip breakdowns
```

```
claude_code (312 files)
  1/312  new     myapp  Fix ingest output...        8x  claude-opus-4-5  session-a1b2c3.jsonl
  2/312  updated myapp  Add tests...                3x  claude-opus-4-5  session-d4e5f6.jsonl
  totals: new 2, updated 1, skipped 309, error 0 (unchanged 309)
```

## When adapters can't parse a file

Sometimes log files are malformed — truncated sessions, encoding issues, schema changes between tool versions. Adapters handle this gracefully:

- Partial parses are accepted (some prompts may be missing)
- Parse errors are logged but don't stop ingestion
- The `ingested_files` table tracks error state

```bash
siftd doctor    # shows files with parse errors
```

If a file consistently fails to parse, check the tool's version — the log format may have changed. You can copy and modify the built-in adapter to handle the new format.
