# Data Model

siftd normalizes conversation logs from different tools into a common structure. Understanding this structure helps you query effectively and know what's possible.

## The core hierarchy

Every log file becomes a **conversation**. A conversation contains **prompts** (what you typed), and each prompt has **responses** (what the model said). Responses may include **tool calls** (actions the model took).

```
Conversation
├── Prompt
│   └── Response
│       ├── text content
│       └── ToolCall
│           ├── input (what was requested)
│           └── result (what happened)
├── Prompt
│   └── Response
│       └── ToolCall
│       └── ToolCall
└── ...
```

This nesting reflects how AI coding tools actually work: you ask something, the model responds, and during that response it might read files, run commands, or edit code. Each of those actions is a tool call with captured inputs and outputs.

## What gets captured

### Conversations

A conversation is a single session with a tool — from when you started it to when you quit or it timed out.

| Field | What it means |
|-------|---------------|
| `id` | siftd's identifier (ULID, sortable by time) |
| `external_id` | The tool's own session identifier |
| `started_at` | When the session began |
| `ended_at` | When it ended (if known) |
| `workspace` | The directory you were working in |
| `harness` | Which tool (Claude Code, Aider, etc.) |

### Prompts

A prompt is one thing you typed or submitted. Multi-turn conversations have multiple prompts.

| Field | What it means |
|-------|---------------|
| `timestamp` | When you submitted it |
| `content` | What you typed (may include attachments) |

### Responses

A response is the model's reply to your prompt. One prompt usually has one response, but streaming or interrupted sessions might have multiple.

| Field | What it means |
|-------|---------------|
| `timestamp` | When the response started |
| `content` | The text the model produced |
| `model` | Which model (claude-opus-4-5, gpt-4o, etc.) |
| `input_tokens` | Tokens consumed from your prompt |
| `output_tokens` | Tokens generated in the response |

### Tool calls

A tool call is an action the model took: reading a file, running a shell command, searching code, editing a file.

| Field | What it means |
|-------|---------------|
| `tool_name` | Canonical name (file.read, shell.execute, file.edit) |
| `input` | Arguments passed to the tool (file path, command, etc.) |
| `result` | What the tool returned |
| `status` | success, error, or pending |

Tool calls are where the real work happens. A typical coding session might have hundreds of tool calls — every `cat`, `grep`, file edit, and test run.

## Vocabulary tables

siftd normalizes raw names into canonical forms using vocabulary tables:

**Harnesses** — the CLI tools that wrap model interactions
- `claude_code`, `aider`, `gemini_cli`, `codex_cli`
- Each has different log formats, but siftd normalizes them

**Models** — the actual model being invoked
- Raw: `claude-3-opus-20240229` → Canonical: `claude-3-opus`
- Includes family (claude, gpt, gemini), variant (opus, sonnet, haiku)

**Tools** — actions models can take
- Raw names vary by harness (Read vs read_file vs file.read)
- siftd maps them to canonical names: `file.read`, `shell.execute`, `file.edit`

**Providers** — who serves the model and bills you
- anthropic, openai, google, openrouter, local

This normalization lets you query across tools. "Show me all shell commands" works whether the session was Claude Code (Bash tool), Aider (run command), or Gemini CLI (execute_shell).

## Workspaces

A workspace is a directory where you worked. siftd groups conversations by workspace automatically.

```bash
siftd query -w myproject    # all conversations in directories containing "myproject"
```

Workspaces also capture git remote URLs when available, so conversations from the same repo on different machines can be linked.

## Content storage

The actual text — your prompts, model responses, tool results — lives in a content-addressable blob store. This means:

1. **Deduplication** — identical content (like repeated system prompts) is stored once
2. **Efficient storage** — large tool results don't bloat the main tables
3. **Integrity** — content is keyed by SHA256 hash

You don't interact with this directly, but it's why siftd can handle thousands of conversations without the database becoming unwieldy.

## What this enables

The normalized structure enables queries you couldn't do with raw log files:

**Filter by any dimension:**
```bash
siftd query -w myproject              # by workspace
siftd query -m claude-opus-4-5        # by model
siftd query --since 2025-01-01        # by time
siftd query --tool-tag shell:test     # by tool usage patterns
```

**Search across all content:**
```bash
siftd search --mode fts "authentication"  # FTS5 keyword search
siftd search "how to handle tokens"       # hybrid: FTS5 + embeddings if configured, else FTS5-only
```

**Aggregate and analyze:**
```bash
siftd db stats                                                   # totals across everything
siftd tag list --on tool_call --prefix shell: --by-workspace     # tool usage patterns per project
siftd report cost                                                # custom SQL reports
```

**Tag for retrieval:**
```bash
siftd tag 01JGK3 decision:auth        # mark a conversation
siftd query -l decision:              # retrieve all decisions
```

The data model is the foundation. Adapters fill it, search queries it, tags annotate it.

## Schema reference

For the complete SQL schema with all columns and constraints, see [Schema Reference](../reference/schema.md).
