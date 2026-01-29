#!/bin/bash
# When user mentions siftd, remind agent to load the skill for workflow guidance.

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

if echo "$PROMPT" | grep -qi "siftd"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "If not already loaded, invoke Skill tool with skill: \"siftd\" to load research workflow instructions."
  }
}
EOF
fi

exit 0
