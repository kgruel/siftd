#!/usr/bin/env bash
# DESC: Run all tests including embeddings
source "$(dirname "$0")/_lib.sh"

usage() {
    cat <<EOF
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
            *) echo "Unknown option: $arg"; exit 1 ;;
        esac
    done

    ensure_venv --embed
    cd "$DEV_ROOT"

    if [ $verbose -eq 1 ]; then
        uv run pytest tests/ -v --tb=short
    else
        echo "Running all tests..."
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
