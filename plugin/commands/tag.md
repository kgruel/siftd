---
name: "siftd:tag"
description: "Tag the current session or a conversation for later retrieval"
argument-hint: "<tag> [tag...] or <conversation-id> <tag> [tag...]"
---

# /siftd:tag

!`command -v siftd >/dev/null 2>&1 && siftd tag --current $ARGUMENTS 2>&1 || echo "✗ siftd not found in PATH — install with: uv pip install siftd"`
