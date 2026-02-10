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
| `Edit`, `apply_diff`, `str_replace` | `edit.apply` |

**Timestamps:**
All converted to ISO 8601 format, sorted correctly regardless of source timezone.

**Content structure:**
Different tools represent prompts and responses differently — some as plain text, some as arrays of typed blocks (text, images, tool calls). Adapters normalize this into a consistent block structure.

This normalization is what makes cross-tool queries work. "Show me all shell commands" finds results whether the session was Claude Code, Aider, or Codex.

## Built-in adapters

siftd ships with adapters for:

| Adapter | Tool | Log location | Format |
|---------|------|--------------|--------|
| `claude_code` | Claude Code | `~/.claude/projects/` | JSONL |
| `aider` | Aider | `~/.aider.chat.history.md` | Markdown |
| `gemini_cli` | Gemini CLI | `~/.gemini/tmp/` | JSONL |
| `codex_cli` | Codex CLI | `~/.codex/sessions/` | JSONL |

Each adapter knows where its tool writes logs by default. When you run `siftd ingest`, all adapters scan their default locations.

```bash
siftd adapters    # list discovered adapters
```

```
claude_code   ~/.claude/projects/         built-in
aider         ~/.aider.chat.history.md    built-in
gemini_cli    ~/.gemini/tmp/              built-in
codex_cli     ~/.codex/sessions/          built-in
```

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

See [Writing Adapters](../writing-adapters.md) for the complete implementation guide.

## Deduplication

Adapters declare how they handle re-ingesting the same content:

**File-based deduplication** (`DEDUP_STRATEGY = "file"`):
One conversation per file. If the file hasn't changed, skip it. Most tools work this way — each session gets its own log file.

**Session-based deduplication** (`DEDUP_STRATEGY = "session"`):
Multiple files may contribute to the same conversation. Re-ingesting updates the existing record. Use this when a tool writes multiple files for one session, or when sessions can be appended to.

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
siftd ingest -v    # verbose output shows each step
```

```
claude_code: scanning ~/.claude/projects/
  found 312 files, 3 new
  parsing session-a1b2c3.jsonl... 12 prompts, 34 responses, 89 tool calls
  parsing session-d4e5f6.jsonl... 5 prompts, 12 responses, 23 tool calls
  ...
aider: scanning ~/.aider.chat.history.md
  found 1 file, 0 new (already ingested)
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
