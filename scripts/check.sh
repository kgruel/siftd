#!/usr/bin/env bash
# DESC: Run lint + test (CI equivalent, quiet by default)
source "$(dirname "$0")/_lib.sh"

usage() {
    cat <<EOF
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
            *) echo "Unknown option: $arg"; exit 1 ;;
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

    echo -e "${GREEN}All checks passed${NC}"
}

main "$@"
