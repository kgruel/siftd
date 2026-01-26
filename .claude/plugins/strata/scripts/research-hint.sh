#!/bin/bash
# Detects research-intent prompts and suggests loading the strata skill.

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty')

# Direct mention of strata
if echo "$PROMPT" | grep -qi "strata"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "If not already loaded, invoke Skill tool with skill: \"strata\" to load research workflow instructions."
  }
}
EOF
  exit 0
fi

# Research-intent keywords
if echo "$PROMPT" | grep -qiE "why did we|when did we|how did we decide|where did we discuss|past decision|design rationale|history of|evolution of|previous conversation|earlier we|rationale for|original discussion|how did .+ evolve|where .+ originated"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "This looks like a research question about past decisions. strata can search past conversations: invoke Skill tool with skill: \"strata\" for the research workflow."
  }
}
EOF
fi

exit 0
