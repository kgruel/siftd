#!/bin/bash
# After agent runs siftd in Bash, suggest refinements based on the subcommand and flags used.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ "$TOOL" != "Bash" ]; then
  exit 0
fi

if ! echo "$COMMAND" | grep -q "siftd "; then
  exit 0
fi

SUBCOMMAND=$(echo "$COMMAND" | sed 's/.*siftd //' | awk '{print $1}')

case "$SUBCOMMAND" in
  search)
    # Check which flags are already used to give contextual hints
    if echo "$COMMAND" | grep -q "\-\-thread"; then
      # Already using --thread, suggest next steps
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: drill into a specific result with `siftd query <id>`. To bookmark a useful find: `siftd tag <id> research:<topic>`."
  }
}
JSON
    elif echo "$COMMAND" | grep -q "\-\-json"; then
      # JSON output, no tips needed
      exit 0
    else
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: for narrative results, re-run with `--thread`. To see surrounding context, add `--context 2`. To bookmark results: `siftd tag <id> research:<topic>`."
  }
}
JSON
    fi
    ;;
  query)
    # Check if it's a drill-down (has an ID argument) or a listing
    if echo "$COMMAND" | grep -qE "query [0-9A-Z]{6,}"; then
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: tag this conversation for later retrieval with `siftd tag <id> <tag>`. Use prefixed tags like `decision:auth` or `research:topic`."
  }
}
JSON
    else
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: drill into a specific conversation with `siftd query <full-id>`. Add `-w <workspace>` to filter by project."
  }
}
JSON
    fi
    ;;
  peek)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: for raw text extraction, add `--last-response`. For a complete session view, use `siftd peek <id> --full`. Tag the current session with `/siftd:tag <tag>`."
  }
}
JSON
    ;;
  tag)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: verify the tag with `siftd query -l <tag-name>` or search within tagged conversations: `siftd search -l <tag-name> \"query\"`."
  }
}
JSON
    ;;
  ingest)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Ingest complete. If using embeddings, rebuild the index with `siftd search --index`. Any pending live tags have been applied."
  }
}
JSON
    ;;
  export)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: use `siftd export --last --format markdown` for readable output, or `--format json` for machine consumption."
  }
}
JSON
    ;;
  *)
    exit 0
    ;;
esac

exit 0
