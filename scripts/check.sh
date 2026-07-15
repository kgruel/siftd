#!/usr/bin/env bash
# check.sh
# DESC: Run lint + test + optional lanes (CI equivalent, quiet by default)
# Usage: ./dev check [-v] [--serve] [--embed] [--slow] [--all]
# Dependencies: uv, ty, ruff, pytest
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev check [-v] [--serve] [--embed] [--slow] [--all]

Run lint, test, and optional test lanes (CI equivalent).

Options:
  -v, --verbose  Show verbose output
  --serve        Also run serve-marked tests with serve dependencies
  --embed        Also run embeddings-marked tests with embedding dependencies
  --slow         Also run slow-marked tests
  --all          Also run serve, embeddings, and slow test lanes
  --help         Show this message
EOF
}

main() {
    local verbose=0
    local run_serve=0
    local run_embed=0
    local run_slow=0

    for arg in "$@"; do
        case "$arg" in
            -v|--verbose) verbose=1 ;;
            --serve) run_serve=1 ;;
            --embed) run_embed=1 ;;
            --slow) run_slow=1 ;;
            --all) run_serve=1; run_embed=1; run_slow=1 ;;
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
        uv run pytest tests/ -v --tb=short -m "not embeddings and not serve and not slow" --ignore=tests/architecture/
        if [ $run_serve -eq 1 ]; then
            echo ""
            echo -e "${BOLD}=== Serve Tests ===${NC}"
            ./dev test-serve -v
        fi
        if [ $run_embed -eq 1 ]; then
            echo ""
            echo -e "${BOLD}=== Embedding Tests ===${NC}"
            ./dev test-embed -v
        fi
        if [ $run_slow -eq 1 ]; then
            echo ""
            echo -e "${BOLD}=== Slow Tests ===${NC}"
            ./dev test-slow -v
        fi
        echo ""
        echo -e "${BOLD}=== Docs ===${NC}"
        ./dev docs --check
    else
        # Quiet mode: single line per step, fail-fast
        printf "Lint... "
        ./dev lint > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev lint; exit 1; }
        printf "Spec... "
        uv run pytest tests/architecture/ -q --tb=line > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; uv run pytest tests/architecture/ -v --tb=short; exit 1; }
        printf "Test... "
        uv run pytest tests/ -q --tb=line -m "not embeddings and not serve and not slow" --ignore=tests/architecture/ > /dev/null 2>&1 \
            && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; uv run pytest tests/ -q --tb=short -m "not embeddings and not serve and not slow" --ignore=tests/architecture/; exit 1; }
        if [ $run_serve -eq 1 ]; then
            printf "Serve tests... "
            ./dev test-serve > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev test-serve; exit 1; }
        fi
        if [ $run_embed -eq 1 ]; then
            printf "Embedding tests... "
            ./dev test-embed > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev test-embed; exit 1; }
        fi
        if [ $run_slow -eq 1 ]; then
            printf "Slow tests... "
            ./dev test-slow > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev test-slow; exit 1; }
        fi
        # Docs last: cheapest blast radius, so it never masks a real test failure.
        printf "Docs... "
        ./dev docs --check > /dev/null 2>&1 && echo -e "${GREEN}ok${NC}" || { echo -e "${RED}failed${NC}"; ./dev docs --check; exit 1; }
    fi

    log_success "All checks passed"
}

main "$@"
