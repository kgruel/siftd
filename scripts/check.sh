#!/usr/bin/env bash
# check.sh
# DESC: Run lint + test (CI equivalent, quiet by default)
# Usage: ./dev check [-v]
# Dependencies: uv, ty, ruff, pytest
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev check [-v]

Run lint and test (CI equivalent).

Options:
  -v, --verbose  Show verbose output
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

    cd "$DEV_ROOT"

    if [ $verbose -eq 1 ]; then
        echo -e "${BOLD}=== Lint ===${NC}"
        ./dev lint
        echo ""
        echo -e "${BOLD}=== Test ===${NC}"
        ./dev test -v
    else
        # Quiet mode: single line per step
        printf "Lint... "
        ./dev lint > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev lint; exit 1; }
        printf "Test... "
        ./dev test > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev test; exit 1; }
    fi

    log_success "All checks passed"
}

main "$@"
