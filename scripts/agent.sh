#!/usr/bin/env bash
# agent.sh
# DESC: Launch agent in a worktree with prompt template
# Usage: ./dev agent <template> <path> [options]
# Dependencies: git, python3, codex|claude (agent)
# Idempotent: No (launches external process)
source "$(dirname "$0")/lib/dev.sh"
source "$(dirname "$0")/lib/templates.sh"

PROMPTS_DIR="$DEV_ROOT/scripts/prompts"
AGENTS_DIR="$DEV_ROOT/.agents"

# Write agent metadata to .agents/<branch>/
write_agent_metadata() {
    local branch="$1"
    local worktree_path="$2"
    local sanitized=$(echo "$branch" | tr '/' '-')
    local agent_dir="$AGENTS_DIR/$sanitized"

    mkdir -p "$agent_dir"
    echo "$worktree_path" > "$agent_dir/worktree"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$agent_dir/started"
}

# Write session ID after discovery
write_session_id() {
    local branch="$1"
    local session_id="$2"
    local sanitized=$(echo "$branch" | tr '/' '-')
    local agent_dir="$AGENTS_DIR/$sanitized"

    mkdir -p "$agent_dir"
    echo "$session_id" > "$agent_dir/session"
}

# Discover session ID for a workspace (most recent matching agent)
discover_session_id() {
    local workspace="$1"
    local agent="$2"
    # Map agent command to model pattern
    local pattern=""
    case "$agent" in
        codex) pattern="codex" ;;
        claude) pattern="claude" ;;
        *) pattern="" ;;
    esac
    # Use siftd to find the most recent session in this workspace
    if [ -n "$pattern" ]; then
        .venv/bin/siftd peek -w "$workspace" --limit 10 2>/dev/null | grep -i "$pattern" | head -1 | awk '{print $1}'
    else
        .venv/bin/siftd peek -w "$workspace" --limit 1 2>/dev/null | awk 'NR==1 {print $1}'
    fi
}

# Template-specific default focus
get_default_focus() {
    case "$1" in
        review)
            echo "1. Does the implementation match the task description?
2. Are there any architectural violations (check CLAUDE.md)?
3. Is error handling consistent with existing patterns?
4. Are tests comprehensive?
5. Run \`./dev check\` to verify lint and tests pass."
            ;;
        implement)
            echo "Implement the task as described. Follow existing patterns."
            ;;
        plan)
            echo "Design an implementation approach. Consider trade-offs."
            ;;
        research)
            echo "Explore and document. No code changes."
            ;;
        *)
            echo ""
            ;;
    esac
}

usage() {
    # List available templates
    local templates=""
    for f in "$PROMPTS_DIR"/*.md; do
        [ -f "$f" ] || continue
        local name=$(basename "$f" .md)
        templates="$templates $name"
    done

    cli_usage <<EOF
Usage: ./dev agent <template> <path> [options]

Launch an agent in a worktree with a prompt template.

Arguments:
  template             Prompt template:$templates
  path                 Path, branch name, or new branch to create
                       - Directory path: use directly
                       - Existing branch: resolve to its worktree
                       - New branch: create branch + worktree from --base

Options:
  --agent <cmd>        Agent command (default: claude)
  --context <file>     Context file (default: auto-detect TASK.md)
  --focus <text>       Instructions (inline)
  --focus-file <file>  Instructions (from file)
  --base <branch>      Base branch for diff (default: main)
  --background         Launch agent in background (tmux)
  --dry-run            Show expanded prompt without launching
  --help               Show this message

Template variables:
  {{branch}}           Current git branch name
  {{base}}             Base branch name
  {{diff_stat}}        git diff --stat against base
  {{context}}          Contents of context file
  {{focus}}            Instructions
  {{dev_commands}}     Dev harness commands (from ./dev help)

Examples:
  ./dev agent review .                      # Review current directory
  ./dev agent implement impl/foo            # Implement (resolve existing worktree)
  ./dev agent plan impl/new-feature         # Plan (creates branch + worktree)
  ./dev agent research . --focus "How does search work?"
  ./dev agent review . --background         # Launch in tmux background
EOF
}

main() {
    local template=""
    local path=""
    local agent="claude"
    local context_file=""
    local focus=""
    local focus_file=""
    local base_branch="main"
    local background=0
    local dry_run=0

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --agent) cli_require_value "$1" "${2:-}" || exit 1; agent="$2"; shift ;;
            --context) cli_require_value "$1" "${2:-}" || exit 1; context_file="$2"; shift ;;
            --focus) cli_require_value "$1" "${2:-}" || exit 1; focus="$2"; shift ;;
            --focus-file) cli_require_value "$1" "${2:-}" || exit 1; focus_file="$2"; shift ;;
            --base) cli_require_value "$1" "${2:-}" || exit 1; base_branch="$2"; shift ;;
            --background) background=1 ;;
            --dry-run) dry_run=1 ;;
            --help|-h) usage; exit 0 ;;
            -*) cli_unknown_flag "$1"; exit 1 ;;
            *)
                if [ -z "$template" ]; then
                    template="$1"
                elif [ -z "$path" ]; then
                    path="$1"
                else
                    log_error "Unexpected argument: $1"
                    exit 1
                fi
                ;;
        esac
        shift
    done

    # Validate template
    if [ -z "$template" ]; then
        usage
        exit 1
    fi

    local prompt_file="$PROMPTS_DIR/$template.md"
    if [ ! -f "$prompt_file" ]; then
        log_error "Unknown template: $template"
        log_info "Available templates:"
        for f in "$PROMPTS_DIR"/*.md; do
            [ -f "$f" ] || continue
            echo "  $(basename "$f" .md)"
        done
        exit 1
    fi

    if [ -z "$path" ]; then
        usage
        exit 1
    fi

    # Resolve path: directory, branch name, or create new worktree
    if [ -d "$path" ]; then
        path=$(cd "$path" && pwd)
    else
        # Try to find worktree by branch name
        local worktree_path
        worktree_path=$(git worktree list | grep -E "\[$path\]$" | awk '{print $1}' || true)
        if [ -n "$worktree_path" ] && [ -d "$worktree_path" ]; then
            path="$worktree_path"
        else
            # Create new branch and worktree
            local branch_name="$path"
            local repo_name=$(basename "$(git rev-parse --show-toplevel)")
            local sanitized=$(echo "$branch_name" | tr '/' '-')
            local worktree_dir="$(dirname "$(git rev-parse --show-toplevel)")/${repo_name}-${sanitized}"

            log_info "Creating worktree for branch '$branch_name'..."

            # Create branch if it doesn't exist
            if ! git show-ref --verify --quiet "refs/heads/$branch_name"; then
                git branch "$branch_name" "$base_branch"
            fi

            # Create worktree
            git worktree add "$worktree_dir" "$branch_name"
            path="$worktree_dir"
        fi
    fi

    # Verify it's a git repo
    if ! git -C "$path" rev-parse --git-dir >/dev/null 2>&1; then
        log_error "'$path' is not a git repository"
        exit 1
    fi

    # Get branch name
    local branch
    branch=$(git -C "$path" branch --show-current 2>/dev/null || echo "detached")

    # Write agent metadata (worktree path + timestamp)
    write_agent_metadata "$branch" "$path"

    # Get diff stat
    local diff_stat
    diff_stat=$(git -C "$path" diff --stat "$base_branch" 2>/dev/null || echo "No changes from $base_branch")

    # Auto-detect context file if not specified
    if [ -z "$context_file" ]; then
        for candidate in "$path/TASK.md" "$path/PLAN.md" "$path/.claude/context.md" "$path/README.md"; do
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
        context="No context file found."
    fi

    # Resolve focus: --focus > --focus-file > template default
    if [ -z "$focus" ]; then
        if [ -n "$focus_file" ] && [ -f "$focus_file" ]; then
            focus=$(cat "$focus_file")
        else
            focus=$(get_default_focus "$template")
        fi
    fi

    # Get dev commands (if ./dev exists in worktree)
    local dev_commands=""
    if [ -x "$path/dev" ]; then
        dev_commands=$("$path/dev" help 2>/dev/null | grep -A100 "^Commands:" | tail -n +2 | head -20 || echo "")
    fi
    if [ -z "$dev_commands" ]; then
        dev_commands="./dev check    # Lint + test"
    fi

    # Read and expand template
    local prompt
    prompt=$(TPL_branch="$branch" \
             TPL_base="$base_branch" \
             TPL_diff_stat="$diff_stat" \
             TPL_context="$context" \
             TPL_focus="$focus" \
             TPL_dev_commands="$dev_commands" \
             template_inject_env "$prompt_file")

    # Output
    echo -e "${BOLD}Template:${NC} $template"
    echo -e "${BOLD}Path:${NC} $path"
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
        if ! command -v tmux &>/dev/null; then
            log_error "tmux required for --background"
            exit 1
        fi
        local session_name="agent-${template}-${branch//\//-}"

        # Write prompt to temp file (avoids quoting issues with tmux)
        local prompt_file=$(mktemp)
        printf '%s' "$prompt" > "$prompt_file"

        log_info "Launching $agent in tmux session: $session_name"
        tmux new-session -d -s "$session_name" -c "$path" \
            "$agent \"\$(cat '$prompt_file')\" ; rm '$prompt_file'"

        # Wait briefly for session to start, then discover and record session ID
        local workspace=$(basename "$path")
        (
            sleep 2
            local session_id=$(discover_session_id "$workspace" "$agent")
            if [ -n "$session_id" ]; then
                write_session_id "$branch" "$session_id"
            fi
        ) &

        echo "Attach with: tmux attach -t $session_name"
        echo "Monitor with: siftd peek -w $workspace"
        echo "Session will be recorded to: .agents/${branch//\//-}/session"
    else
        log_info "Launching $agent..."
        (cd "$path" && exec $agent "$prompt")
    fi
}

main "$@"
