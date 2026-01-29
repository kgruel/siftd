#!/bin/bash
# When agent runs siftd commands directly in Bash, nudge toward the skill.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ "$TOOL" = "Bash" ] && echo "$COMMAND" | grep -q "^siftd "; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "If not already loaded, consider loading the siftd skill for research workflow guidance: Skill tool with skill: \"siftd\"."
  }
}
EOF
fi

exit 0
