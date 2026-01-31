---
skill-interface-version: 1
name: siftd-tag
description: "Tag the current session or exchange for later retrieval. Tags are queued and applied when the session is ingested into siftd."
user-invocable: true
---

# /siftd:tag — Tag Current Session

Tag the current session or a specific exchange for later retrieval. Tags are queued and applied when the session is ingested.

## Usage

```
/siftd:tag <tag-name> [tag-name...]           # tag the conversation
/siftd:tag --exchange <tag-name> [tag-name...] # tag the current exchange
```

## Examples

```bash
/siftd:tag decision:auth              # tag conversation with decision:auth
/siftd:tag research:caching useful    # apply multiple tags
/siftd:tag --exchange key-insight     # tag the current exchange
```

## How It Works

1. The session ID is retrieved using `siftd session-id`
2. For `--exchange`, the current exchange count is determined via `siftd peek`
3. Tags are queued using `siftd tag --session`
4. When `siftd ingest` runs, queued tags are applied to the conversation

## Tag Conventions

Use namespaced tags for organization:

| Prefix | Usage |
|--------|-------|
| `decision:*` | Key architectural decisions (decision:auth, decision:schema) |
| `research:*` | Investigation findings (research:caching, research:patterns) |
| `useful:*` | Reusable patterns/examples (useful:pattern, useful:example) |
| `rationale:*` | Why we chose X over Y |
| `genesis:*` | First discussion of a concept |

## Implementation

Execute the following when this skill is invoked:

```bash
#!/bin/bash
# Get session ID for current workspace
SESSION_ID=$(siftd session-id 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
  echo "Error: No session ID found. Is the siftd hook installed?"
  echo "Tip: Ensure plugin/scripts/session-start.sh is configured."
  exit 1
fi

# Check for --exchange flag
if [ "$1" = "--exchange" ]; then
  shift
  if [ $# -eq 0 ]; then
    echo "Usage: /siftd:tag --exchange <tag-name> [tag-name...]"
    exit 1
  fi

  # Get current exchange count (0-based, so count-1 is last exchange)
  EXCHANGE_COUNT=$(siftd peek "$SESSION_ID" --json 2>/dev/null | jq -r '.exchange_count // 0')
  if [ "$EXCHANGE_COUNT" -gt 0 ]; then
    EXCHANGE_INDEX=$((EXCHANGE_COUNT - 1))
    siftd tag --session "$SESSION_ID" --exchange "$EXCHANGE_INDEX" "$@"
  else
    echo "Error: No exchanges found in session"
    exit 1
  fi
else
  if [ $# -eq 0 ]; then
    echo "Usage: /siftd:tag <tag-name> [tag-name...]"
    exit 1
  fi
  siftd tag --session "$SESSION_ID" "$@"
fi
```

## Notes

- Tags are applied at ingest time, not immediately
- The hook must be installed for session registration to work
- Same tag queued twice is a no-op (UNIQUE constraint)
- Exchange-level tags target the prompt (user message) at that index
