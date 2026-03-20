#!/bin/bash
# Register session for live tagging and remind agent about siftd after compaction.
#
# Registration happens on every SessionStart (start, resume, compact) so that
# `siftd tag --current` always resolves the session ID.
#
# The skill reminder only fires on compact/resume (when context was lost).

INPUT=$(cat)

# Only fire if siftd is installed
command -v siftd >/dev/null 2>&1 || exit 0

# --- Always register the session for live tagging ---
SESSION_ID=$(echo "$INPUT" | jq -r '.sessionId // empty')
if [ -n "$SESSION_ID" ]; then
  siftd register --session "claude_code::$SESSION_ID" --adapter claude_code --workspace "$PWD" 2>/dev/null
fi

# --- Reset per-session PostToolUse hint dedup ---
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/siftd/hook-hints"
rm -f "$STATE_DIR"/* 2>/dev/null

# --- Determine event reason for conditional reminder ---
# Claude Code may send different payload shapes across versions.
# Try the documented field first, then fall back to alternatives.
REASON=$(
  echo "$INPUT" | jq -r '
    .reason // .event // .trigger // empty
  ' 2>/dev/null | tr '[:upper:]' '[:lower:]'
)

# On compact/resume, context was lost — remind agent about siftd
case "$REASON" in
  compact|resume)
    cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Context was compacted. siftd is available for researching past conversations. Use `/siftd \"query\"` for quick search or load the full skill: Skill tool with skill: \"siftd\"."
  }
}
EOF
    ;;
  *)
    # Fresh start or unknown reason — register was done above, no reminder needed
    exit 0
    ;;
esac

exit 0
