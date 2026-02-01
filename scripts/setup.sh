#!/usr/bin/env bash
# DESC: Setup worktree (venv, deps, optional embeddings)
source "$(dirname "$0")/_lib.sh"

usage() {
    cat <<EOF
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
            *) echo "Unknown option: $arg"; exit 1 ;;
        esac
    done

    cd "$DEV_ROOT"

    # Create venv if missing
    if [ ! -d ".venv" ]; then
        echo "Creating venv..."
        uv venv .venv
    fi

    # Sync dev dependencies
    echo "Syncing dependencies..."
    uv sync --extra dev --quiet

    # Optional: embeddings setup
    if [ $with_embed -eq 1 ]; then
        echo "Installing embeddings dependencies..."
        uv sync --extra dev --extra embed --quiet

        echo "Warming fastembed cache (downloading model)..."
        uv run python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" 2>/dev/null || {
            echo -e "${YELLOW}Warning: Could not warm fastembed cache${NC}"
        }

        echo "Running initial ingest..."
        uv run siftd ingest || {
            echo -e "${YELLOW}Warning: Ingest had issues (may be first run)${NC}"
        }
    fi

    echo -e "${GREEN}Worktree ready.${NC} Run ./dev check to verify."
}

main "$@"
