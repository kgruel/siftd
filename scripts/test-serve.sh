#!/usr/bin/env bash
# test-serve.sh
# DESC: Run serve tests
# Usage: ./dev test-serve [-v]
# Dependencies: uv, pytest, litestar
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev test-serve [-v]

Run pytest tests marked serve with serve dependencies installed.

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

    local extras="dev serve"
    local marker="serve and not embeddings"
    pytest_lane "$extras" "$marker" "$verbose" "serve"
}

main "$@"
