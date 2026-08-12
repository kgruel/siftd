# Adapters

Every AI coding tool writes logs differently. Claude Code uses JSONL with one structure, Aider uses Markdown, Gemini CLI uses a different JSON schema. Adapters are the translation layer — they read these different formats and produce the common data model siftd understands.

## The problem adapters solve

Without adapters, you'd need separate tools for each coding assistant. Want to search across Claude Code and Aider sessions? You'd need to understand both log formats and write custom queries for each.

Adapters let siftd treat all your conversations uniformly. Once ingested, a conversation from Claude Code looks the same as one from Gemini CLI — same fields, same query interface, same search.

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
| `gemini_cli` | Gemini CLI | `~/.gemini/tmp/` | JSONL |
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
  (`gemini_cli`, `aider`).

Tiers show up in the `siftd adapters` listing, and ingest tags file-error
warnings from non-core adapters with their tier.

```bash
siftd adapters    # list discovered adapters
```

```
claude_code      builtin  core    ~/.claude/projects, ~/.config/claude/projects
aider            builtin  frozen  ~/.aider
gemini_cli       builtin  frozen  ~/.gemini/tmp
antigravity_cli  builtin  core    ~/.gemini/antigravity-cli
codex_cli        builtin  core    ~/.codex/sessions
```

### Disabling an adapter

Any adapter can be turned off in config — useful when a frozen-tier adapter's
parse warnings are noise for a tool you no longer use:

```toml
# ~/.config/siftd/config.toml
[adapters.gemini_cli]
enabled = false
```

Or via the CLI:

```bash
siftd config set adapters.gemini_cli.enabled false
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
2. Implement `parse()` to read your tool's log format
3. Map tool names to canonical forms in `TOOL_ALIASES`

Drop-in adapters are auto-discovered — just save the file and run `siftd ingest`.

See [Writing Adapters](../guides/writing-adapters.md) for the complete implementation guide.

## Deduplication

Adapters declare how they handle re-ingesting the same content:

**File-based deduplication** (`DEDUP_STRATEGY = "file"`):
One conversation per file. If the file hasn't changed, skip it; if it has, the conversation is replaced. Most tools work this way — each session gets its own log file. A parse that yields more than one conversation fails the source.

**Session-based deduplication** (`DEDUP_STRATEGY = "session"`):
The source is a container of many conversations, each deduped independently by `external_id`. Use this when one source holds several sessions or grows new ones over its life — a SQLite database of chats (Gemini CLI, OpenCode), or a markdown history file every session is appended to (aider).

The deduplication strategy ensures `siftd ingest` is idempotent — run it as often as you want without creating duplicate conversations.

## Adapter lifecycle

When you run `siftd ingest`:

1. All adapters are loaded (built-in + drop-in)
2. Each adapter's `discover()` finds candidate files
3. Files are checked against `ingested_files` table (skip if already processed and unchanged)
4. For new/changed files, `can_handle()` confirms the adapter can parse it
5. `parse()` produces `Conversation` objects
6. Storage layer writes to SQLite, normalizes tool names, builds FTS index

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
