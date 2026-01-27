#!/bin/bash
# After compaction/resume, remind agent that strata is available for research.

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

# Only fire if strata is installed
command -v strata >/dev/null 2>&1 || exit 0

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Context was compacted. strata is available for researching past conversations. Load the skill first: Skill tool with skill: \"strata\"."
  }
}
EOF

exit 0
