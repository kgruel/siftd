#!/usr/bin/env bash
# agent-close.sh
# DESC: Cleanup agent worktrees after merge
# Usage: ./dev agent-close
# Dependencies: git
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

AGENTS_DIR="$DEV_ROOT/.agents"

usage() {
    cli_usage <<USAGE
Usage: ./dev agent-close

Cleanup merged agent worktrees (from .agents/*/worktree).

Options:
  --help  Show this message
USAGE
}

get_branch_for_worktree() {
    local worktree_path="$1"
    local info="$2"
    echo "$info" | awk -v target="$worktree_path" '
        $1 == "worktree" { wt = $2 }
        $1 == "branch" && wt == target {
            sub("refs/heads/", "", $2)
            print $2
            exit
        }
    '
}

main() {
    for arg in "$@"; do
        case "$arg" in
            --help|-h) usage; exit 0 ;;
            *) cli_unknown_flag "$arg"; exit 1 ;;
        esac
    done

    require_command git

    if [ ! -d "$AGENTS_DIR" ]; then
        log_info "No agent metadata found."
        exit 0
    fi

    shopt -s nullglob
    local worktree_files=("$AGENTS_DIR"/*/worktree)
    shopt -u nullglob

    if [ ${#worktree_files[@]} -eq 0 ]; then
        log_info "No agent worktrees found."
        exit 0
    fi

    local worktree_info
    worktree_info=$(git -C "$DEV_ROOT" worktree list --porcelain)

    local candidates=()
    local branches=()
    local metadata_dirs=()

    local wt_file
    for wt_file in "${worktree_files[@]}"; do
        local worktree_path=""
        if ! read -r worktree_path < "$wt_file"; then
            continue
        fi
        if [ -z "$worktree_path" ]; then
            continue
        fi

        local branch
        branch=$(get_branch_for_worktree "$worktree_path" "$worktree_info")

        if [ -z "$branch" ]; then
            log_warn "No branch found for worktree: $worktree_path"
            continue
        fi

        if [ "$branch" = "main" ]; then
            continue
        fi

        if ! git -C "$DEV_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
            log_warn "Branch not found for worktree: $worktree_path ($branch)"
            continue
        fi

        if git -C "$DEV_ROOT" merge-base --is-ancestor "$branch" main; then
            candidates+=("$worktree_path")
            branches+=("$branch")
            metadata_dirs+=("$(dirname "$wt_file")")
        fi
    done

    if [ ${#candidates[@]} -eq 0 ]; then
        log_info "No merged agent worktrees to remove."
        exit 0
    fi

    log_info "Merged agent worktrees to remove:"
    local i
    for i in "${!candidates[@]}"; do
        echo "  - ${branches[$i]} -> ${candidates[$i]}"
        if [ -d "${metadata_dirs[$i]}" ]; then
            echo "      metadata: ${metadata_dirs[$i]}"
        fi
    done

    echo ""
    local reply=""
    if ! read -r -p "Proceed with removal? [y/N] " reply; then
        log_info "Aborted."
        exit 1
    fi
    case "$reply" in
        y|Y|yes|YES) ;;
        *)
            log_info "Aborted."
            exit 0
            ;;
    esac

    for i in "${!candidates[@]}"; do
        log_info "Removing worktree ${candidates[$i]} (${branches[$i]})"
        if git -C "$DEV_ROOT" worktree remove "${candidates[$i]}"; then
            log_success "Removed worktree ${candidates[$i]}"
            if [ -d "${metadata_dirs[$i]}" ]; then
                rm -rf "${metadata_dirs[$i]}"
                log_success "Removed metadata ${metadata_dirs[$i]}"
            fi
        else
            log_warn "Failed to remove worktree ${candidates[$i]}"
        fi
    done
}

main "$@"
