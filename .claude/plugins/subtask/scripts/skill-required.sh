#!/bin/bash
# Reminds Claude to use the subtask skill when it runs subtask commands directly

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ "$TOOL" = "Bash" ] && echo "$COMMAND" | grep -q "^subtask "; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "If not already loaded, consider loading the subtask skill for workflow guidance."
  }
}
EOF
fi

exit 0
