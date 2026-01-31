#!/bin/bash
# After compaction/resume, remind agent that siftd is available for research.
# Also registers the session for live tagging.

INPUT=$(cat)

REASON=$(
  echo "$INPUT" | jq -r '
    .reason // .event // .event_name // .eventName // .session_event // .sessionEvent // .trigger // empty
  ' | tr '[:upper:]' '[:lower:]'
)

case "$REASON" in
  compact|resume|start) ;;
  *) exit 0 ;;
esac

# Only fire if siftd is installed
command -v siftd >/dev/null 2>&1 || exit 0

# Register this session for live tagging
# Use namespaced session ID (claude_code::sessionId) to match adapter's external_id format
SESSION_ID=$(echo "$INPUT" | jq -r '.sessionId // empty')
if [ -n "$SESSION_ID" ]; then
  siftd register --session "claude_code::$SESSION_ID" --adapter claude_code --workspace "$PWD" 2>/dev/null
fi

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Context was compacted. siftd is available for researching past conversations. Load the skill first: Skill tool with skill: \"siftd\"."
  }
}
EOF

exit 0
