---
skill-interface-version: 1
name: siftd
description: "Search and research past conversations from CLI coding sessions. Use when researching past decisions, design rationale, project history, or finding where an idea originated. Also use when the user mentions siftd, searching past conversations, or asks about previous sessions/decisions."
argument-hint: '"query" or [-w workspace] "query" or --recent or --genesis "concept"'
---

# siftd — Search past coding conversations

siftd searches your past coding conversations (Claude Code, Codex, Gemini CLI, Pi, Aider, VSCode, Copilot CLI, OpenCode) to find decisions, trace how ideas evolved, and retrieve context.

## Auto-trigger guidance

When this skill is auto-loaded by research-intent detection (user did not type `/siftd`):

1. Run a best-guess search with `--mode=thread` (add `-w <workspace>` if implied).
2. Summarize findings in natural language — do not dump raw output.
3. Offer next steps: drill into a conversation (`siftd show <id>`), refine the search, or tag useful results (`siftd tag <id> research:<topic>`).

When the user explicitly invokes `/siftd`, follow the mode detection below.

If the user invokes `/siftd:tag`, skip search — run `siftd tag --current <tags>`.

## Quick Search: `/siftd`

**Arguments received:** `$ARGUMENTS`

### Mode detection

| User input | Command to run |
|------------|----------------|
| `/siftd "query"` | `siftd search "query" --mode=thread` |
| `/siftd -w proj "query"` | `siftd search -w proj "query" --mode=thread` |
| `/siftd --recent` | `siftd query -n 5` |
| `/siftd --recent -w proj` | `siftd query -n 5 -w proj` |
| `/siftd --genesis "concept"` | `siftd search --select=first --sort=time "concept"` |

**Default**: If arguments don't match a flag pattern, treat as semantic search query.

### After running the search

1. Show results
2. Offer drill-down: `siftd show <id>`
3. If results are useful, suggest tagging: `siftd tag <id> research:<topic>`

## Live tagging: `/siftd:tag`

The SessionStart hook registers the session automatically. `/siftd:tag` resolves it via `siftd tag --current`.

If no session is detected, `--current` falls back to `--last 1` (most recent ingested conversation). The command output indicates which path was taken.

Tags are queued as "pending" and applied when the conversation is ingested (`siftd ingest`).

**Tag conventions:**

| Prefix | Usage |
|--------|-------|
| `decision:*` | Architectural/design decisions |
| `research:*` | Investigation findings |
| `useful:*` | Reusable patterns/examples |
| `rationale:*` | Why X over Y |
| `genesis:*` | First discussion of a concept |

## Reference docs

For full flag lists, composition patterns, and filtering options:

> **Search**: `reference/search.md` — output modes, filters, diversity tuning, composition examples
> **Query**: `reference/query.md` — listing, drill-down, SQL queries, tool-tag filtering
> **Tags**: `reference/tags.md` — apply/remove, boolean filtering, tag conventions, tool call tags
