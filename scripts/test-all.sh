#!/usr/bin/env bash
# test-all.sh
# DESC: Run all tests including embeddings
# Usage: ./dev test-all [-v]
# Dependencies: uv, pytest, fastembed
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev test-all [-v]

Run all pytest tests including embedding tests.

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

    ensure_venv --embed
    cd "$DEV_ROOT"

    if [ $verbose -eq 1 ]; then
        uv run pytest tests/ -v --tb=short
    else
        log_info "Running all tests..."
        set +e
        output=$(uv run pytest tests/ -q --tb=line 2>&1)
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
