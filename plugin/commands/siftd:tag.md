---
name: "siftd:tag"
description: "Tag the current session or a conversation for later retrieval"
argument-hint: "<tag> [tag...] or <conversation-id> <tag> [tag...]"
---

# /siftd:tag — Direct tagging

Tags the current session (or a specific conversation) for later retrieval.

## Session info
!`siftd session-id 2>/dev/null || echo "No active session detected"`

## Applying tags

!`if siftd session-id >/dev/null 2>&1; then SESSION_ID=$(siftd session-id 2>/dev/null); siftd tag --session "$SESSION_ID" $ARGUMENTS 2>&1; else siftd tag $ARGUMENTS 2>&1; fi`

## Verify

!`echo "Tagged. Retrieve later with: siftd query -l <tag-name>"`
