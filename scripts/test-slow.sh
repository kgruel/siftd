#!/usr/bin/env bash
# test-slow.sh
# DESC: Run slow tests
# Usage: ./dev test-slow [-v]
# Dependencies: uv, pytest
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev test-slow [-v]

Run pytest tests marked slow.

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

    local extras=""
    local marker="slow"
    pytest_lane "$extras" "$marker" "$verbose" "slow"
}

main "$@"
