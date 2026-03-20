---
name: "siftd:tag"
description: "Tag the current session or a conversation for later retrieval"
argument-hint: "<tag> [tag...] or <conversation-id> <tag> [tag...]"
---

# /siftd:tag — Direct tagging

Tags the current session (or a specific conversation) for later retrieval.

## Session status

!`siftd session-id 2>/dev/null && echo "✓ Session registered — tags will queue for this session" || echo "⚠ No active session detected — will tag most recent conversation instead"`

## Applying tags

!`siftd tag --current $ARGUMENTS 2>&1`

## Verify

!`echo "Retrieve later with: siftd query -l <tag-name>"`
!`echo "Search within tagged: siftd search -l <tag-name> \"query\""`
