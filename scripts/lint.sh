#!/usr/bin/env bash
# lint.sh
# DESC: Run ty type checker + ruff linter (with autofix)
# Usage: ./dev lint
# Dependencies: uv, ty, ruff
# Idempotent: Yes
source "$(dirname "$0")/_lib.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev lint

Run type checking (ty) and linting (ruff) with autofix.
EOF
}

main() {
    for arg in "$@"; do
        case "$arg" in
            --help|-h) usage; exit 0 ;;
            *) cli_unknown_flag "$arg"; exit 1 ;;
        esac
    done

    ensure_venv
    cd "$DEV_ROOT"

    local errors=0

    # Type check - show only errors/warnings
    log_info "Running ty..."
    set +e
    ty_out=$(uv run ty check src/ 2>&1)
    ty_status=$?
    set -e

    if [ $ty_status -ne 0 ]; then
        echo "$ty_out" | grep -E "^(error|warning)\[" | head -20
        errors=1
    fi

    # Lint with autofix
    log_info "Running ruff..."
    set +e
    ruff_out=$(uv run ruff check src/ --fix 2>&1)
    ruff_status=$?
    set -e

    if [ $ruff_status -ne 0 ]; then
        echo "$ruff_out" | grep -v "^\[" | head -20
        errors=1
    fi

    if [ $errors -eq 0 ]; then
        log_success "Lint passed"
    else
        log_error "Lint failed"
        exit 1
    fi
}

main "$@"
