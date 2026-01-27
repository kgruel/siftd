#!/bin/bash
# When agent runs strata commands directly in Bash, nudge toward the skill.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ "$TOOL" = "Bash" ] && echo "$COMMAND" | grep -q "^strata "; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "If not already loaded, consider loading the strata skill for research workflow guidance: Skill tool with skill: \"strata\"."
  }
}
EOF
fi

exit 0
