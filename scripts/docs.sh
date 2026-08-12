#!/usr/bin/env bash
# docs.sh
# DESC: Generate docs; --check fails if the result is not staged or committed
# Usage: ./dev docs [--check]
# Dependencies: uv, python3, git
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev docs [--check]

Generate reference docs (docs/reference/) and the generated spans of the
managed per-folder READMEs (see scripts/gen_docs.py MANIFEST).

Options:
  --check  Regenerate strictly (a skipped target is a hard failure) and fail
           if the result is not already staged or committed (for CI)
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
    if [ $check_mode -eq 1 ]; then
        # Strict: a skipped target (e.g. api.md without optional deps) is a false
        # green under --check, so fail instead of degrading gracefully.
        uv run python scripts/gen_docs.py --strict
    else
        uv run python scripts/gen_docs.py
    fi

    if [ $check_mode -eq 1 ]; then
        # Diff the generated reference docs and the managed READMEs explicitly —
        # the README paths come from the manifest, no repo-wide diff.
        local readme_paths
        readme_paths=$(uv run python scripts/gen_docs.py readmes --list)
        if ! git diff --quiet docs/reference/ $readme_paths; then
            # --check regenerated above, so the working tree is fresh by
            # construction and re-running './dev docs' can never change this
            # outcome. The diff is against the index, so what this asserts is
            # that generated docs travel with the source they describe.
            log_error "Generated docs are not staged — 'git add' the files below to include them."
            git diff --stat docs/reference/ $readme_paths
            exit 1
        fi
        log_success "Docs are up to date and staged"
    fi
}

main "$@"
