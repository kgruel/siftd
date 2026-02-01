#!/usr/bin/env bash
# setup.sh
# DESC: Setup worktree (venv, deps, optional embeddings)
# Usage: ./dev setup [--embed]
# Dependencies: uv, python3
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev setup [--embed]

Setup the development environment.

Options:
  --embed    Also install embeddings dependencies and warm cache
  --help     Show this message
EOF
}

main() {
    local with_embed=0

    for arg in "$@"; do
        case "$arg" in
            --embed) with_embed=1 ;;
            --help|-h) usage; exit 0 ;;
            *) cli_unknown_flag "$arg"; exit 1 ;;
        esac
    done

    cd "$DEV_ROOT"

    # Create venv if missing
    if [ ! -d ".venv" ]; then
        log_info "Creating venv..."
        uv venv .venv
    fi

    # Sync dev dependencies
    log_info "Syncing dependencies..."
    uv sync --extra dev --quiet

    # Optional: embeddings setup
    if [ $with_embed -eq 1 ]; then
        log_info "Installing embeddings dependencies..."
        uv sync --extra dev --extra embed --quiet

        log_info "Warming fastembed cache (downloading model)..."
        uv run python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" 2>/dev/null || {
            log_warn "Could not warm fastembed cache"
        }

        log_info "Running initial ingest..."
        uv run siftd ingest || {
            log_warn "Ingest had issues (may be first run)"
        }
    fi

    log_success "Worktree ready. Run ./dev check to verify."
}

main "$@"
