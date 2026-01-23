#!/bin/bash
# Reminds Claude to load the subtask skill after compaction/resume (skills cannot be force-loaded).

INPUT=$(cat)

# Try to detect why the session started. Claude Code hook payloads may vary, so probe a few common keys.
REASON=$(
  echo "$INPUT" | jq -r '
    .reason // .event // .event_name // .eventName // .session_event // .sessionEvent // .trigger // empty
  ' | tr '[:upper:]' '[:lower:]'
)

case "$REASON" in
  compact|resume) ;;
  *) exit 0 ;;
esac

# Prefer the hook-provided cwd if present; fall back to current directory.
CWD=$(echo "$INPUT" | jq -r '.cwd // .working_directory // .workingDirectory // empty')
if [ -n "$CWD" ] && [ -d "$CWD" ]; then
  cd "$CWD" || exit 0
fi

if [ -d ".subtask" ]; then
  cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Context was compacted. If using subtask, load the skill first: Skill tool with skill: \"subtask\"."
  }
}
EOF
fi

exit 0

