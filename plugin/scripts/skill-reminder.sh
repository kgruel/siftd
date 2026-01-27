#!/bin/bash
# When user mentions strata, remind agent to load the skill for workflow guidance.

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

if echo "$PROMPT" | grep -qi "strata"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "If not already loaded, invoke Skill tool with skill: \"strata\" to load research workflow instructions."
  }
}
EOF
fi

exit 0
