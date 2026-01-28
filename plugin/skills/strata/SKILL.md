---
name: strata
description: "Search and research past conversations from CLI coding sessions. Use when researching past decisions, design rationale, project history, or finding where an idea originated. Also use when the user mentions strata, strata ask, or searching past conversations."
argument-hint: "[query] or [--recent] or [--genesis query]"
---

# strata — Search past coding conversations

strata searches your past coding conversations (Claude Code, Codex, Gemini CLI) to find decisions, trace how ideas evolved, and retrieve context.

## Quick Search: `/strata`

When the user invokes `/strata`, parse their arguments and run the appropriate command:

**Arguments received:** `$ARGUMENTS`

### Mode detection

Parse the arguments to determine mode:

| User input | Mode | Command to run |
|------------|------|----------------|
| `/strata "query"` | Search | `strata ask "query" --thread` |
| `/strata -w proj "query"` | Workspace search | `strata ask -w proj "query" --thread` |
| `/strata --recent` | Recent | `strata query -n 5` |
| `/strata --recent -w proj` | Recent (scoped) | `strata query -n 5 -w proj` |
| `/strata --genesis "concept"` | First mention | `strata ask --first --chrono "concept"` |

**Default mode**: If arguments don't match a flag pattern, treat as semantic search query.

### After running the search

1. **Show results** — let the user see what was found
2. **Offer follow-up** — based on results:
   - To drill into a conversation: `strata query <id>`
   - To bookmark for later: `strata tag <id> research:<topic>`
   - To compare workspaces: repeat with `-w <workspace>`
3. **Tag valuable findings** — if results are useful, prompt to tag before moving on

### Examples

User: `/strata "error handling"`
→ Run: `strata ask "error handling" --thread`
→ Show results, offer drill-down

User: `/strata -w myproject "auth flow"`
→ Run: `strata ask -w myproject "auth flow" --thread`
→ Show results scoped to myproject

User: `/strata --recent`
→ Run: `strata query -n 5`
→ Show 5 most recent conversations

User: `/strata --genesis "chunking strategy"`
→ Run: `strata ask --first --chrono "chunking strategy"`
→ Show earliest conversation mentioning this concept

---

# Research Patterns

Deeper patterns for when the quick search isn't enough.

## Core: search sequences

A single query rarely lands the answer. Research is iterative: broad → narrow → inspect.

**Broad sweep, then workspace focus:**
```bash
strata ask "error handling"                   # what's out there?
strata ask -w myproject "error handling"      # narrow to the project that matters
```

**Sweep, then drill:**
```bash
strata ask -w myproject "auth flow"           # find the conversation
strata query 01HX...                          # read the full conversation timeline
```

**Cross-workspace comparison** — same question, different projects:
```bash
strata ask -w projectA "state management"
strata ask -w projectB "state management"
```
Useful when a pattern was explored in one project and you want to apply it in another.

> Full feature set: `reference/ask.md` (search modes, composition) and `reference/query.md` (drill-down, SQL, tool-tags).

## Output: reading modes for different goals

The default output shows ranked chunks. When that's not enough, the output modes serve different research needs.

**Understanding a decision (narrative):**
```bash
strata ask -w myproject "why we chose X" --thread
```
`--thread` expands the top conversations into a readable narrative. This is the best mode when you need to understand reasoning, not just find a keyword.

**Following a discussion (context window):**
```bash
strata ask "auth token refresh" --thread              # find the conversation
strata ask "auth token refresh" --context 3           # see ±3 exchanges around the match
```
`--context N` shows the surrounding exchanges. Use after `--thread` identifies the right conversation but you need the back-and-forth.

**Verifying exact wording:**
```bash
strata ask -v "the chunking algorithm"
```
`-v` shows full chunk text. Use when you need to quote or verify specific wording rather than browse.

**Anti-pattern — avoid `--full` for research:**
```bash
# Don't do this for research — too much noise:
strata ask --full "chunking"

# Do this instead — structured narrative:
strata ask --thread "chunking"
```
`--full` dumps entire prompt+response exchanges. It's useful for exact reproduction but overwhelms research workflows.

> All output modes, `--refs`, `--json`, and `--format`: `reference/ask.md` § Output modes.

## Filtering: composing constraints

Filters narrow the candidate set before ranking. They compose with each other and with output modes.

**Date-scoped research:**
```bash
strata ask --since 2025-01 "migration strategy"       # recent conversations only
strata ask --since 2025-01 --before 2025-06 "migration"  # specific window
```

**Tagged subset + semantic search:**
```bash
strata ask -l research:auth "token expiry"            # search only tagged conversations
```
This is where tagging pays off — pre-filtered semantic search over curated conversations.

**Score threshold to cut noise:**
```bash
strata ask --threshold 0.7 "event sourcing"           # only high-relevance hits
strata ask --threshold 0.7 -w myproject "event sourcing"  # threshold + workspace
```

**Temporal trace — how an idea evolved:**
```bash
strata ask "state management" --chrono --since 2024-06
```
`--chrono` sorts by time instead of score. Combined with `--since`, this traces how a concept evolved across sessions.

**Full composition example:**
```bash
strata ask -w myproject --since 2025-01 --threshold 0.7 "auth redesign" --thread
```
Workspace + date + threshold + narrative output. Each filter narrows; the output mode controls rendering.

> All filters (`--role`, `--model`, boolean tags, `--tool-tag`): `reference/ask.md` § Filters and `reference/query.md` § Filters.

## Preserving: the tag-retrieve loop

Tagging is investment; retrieval is the payoff. The loop:

**1. Search finds something valuable:**
```bash
strata ask -w myproject "why we switched to JWT" --thread
# Result shows conversation 01HX... with the decision rationale
```

**2. Tag it for future retrieval:**
```bash
strata tag 01HX... research:auth
```

**3. Future session retrieves instantly:**
```bash
strata query -l research:auth                         # all auth research, no searching needed
strata ask -l research:auth "token rotation"          # semantic search within tagged set
```

**Batch tagging after a research session:**
```bash
strata tag --last research:architecture               # tag the conversation you just drilled into
strata tag --last 3 review                            # tag your last 3 conversations
```

**Tag conventions:**
- `research:*` — investigation findings (`research:auth`, `research:migration`)
- `useful:*` — patterns and examples (`useful:pattern`, `useful:example`)

These conventions are shared with the project's CLAUDE.md, so tags are consistent across all agents and sessions.

> Boolean tag filtering, tag rename/delete, tool call tags: `reference/tags.md`.
