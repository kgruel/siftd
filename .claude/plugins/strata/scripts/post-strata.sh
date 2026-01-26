#!/bin/bash
# After strata commands, suggests refinement flags and tagging.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

[ "$TOOL" = "Bash" ] || exit 0

# Only fire on strata/tbd commands
echo "$COMMAND" | grep -qE "^(strata|tbd|uv run (strata|tbd)) " || exit 0

# After strata ask: suggest refinement or tagging
if echo "$COMMAND" | grep -qE "(strata|tbd) ask|uv run (strata|tbd) ask"; then
  # Basic usage (no refinement flags) — suggest refinement
  if ! echo "$COMMAND" | grep -qE "\-\-thread|\-\-context|\-\-role|\-\-conversations|\-\-first"; then
    cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "strata tip: refine with --thread (narrative), --context N (surrounding exchanges), or --role user (just prompts). Tag useful results: strata tag <id> research:<topic>"
  }
}
EOF
  else
    cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tag useful results for future retrieval: strata tag <id> research:<topic>"
  }
}
EOF
  fi
  exit 0
fi

# After strata query <id>: suggest tagging
if echo "$COMMAND" | grep -qE "(strata|tbd) query [0-9a-zA-Z]|uv run (strata|tbd) query [0-9a-zA-Z]"; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "If this conversation is useful, bookmark it: strata tag <id> research:<topic>"
  }
}
EOF
fi

exit 0
