#!/usr/bin/env bash
# browser-smoke.sh
# DESC: Run the real-browser CSP smoke (T3) against a from-source server
# Usage: ./dev browser-smoke [--remote] [--artifacts DIR]
# Dependencies: uv, chromium (local mode only), serve extra
# Idempotent: Yes
source "$(dirname "$0")/lib/dev.sh"

usage() {
    cli_usage <<EOF
Usage: ./dev browser-smoke [--remote] [--artifacts DIR]

Run the T3 real-browser CSP smoke (tests/browser_smoke/smoke.py): build a
fixture DB, serve it from source, drive a browser with real input events, and
fail on any CSP violation. Method + tier rationale:
docs/guides/serve-browser-testing.md.

Without --remote, launch local Chromium over CDP as before. Its isolated
profile uses Chromium's mock Keychain so the smoke never consults a real macOS
Keychain.

--remote is explicit opt-in. It connects native Playwright to the caller's
Browserless endpoint and reaches only the owned fixture through one temporary
SSH reverse forward, bound to loopback at both ends. It requires:
  SIFTD_BROWSER_SMOKE_ENDPOINT    secure native-Playwright wss:// endpoint
  SIFTD_BROWSER_SMOKE_SSH_TARGET  SSH destination as user@host

Environment:
  CHROMIUM_BIN          Local Chromium binary (local mode only)
  SIFTD_SMOKE_PORT      Local server port (default: 8378; local mode only)
  SIFTD_SMOKE_CDP_PORT  Local Chromium debugging port (default: 9378)

Options:
  --remote        Use the explicit Browserless + private SSH fixture route
  --artifacts DIR  Persist remote fixture-only receipt/events in DIR
  --help           Show this message
EOF
}

main() {
    local remote=0
    local -a smoke_args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --remote) remote=1; smoke_args+=("$1") ;;
            --artifacts)
                if [ "$#" -lt 2 ]; then
                    log_error "--artifacts requires a directory"
                    exit 1
                fi
                smoke_args+=("$1" "$2")
                shift
                ;;
            --help|-h) usage; exit 0 ;;
            *) cli_unknown_flag "$1"; exit 1 ;;
        esac
        shift
    done

    ensure_venv
    cd "$DEV_ROOT"

    local -a extras=(--extra dev --extra serve)
    if [ "$remote" -eq 1 ]; then
        extras+=(--extra browser)
    fi
    log_info "Installing browser-smoke dependencies..."
    uv sync "${extras[@]}" --quiet

    log_info "Running browser CSP smoke..."
    if [ "${#smoke_args[@]}" -gt 0 ]; then
        SIFTD_NO_UPDATE_CHECK=1 uv run python tests/browser_smoke/smoke.py "${smoke_args[@]}"
    else
        SIFTD_NO_UPDATE_CHECK=1 uv run python tests/browser_smoke/smoke.py
    fi
}

main "$@"
