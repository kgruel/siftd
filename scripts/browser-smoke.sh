#!/usr/bin/env bash
# browser-smoke.sh
# DESC: Run the real-browser CSP smoke (T3) against a from-source server
# Usage: ./dev browser-smoke
# Dependencies: uv, chromium (or CHROMIUM_BIN), serve extra
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev browser-smoke

Run the T3 real-browser CSP smoke (tests/browser_smoke/smoke.py): build a
fixture DB, serve it from source, drive headless Chromium over CDP with real
input events, and fail on any CSP violation. Method + tier rationale:
docs/guides/serve-browser-testing.md.

Environment:
  CHROMIUM_BIN          Chromium binary (default: found on PATH / homebrew)
  SIFTD_SMOKE_PORT      Server port (default: 8378)
  SIFTD_SMOKE_CDP_PORT  Chromium debugging port (default: 9378)

Options:
  --help         Show this message
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

    log_info "Installing serve dependencies..."
    uv sync --extra dev --extra serve --quiet

    log_info "Running browser CSP smoke..."
    uv run python tests/browser_smoke/smoke.py
}

main "$@"
