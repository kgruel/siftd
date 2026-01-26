#!/bin/bash
# Reminds agent to load strata skill after context compaction/resume.

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

# Check if strata is available
if command -v strata &>/dev/null || command -v tbd &>/dev/null; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Context was compacted. strata is available for researching past conversations. If doing research, load the skill: Skill tool with skill: \"strata\"."
  }
}
EOF
fi

exit 0
