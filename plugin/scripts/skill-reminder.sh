#!/bin/bash
# When user mentions siftd or expresses research intent, suggest loading the skill.

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

# Exit early if no prompt text
[ -z "$PROMPT" ] && exit 0

# Check for explicit siftd mention
if echo "$PROMPT" | grep -qi "siftd"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "If not already loaded, invoke Skill tool with skill: \"siftd\" to load research workflow instructions."
  }
}
EOF
  exit 0
fi

# Check for research intent patterns (case-insensitive)
# These suggest the user wants to look up past work
RESEARCH_PATTERNS=(
  "past conversation"
  "previous session"
  "search history"
  "what did we"
  "when did we"
  "how did we"
  "why did we"
  "last time"
  "earlier session"
  "previous discussion"
  "conversation where"
  "session where"
  "find the conversation"
  "look up.*conversation"
  "search.*past"
  "search.*previous"
)

for pattern in "${RESEARCH_PATTERNS[@]}"; do
  if echo "$PROMPT" | grep -qiE "$pattern"; then
    cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "This looks like a research question. siftd can search past conversations. Load the skill with: Skill tool with skill: \"siftd\""
  }
}
EOF
    exit 0
  fi
done

exit 0
