#!/usr/bin/env bash
# DESC: Generate docs; --check fails if stale
source "$(dirname "$0")/_lib.sh"

usage() {
    cat <<EOF
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
            *) echo "Unknown option: $arg"; exit 1 ;;
        esac
    done

    ensure_venv
    cd "$DEV_ROOT"

    echo "Generating docs..."
    uv run python scripts/gen_docs.py

    if [ $check_mode -eq 1 ]; then
        # Check if any docs changed
        if ! git diff --quiet docs/reference/; then
            echo -e "${RED}Docs are stale. Run './dev docs' to regenerate.${NC}"
            git diff --stat docs/reference/
            exit 1
        fi
        echo -e "${GREEN}Docs are up to date${NC}"
    fi
}

main "$@"
