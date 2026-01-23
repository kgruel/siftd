#!/bin/bash
# Detects "subtask" mentions and reminds Claude to use the skill

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

if echo "$PROMPT" | grep -qi "subtask"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "If not already loaded, invoke Skill tool with skill: \"subtask\" to load workflow instructions."
  }
}
EOF
fi

exit 0
