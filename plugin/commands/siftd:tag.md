---
name: "siftd:tag"
description: "Tag the current session or a conversation for later retrieval"
argument-hint: "<tag> [tag...] or <conversation-id> <tag> [tag...]"
---

# /siftd:tag — Direct tagging

Tags the current session (or a specific conversation) for later retrieval.

## Pre-flight

!`command -v siftd >/dev/null 2>&1 && echo "✓ siftd installed" || echo "✗ siftd not found in PATH — install with: uv pip install siftd"`

!`siftd session-id 2>/dev/null && echo "✓ Session registered — tags will queue for this session" || echo "⚠ No active session — will tag most recent ingested conversation"`

## Applying tags

!`siftd tag --current $ARGUMENTS 2>&1`

## Next steps

!`echo "Retrieve later: siftd query -l <tag-name>"`
!`echo "Search within:  siftd search -l <tag-name> \"query\""`
