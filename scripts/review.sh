#!/usr/bin/env bash
# review.sh
# DESC: Launch review agent in a worktree
# Usage: ./dev review <path> [options]
# Dependencies: git, python3, codex|claude (agent)
# Idempotent: No (launches external process)
source "$(dirname "$0")/lib/dev.sh"
source "$(dirname "$0")/lib/templates.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev review <path> [options]

Launch a review agent in a worktree directory.

Arguments:
  path                 Path to worktree (or . for current directory)

Options:
  --agent <cmd>        Agent command (default: codex)
  --prompt <file>      Prompt template (default: scripts/review-prompt.md)
  --context <file>     Context file (default: auto-detect TASK.md)
  --base <branch>      Base branch for diff (default: main)
  --background         Launch agent in background
  --dry-run            Show expanded prompt without launching
  --help               Show this message

Template variables:
  {{branch}}           Current git branch name
  {{diff_stat}}        git diff --stat against base
  {{context}}          Contents of context file
  {{dev_commands}}     Dev harness commands (from ./dev help)

Examples:
  ./dev review .                          # Review current directory
  ./dev review ./worktrees/impl-foo       # Review specific worktree
  ./dev review . --agent claude           # Use claude instead of codex
  ./dev review . --agent "aider --msg"    # Custom agent command
  ./dev review . --prompt my-review.md    # Custom prompt template
  ./dev review . --background             # Launch and return
EOF
}

main() {
    local path=""
    local agent="codex"
    local prompt_file="$DEV_ROOT/scripts/review-prompt.md"
    local context_file=""
    local base_branch="main"
    local background=0
    local dry_run=0

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --agent) cli_require_value "$1" "${2:-}" || exit 1; agent="$2"; shift ;;
            --prompt) cli_require_value "$1" "${2:-}" || exit 1; prompt_file="$2"; shift ;;
            --context) cli_require_value "$1" "${2:-}" || exit 1; context_file="$2"; shift ;;
            --base) cli_require_value "$1" "${2:-}" || exit 1; base_branch="$2"; shift ;;
            --background) background=1 ;;
            --dry-run) dry_run=1 ;;
            --help|-h) usage; exit 0 ;;
            -*) cli_unknown_flag "$1"; exit 1 ;;
            *) path="$1" ;;
        esac
        shift
    done

    if [ -z "$path" ]; then
        usage
        exit 1
    fi

    # Resolve path
    path=$(cd "$path" 2>/dev/null && pwd) || {
        log_error "Cannot access path '$path'"
        exit 1
    }

    # Verify it's a git repo
    if ! git -C "$path" rev-parse --git-dir >/dev/null 2>&1; then
        log_error "'$path' is not a git repository"
        exit 1
    fi

    # Get branch name
    local branch
    branch=$(git -C "$path" branch --show-current 2>/dev/null || echo "detached")

    # Get diff stat
    local diff_stat
    diff_stat=$(git -C "$path" diff --stat "$base_branch" 2>/dev/null || echo "No changes from $base_branch")

    # Auto-detect context file if not specified
    if [ -z "$context_file" ]; then
        for candidate in "$path/TASK.md" "$path/.claude/context.md" "$path/README.md"; do
            if [ -f "$candidate" ]; then
                context_file="$candidate"
                break
            fi
        done
    fi

    # Read context
    local context=""
    if [ -n "$context_file" ] && [ -f "$context_file" ]; then
        context=$(cat "$context_file")
    else
        context="No context file found. Review the code changes directly."
    fi

    # Get dev commands (if ./dev exists in worktree)
    local dev_commands=""
    if [ -x "$path/dev" ]; then
        dev_commands=$("$path/dev" help 2>/dev/null | grep -A100 "^Commands:" | tail -n +2 | head -20 || echo "")
    fi

    # Read and expand template
    if [ ! -f "$prompt_file" ]; then
        log_error "Prompt template not found: $prompt_file"
        exit 1
    fi

    local prompt
    prompt=$(TPL_branch="$branch" \
             TPL_diff_stat="$diff_stat" \
             TPL_context="$context" \
             TPL_dev_commands="$dev_commands" \
             template_inject_env "$prompt_file")

    # Output
    echo -e "${BOLD}Review:${NC} $path"
    echo -e "${BOLD}Branch:${NC} $branch"
    echo -e "${BOLD}Agent:${NC} $agent"
    [ -n "$context_file" ] && echo -e "${BOLD}Context:${NC} $context_file"

    if [ $dry_run -eq 1 ]; then
        echo ""
        echo -e "${BOLD}Expanded prompt:${NC}"
        echo "----------------------------------------"
        echo "$prompt"
        echo "----------------------------------------"
        exit 0
    fi

    echo ""

    # Ensure worktree has venv if it has ./dev
    if [ -x "$path/dev" ] && [ ! -d "$path/.venv" ]; then
        log_info "Setting up worktree..."
        (cd "$path" && ./dev setup)
    fi

    # Launch agent
    if [ $background -eq 1 ]; then
        log_info "Launching $agent in background..."
        (cd "$path" && nohup $agent "$prompt" > .review.log 2>&1 &)
        echo "Monitor with: siftd peek -w $(basename "$path")"
    else
        log_info "Launching $agent..."
        (cd "$path" && exec $agent "$prompt")
    fi
}

main "$@"
