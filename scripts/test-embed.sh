#!/usr/bin/env bash
# test-embed.sh
# DESC: Run embedding tests
# Usage: ./dev test-embed [-v]
# Dependencies: uv, pytest, fastembed
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev test-embed [-v]

Run pytest tests marked embeddings with embedding dependencies installed.

Options:
  -v, --verbose  Show verbose test output
  --help         Show this message
EOF
}

main() {
    local verbose=0

    for arg in "$@"; do
        case "$arg" in
            -v|--verbose) verbose=1 ;;
            --help|-h) usage; exit 0 ;;
            *) cli_unknown_flag "$arg"; exit 1 ;;
        esac
    done

    ensure_venv
    cd "$DEV_ROOT"

    log_info "Installing embedding dependencies..."
    uv sync --extra dev --extra embed --quiet

    if [ $verbose -eq 1 ]; then
        uv run pytest tests/ -v --tb=short -m embeddings
    else
        log_info "Running embedding tests..."
        set +e
        output=$(uv run pytest tests/ -q --tb=line -m embeddings 2>&1)
        status=$?
        set -e
        if [ $status -ne 0 ]; then
            echo "$output"
            exit 1
        fi
        echo "$output" | tail -1
    fi
}

main "$@"
