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
        echo -e "${BOLD}=== Spec ===${NC}"
        uv run pytest tests/architecture/ -v --tb=short
        echo ""
        echo -e "${BOLD}=== Test ===${NC}"
        uv run pytest tests/ -v --tb=short -m "not embeddings and not serve" --ignore=tests/architecture/
    else
        # Quiet mode: single line per step, fail-fast
        printf "Lint... "
        ./dev lint > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev lint; exit 1; }
        printf "Spec... "
        uv run pytest tests/architecture/ -q --tb=line > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; uv run pytest tests/architecture/ -v --tb=short; exit 1; }
        printf "Test... "
        uv run pytest tests/ -q --tb=line -m "not embeddings and not serve" --ignore=tests/architecture/ > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; uv run pytest tests/ -q --tb=short -m "not embeddings and not serve" --ignore=tests/architecture/; exit 1; }
    fi

    log_success "All checks passed"
}

main "$@"
