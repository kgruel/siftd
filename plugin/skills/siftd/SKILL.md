---
skill-interface-version: 1
name: siftd
description: "Search and research past conversations from CLI coding sessions. Use when researching past decisions, design rationale, project history, or finding where an idea originated. Also use when the user mentions siftd, siftd search, or searching past conversations."
argument-hint: "[query] or [--recent] or [--genesis query]"
---

# siftd — Search past coding conversations

siftd searches your past coding conversations (Claude Code, Codex, Gemini CLI) to find decisions, trace how ideas evolved, and retrieve context.

## Quick Search: `/siftd`

When the user invokes `/siftd`, parse their arguments and run the appropriate command:

**Arguments received:** `$ARGUMENTS`

### Mode detection

Parse the arguments to determine mode:

| User input | Mode | Command to run |
|------------|------|----------------|
| `/siftd "query"` | Search | `siftd search "query" --thread` |
| `/siftd -w proj "query"` | Workspace search | `siftd search -w proj "query" --thread` |
| `/siftd --recent` | Recent | `siftd query -n 5` |
| `/siftd --recent -w proj` | Recent (scoped) | `siftd query -n 5 -w proj` |
| `/siftd --genesis "concept"` | First mention | `siftd search --first --by-time "concept"` |

**Default mode**: If arguments don't match a flag pattern, treat as semantic search query.

### After running the search

1. **Show results** — let the user see what was found
2. **Offer follow-up** — based on results:
   - To drill into a conversation: `siftd query <id>`
   - To bookmark for later: `siftd tag <id> research:<topic>`
   - To compare workspaces: repeat with `-w <workspace>`
3. **Tag valuable findings** — if results are useful, prompt to tag before moving on

### Examples

User: `/siftd "error handling"`
→ Run: `siftd search "error handling" --thread`
→ Show results, offer drill-down

User: `/siftd -w myproject "auth flow"`
→ Run: `siftd search -w myproject "auth flow" --thread`
→ Show results scoped to myproject

User: `/siftd --recent`
→ Run: `siftd query -n 5`
→ Show 5 most recent conversations

User: `/siftd --genesis "chunking strategy"`
→ Run: `siftd search --first --by-time "chunking strategy"`
→ Show earliest conversation mentioning this concept

---
skill-interface-version: 1

# Research Patterns

Deeper patterns for when the quick search isn't enough.

## Core: search sequences

A single query rarely lands the answer. Research is iterative: broad → narrow → inspect.

**Broad sweep, then workspace focus:**
```bash
siftd search "error handling"                   # what's out there?
siftd search -w myproject "error handling"      # narrow to the project that matters
```

**Sweep, then drill:**
```bash
siftd search -w myproject "auth flow"           # find the conversation
siftd query 01HX...                          # read the full conversation timeline
```

**Cross-workspace comparison** — same question, different projects:
```bash
siftd search -w projectA "state management"
siftd search -w projectB "state management"
```
Useful when a pattern was explored in one project and you want to apply it in another.

> Full feature set: `reference/search.md` (search modes, composition) and `reference/query.md` (drill-down, SQL, tool-tags).

## Output: reading modes for different goals

The default output shows ranked chunks. When that's not enough, the output modes serve different research needs.

**Understanding a decision (narrative):**
```bash
siftd search -w myproject "why we chose X" --thread
```
`--thread` expands the top conversations into a readable narrative. This is the best mode when you need to understand reasoning, not just find a keyword.

**Following a discussion (context window):**
```bash
siftd search "auth token refresh" --thread              # find the conversation
siftd search "auth token refresh" --context 3           # see ±3 exchanges around the match
```
`--context N` shows the surrounding exchanges. Use after `--thread` identifies the right conversation but you need the back-and-forth.

**Verifying exact wording:**
```bash
siftd search -v "the chunking algorithm"
```
`-v` shows full chunk text. Use when you need to quote or verify specific wording rather than browse.

**Anti-pattern — avoid `--full` for research:**
```bash
# Don't do this for research — too much noise:
siftd search --full "chunking"

# Do this instead — structured narrative:
siftd search --thread "chunking"
```
`--full` dumps entire prompt+response exchanges. It's useful for exact reproduction but overwhelms research workflows.

> All output modes, `--refs`, `--json`, and `--format`: `reference/search.md` § Output modes.

## Filtering: composing constraints

Filters narrow the candidate set before ranking. They compose with each other and with output modes.

**Date-scoped research:**
```bash
siftd search --since 2025-01 "migration strategy"       # recent conversations only
siftd search --since 2025-01 --before 2025-06 "migration"  # specific window
```

**Tagged subset + semantic search:**
```bash
siftd search -l research:auth "token expiry"            # search only tagged conversations
```
This is where tagging pays off — pre-filtered semantic search over curated conversations.

**Score threshold to cut noise:**
```bash
siftd search --threshold 0.7 "event sourcing"           # only high-relevance hits
siftd search --threshold 0.7 -w myproject "event sourcing"  # threshold + workspace
```

**Temporal trace — how an idea evolved:**
```bash
siftd search "state management" --by-time --since 2024-06
```
`--by-time` sorts by time instead of score. Combined with `--since`, this traces how a concept evolved across sessions.

**Full composition example:**
```bash
siftd search -w myproject --since 2025-01 --threshold 0.7 "auth redesign" --thread
```
Workspace + date + threshold + narrative output. Each filter narrows; the output mode controls rendering.

> All filters (`--model`, boolean tags, `--tool-tag`): `reference/search.md` § Filters and `reference/query.md` § Filters.

## Preserving: the tag-retrieve loop

Tagging is investment; retrieval is the payoff. The loop:

**1. Search finds something valuable:**
```bash
siftd search -w myproject "why we switched to JWT" --thread
# Result shows conversation 01HX... with the decision rationale
```

**2. Tag it for future retrieval:**
```bash
siftd tag 01HX... research:auth
```

**3. Future session retrieves instantly:**
```bash
siftd query -l research:auth                         # all auth research, no searching needed
siftd search -l research:auth "token rotation"          # semantic search within tagged set
```

**Batch tagging after a research session:**
```bash
siftd tag --last research:architecture               # tag the conversation you just drilled into
siftd tag --last 3 review                            # tag your last 3 conversations
```

**Tag conventions:**
- `research:*` — investigation findings (`research:auth`, `research:migration`)
- `useful:*` — patterns and examples (`useful:pattern`, `useful:example`)

These conventions are shared with the project's CLAUDE.md, so tags are consistent across all agents and sessions.

> Boolean tag filtering, tag rename/delete, tool call tags: `reference/tags.md`.

## Siftd-First Documentation

HANDOFF.md and CLAUDE.md are thin indexes. Siftd holds the depth.

### Reading HANDOFF references

HANDOFF.md contains references instead of inline rationale:

```markdown
## Key Decisions
| Topic | Reference |
|-------|-----------|
| Auth approach | `01KFMBEQRGX7` • `decision:auth` |
```

To get context, run the reference:
```bash
siftd query 01KFMBEQRGX7              # specific conversation
siftd query -l decision:auth          # all auth decisions
siftd search -w project "auth approach"  # or search
```

### Documentation tags

| Tag | When to apply |
|-----|---------------|
| `decision:*` | Key architectural/design decisions |
| `handoff:update` | Sessions that modified HANDOFF.md |
| `rationale:*` | Why we chose X over Y |
| `genesis:*` | First discussion of a concept |

### End-of-session workflow

If you modified HANDOFF.md during the session:

1. **Tag the session:**
   ```bash
   siftd tag --last handoff:update
   ```

2. **Update HANDOFF.md:**
   - Revise Current Focus section
   - Add row to History table: `| date | session-id | summary |`

3. **Tag any decisions made:**
   ```bash
   siftd tag --last decision:topic-name
   ```

### Walking back through history

Reconstruct how a decision evolved:
```bash
# List all HANDOFF updates
siftd query -l handoff:update --since 2025-01

# Search within HANDOFF sessions
siftd search -l handoff:update "topic" --by-time

# Find when something was first discussed
siftd search --first "concept name"
```

Each HANDOFF update conversation contains the HANDOFF state at that moment (in the prompt), so you can see what changed and why.
