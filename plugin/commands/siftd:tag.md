---
name: "siftd:tag"
description: "Tag the current session or a conversation for later retrieval"
argument-hint: "<tag> [tag...] or <conversation-id> <tag> [tag...]"
---

# /siftd:tag — Direct tagging

Tags the current session (or a specific conversation) for later retrieval.

## Applying tags

!`siftd tag --current $ARGUMENTS 2>&1`

## Verify

!`echo "Tagged. Retrieve later with: siftd query -l <tag-name>"`
