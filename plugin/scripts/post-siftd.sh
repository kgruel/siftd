#!/bin/bash
# After agent runs siftd in Bash, suggest refinements — once per subcommand per session.
#
# Uses a state file to suppress repeated hints. First use of `siftd search` gets a tip;
# second use doesn't. Resets on session start (see session-start.sh).

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

[ "$TOOL" = "Bash" ] || exit 0
echo "$COMMAND" | grep -q 'siftd ' || exit 0

SUBCOMMAND=$(echo "$COMMAND" | sed 's/.*siftd //' | awk '{print $1}')

# --- Per-session dedup ---
# State dir lives in XDG_STATE_HOME or /tmp as fallback.
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/siftd/hook-hints"
mkdir -p "$STATE_DIR" 2>/dev/null

HINT_KEY="$SUBCOMMAND"
# For search, distinguish plain vs --thread (different tips)
if [ "$SUBCOMMAND" = "search" ] && echo "$COMMAND" | grep -q '\-\-thread'; then
  HINT_KEY="search-thread"
fi

MARKER="$STATE_DIR/$HINT_KEY"
if [ -f "$MARKER" ]; then
  exit 0  # Already hinted this subcommand in this session
fi
touch "$MARKER" 2>/dev/null

case "$SUBCOMMAND" in
  search)
    if echo "$COMMAND" | grep -q '\-\-thread'; then
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: drill into a specific result with `siftd query <id>`. To bookmark: `siftd tag <id> research:<topic>`."
  }
}
JSON
    elif echo "$COMMAND" | grep -q '\-\-json'; then
      exit 0
    else
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: for narrative results, add `--thread`. To bookmark results: `siftd tag <id> research:<topic>`."
  }
}
JSON
    fi
    ;;
  query)
    if echo "$COMMAND" | grep -qE 'query [0-9A-Z]{6,}'; then
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: tag this conversation with `siftd tag <id> decision:<topic>` or `research:<topic>`."
  }
}
JSON
    else
      cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Tip: drill into a conversation with `siftd query <full-id>`. Filter by project with `-w <workspace>`."
  }
}
JSON
    fi
    ;;
  tag)
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Verify: `siftd query -l <tag-name>` or `siftd search -l <tag-name> \"query\"`."
  }
}
JSON
    ;;
  *)
    # No hints for peek, ingest, export, etc. — subcommand output is self-explanatory.
    rm -f "$MARKER" 2>/dev/null  # Don't count as hinted
    exit 0
    ;;
esac

exit 0
