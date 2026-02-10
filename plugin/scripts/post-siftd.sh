#!/bin/bash
# After agent runs siftd in Bash, suggest refinements based on the subcommand.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ "$TOOL" != "Bash" ]; then
  exit 0
fi

if ! echo "$COMMAND" | grep -q "^siftd "; then
  exit 0
fi

SUBCOMMAND=$(echo "$COMMAND" | awk '{print $2}')

case "$SUBCOMMAND" in
  search)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: for narrative results, re-run with `--thread`. If you need surrounding exchanges, add `--context 2`. To surface file references, add `--refs`."
  }
}
JSON
    ;;
  query)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: drill into a specific conversation with `siftd query <full-id>` (use the full ID from the list)."
  }
}
JSON
    ;;
  peek)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: for raw text extraction, add `--last-response`. For a complete session view, use `siftd peek <id> --full`."
  }
}
JSON
    ;;
  tag)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: verify the tag with `siftd query -l <tag-name>` or `siftd search -l <tag-name> \"query\"`."
  }
}
JSON
    ;;
  *)
    exit 0
    ;;
esac

exit 0
