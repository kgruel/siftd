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
source "$_LIB_DIR/paths.sh"

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

# Run uv command in project root
# Usage: run_uv <command> [args...]
run_uv() {
    (cd "$DEV_ROOT" && uv "$@")
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
