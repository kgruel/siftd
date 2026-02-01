#!/usr/bin/env bash
# test.sh
# DESC: Run tests (excluding embeddings)
# Usage: ./dev test [-v]
# Dependencies: uv, pytest
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev test [-v]

Run pytest excluding embedding tests.

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

    if [ $verbose -eq 1 ]; then
        uv run pytest tests/ -v --tb=short -m "not embeddings"
    else
        # Quiet mode: minimal output, details only on failure
        log_info "Running tests (excluding embeddings)..."
        set +e
        output=$(uv run pytest tests/ -q --tb=line -m "not embeddings" 2>&1)
        status=$?
        set -e
        if [ $status -ne 0 ]; then
            echo "$output"
            exit 1
        fi
        # Show just the summary line
        echo "$output" | tail -1
    fi
}

main "$@"
