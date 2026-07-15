#!/usr/bin/env bash
# lib/dev.sh - Project-specific dev script utilities
# Usage: source "$(dirname "$0")/lib/dev.sh"
# Dependencies: none
#
# Sources all generic libs and adds siftd-specific helpers.
# This is the single entry point for dev scripts.

set -euo pipefail

# Source generic libs
_LIB_DIR="$(dirname "${BASH_SOURCE[0]}")"
source "$_LIB_DIR/log.sh"
source "$_LIB_DIR/cli.sh"

# Project root (two levels up from scripts/lib/)
DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Ensure venv exists, auto-setup if missing
# Usage: ensure_venv [--embed]
ensure_venv() {
    if [ ! -d "$DEV_ROOT/.venv" ]; then
        log_info "Venv missing, running setup..."
        "$DEV_ROOT/dev" setup "$@"
    fi
}

# Check that a command exists, with install hint
# Usage: require_command <name> <install_hint>
require_command() {
    local name="$1"
    local hint="${2:-}"
    if ! command -v "$name" &>/dev/null; then
        log_error "$name not found${hint:+. Install with: $hint}"
        exit 1
    fi
}

# Run a pytest lane with optional dependency extras.
# Usage: pytest_lane <extras> <marker-expression> <verbose> <label> [pytest args...]
# extras is a space-separated list and may be empty; marker-expression may be empty.
pytest_lane() {
    local extras="$1"
    local marker="$2"
    local verbose="$3"
    local label="$4"
    shift 4
    local -a sync_args=()
    local -a pytest_args=(tests/)

    ensure_venv
    cd "$DEV_ROOT"

    if [ -n "$extras" ]; then
        local extra
        for extra in $extras; do
            sync_args+=(--extra "$extra")
        done
        log_info "Installing $label dependencies..."
        uv sync "${sync_args[@]}" --quiet
    fi

    if [ -n "$marker" ]; then
        pytest_args+=(-m "$marker")
    fi
    pytest_args+=("$@")

    if [ "$verbose" -eq 1 ]; then
        uv run pytest "${pytest_args[@]}" -v --tb=short
        return
    fi

    log_info "Running $label tests..."
    local output status
    set +e
    output=$(uv run pytest "${pytest_args[@]}" -q --tb=line 2>&1)
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        echo "$output"
        return "$status"
    fi
    echo "$output" | tail -1
}
