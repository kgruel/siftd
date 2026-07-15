#!/usr/bin/env bash
# test-embed.sh
# DESC: Run embedding tests
# Usage: ./dev test-embed [-v]
# Dependencies: uv, pytest, fastembed, litestar
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

    local extras="dev embed serve"
    local marker="embeddings"
    pytest_lane "$extras" "$marker" "$verbose" "embedding"
}

main "$@"
