#!/bin/bash
# On session exit, run a targeted ingest to apply any pending tags.
#
# Only ingests the claude_code adapter — not a full ingest.
# Runs quietly; the session is ending so there's no one to show output to.

command -v siftd >/dev/null 2>&1 || exit 0

INPUT=$(cat)

# Try to narrow ingest to just this workspace's project dir.
# Claude Code stores sessions under ~/.claude/projects/<workspace-hash>/
# Passing the specific dir via -p avoids scanning the entire tree.
PROJECT_DIR=""
WORKSPACE="$PWD"
if [ -n "$WORKSPACE" ]; then
  # Replicate Claude Code's workspace hashing: path with / → -
  HASH=$(echo -n "$WORKSPACE" | sed 's|/|-|g; s|^-||')
  CANDIDATE="${HOME}/.claude/projects/${HASH}"
  if [ -d "$CANDIDATE" ]; then
    PROJECT_DIR="$CANDIDATE"
  fi
fi

if [ -n "$PROJECT_DIR" ]; then
  # Scoped ingest: just this workspace's sessions
  siftd ingest -a claude_code -p "$PROJECT_DIR" -q 2>/dev/null
else
  # Fallback: sweep the whole adapter (slower but still bounded)
  siftd ingest -a claude_code -q 2>/dev/null
fi

exit 0
