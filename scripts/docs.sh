#!/usr/bin/env bash
# docs.sh
# DESC: Generate docs; --check fails if stale
# Usage: ./dev docs [--check]
# Dependencies: uv, python3, git
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev docs [--check]

Generate reference documentation.

Options:
  --check  Fail if docs are stale (for CI)
  --help   Show this message
EOF
}

main() {
    local check_mode=0

    for arg in "$@"; do
        case "$arg" in
            --check) check_mode=1 ;;
            --help|-h) usage; exit 0 ;;
            *) cli_unknown_flag "$arg"; exit 1 ;;
        esac
    done

    ensure_venv
    cd "$DEV_ROOT"

    log_info "Generating docs..."
    uv run python scripts/gen_docs.py

    if [ $check_mode -eq 1 ]; then
        # Check if any docs changed
        if ! git diff --quiet docs/reference/; then
            log_error "Docs are stale. Run './dev docs' to regenerate."
            git diff --stat docs/reference/
            exit 1
        fi
        log_success "Docs are up to date"
    fi
}

main "$@"
