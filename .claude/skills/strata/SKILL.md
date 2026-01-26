---
name: strata
description: "Search and research past conversations from CLI coding sessions. Use when researching past decisions, design rationale, project history, or finding where an idea originated. Also use when the user mentions strata, strata ask, or searching past conversations."
---

# strata — Conversation Research Tool

strata searches your past coding conversations (Claude Code, Codex, Gemini CLI).
Use it to find past decisions, trace how ideas evolved, and retrieve context.

## When to Use

- Researching why a decision was made
- Finding where an idea originated
- Understanding how a design evolved
- Looking up past implementation patterns
- Retrieving context from prior sessions

## Workflow: Search → Refine → Inspect → Save

### 1. Search
```bash
strata ask "error handling"                        # basic semantic search
strata ask -w myproject "auth flow"                # filter by workspace
strata ask --since 2024-06 "testing"               # filter by date
```

### 2. Refine
```bash
strata ask "design decision" --thread              # narrative: top conversations expanded
strata ask "why we chose X" --context 2            # ±2 surrounding exchanges
strata ask "testing approach" --role user           # just human prompts, not responses
strata ask "event sourcing" --conversations        # rank whole conversations, not chunks
strata ask "when first discussed Y" --first        # earliest match above threshold
```

### 3. Inspect
```bash
strata ask -v "chunking"                           # full chunk text
strata ask --full "chunking"                       # complete prompt+response exchange
strata ask --refs "authelia"                       # file references + content
strata query <id>                                  # full conversation timeline
```

### 4. Save
Tag useful results so future sessions can find them:
```bash
strata tag <id> research:<topic>                   # bookmark a conversation
strata tag --last research:<topic>                 # tag most recent conversation
strata query -l research:<topic>                   # retrieve tagged conversations
```

Tag conventions:
- `research:*` — investigation findings worth preserving
- `useful:*` — general bookmarks (useful:pattern, useful:example)

## What Works Well

- **Concrete queries** score higher than abstract/philosophical ones
- **Workspace filtering** (`-w`) dramatically improves relevance
- **`--thread`** is the best default for narrative research
- **`--context 2`** gives the surrounding discussion, not just the matching chunk
- Scores: 0.7+ = on-topic, 0.6-0.7 = tangential, <0.6 = noise

## What Doesn't Work Well

- **`--full` for research** — too noisy, shows entire exchanges. Use `--thread` or `--context` instead
- **Abstract philosophical queries** — "what is our philosophy on X" scores lower than "when did we decide to use X"
- **Searching your own session output** — active sessions are auto-excluded, but rephrase if results look circular
- **Single broad queries** — iterate: start specific, broaden if needed

## Quick Reference

| Goal | Command |
|------|---------|
| Find a past decision | `strata ask -w project "why we chose X" --thread` |
| Trace idea evolution | `strata ask "concept" --conversations --chrono` |
| Find first mention | `strata ask "concept" --first` |
| Research a topic | `strata ask "topic" --thread --context 2` |
| Check what you asked | `strata ask "topic" --role user` |
| Bookmark for later | `strata tag <id> research:<topic>` |
