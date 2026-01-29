#!/bin/bash
# After compaction/resume, remind agent that siftd is available for research.

INPUT=$(cat)

REASON=$(
  echo "$INPUT" | jq -r '
    .reason // .event // .event_name // .eventName // .session_event // .sessionEvent // .trigger // empty
  ' | tr '[:upper:]' '[:lower:]'
)

case "$REASON" in
  compact|resume) ;;
  *) exit 0 ;;
esac

# Only fire if siftd is installed
command -v siftd >/dev/null 2>&1 || exit 0

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Context was compacted. siftd is available for researching past conversations. Load the skill first: Skill tool with skill: \"siftd\"."
  }
}
EOF

exit 0
