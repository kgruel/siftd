#!/usr/bin/env bash
# test-all.sh
# DESC: Run all tests including optional extras
# Usage: ./dev test-all [-v]
# Dependencies: uv, pytest, fastembed, litestar
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev test-all [-v]

Run all pytest tests including embedding and serve tests.

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

    local extras="dev embed serve"
    local marker=""
    pytest_lane "$extras" "$marker" "$verbose" "all"
}

main "$@"
