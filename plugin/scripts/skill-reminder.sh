#!/bin/bash
# When user explicitly mentions siftd or past conversations, suggest loading the skill.
#
# Design: high-precision, low-recall. False negatives are cheap (user says /siftd).
# False positives are annoying. Only fire on unambiguous research signals.

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

[ -z "$PROMPT" ] && exit 0

# Explicit siftd mention — always fire
if echo "$PROMPT" | grep -qi '\bsiftd\b'; then
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

# High-confidence signals only: phrases that unambiguously reference past sessions.
# Intentionally excludes "what did we", "how did we", "last time" — too generic.
if echo "$PROMPT" | grep -qiE '\b(past (session|conversation)|earlier (session|conversation)|previous (session|conversation)|search (my |our )?(past |previous )?(session|conversation))\b'; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "siftd can search past conversations. Use `/siftd \"query\"` or load the full skill: Skill tool with skill: \"siftd\""
  }
}
EOF
  exit 0
fi

exit 0
