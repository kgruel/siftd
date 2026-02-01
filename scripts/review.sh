#!/usr/bin/env bash
# DESC: Enter subtask worktree and launch review agent
source "$(dirname "$0")/_lib.sh"

usage() {
    cat <<EOF
Usage: ./dev review <task> [agent] [--dry-run]

Enter a subtask worktree and launch a review agent.

Arguments:
  task       Subtask name (e.g., impl/peek-parent-session)
  agent      codex (default), claude

Options:
  --dry-run  Show prompt without launching agent
  --help     Show this message

Examples:
  ./dev review impl/my-feature
  ./dev review impl/my-feature claude
  ./dev review impl/my-feature --dry-run
EOF
}

main() {
    local task=""
    local agent="codex"
    local dry_run=0

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry_run=1 ;;
            --help|-h) usage; exit 0 ;;
            codex|claude) agent="$1" ;;
            -*) echo "Unknown option: $1"; exit 1 ;;
            *) task="$1" ;;
        esac
        shift
    done

    if [ -z "$task" ]; then
        usage
        exit 1
    fi

    # Get worktree path
    local worktree
    worktree=$(subtask workspace "$task" 2>/dev/null) || {
        echo -e "${RED}Error: Could not find worktree for task '$task'${NC}"
        echo "Run 'subtask list' to see available tasks"
        exit 1
    }

    echo -e "${BOLD}Task:${NC} $task"
    echo -e "${BOLD}Worktree:${NC} $worktree"

    # Setup if needed
    if [ ! -d "$worktree/.venv" ]; then
        echo "Setting up worktree..."
        (cd "$worktree" && ./dev setup)
    fi

    # Get task info for prompt
    local title changes
    title=$(subtask show "$task" 2>/dev/null | grep "^Title:" | cut -d: -f2- | xargs)
    changes=$(subtask show "$task" 2>/dev/null | grep "^Changes:" | cut -d: -f2- | xargs)

    # Build review prompt
    local prompt
    prompt="Review the $task branch: $title

Changes: $changes

## Dev Commands
\`\`\`
./dev check          # Lint + test (quiet)
./dev check -v       # Lint + test (verbose)
./dev lint           # Type check + lint only
./dev test           # Run tests only
./dev test -v        # Run tests (verbose)
\`\`\`

## Review Focus
1. Does the implementation match the task description?
2. Are there any architectural violations (check CLAUDE.md)?
3. Is error handling consistent with existing patterns?
4. Are tests comprehensive?
5. Run \`./dev check\` to verify lint and tests pass.

Start by reading the task description: \`cat .subtask/tasks/${task//\//--}/TASK.md\`
Then review the diff: \`git diff main\`"

    if [ $dry_run -eq 1 ]; then
        echo ""
        echo -e "${BOLD}Review prompt:${NC}"
        echo "----------------------------------------"
        echo "$prompt"
        echo "----------------------------------------"
        echo ""
        echo -e "${BOLD}To launch:${NC} cd $worktree && $agent \"<prompt>\""
    else
        echo ""
        echo -e "${BOLD}Launching $agent in worktree...${NC}"
        echo ""

        case "$agent" in
            codex)
                (cd "$worktree" && exec codex "$prompt")
                ;;
            claude)
                (cd "$worktree" && exec claude "$prompt")
                ;;
            *)
                echo "Unknown agent: $agent (use 'codex' or 'claude')"
                exit 1
                ;;
        esac
    fi
}

main "$@"
