# _lib.sh - Shared utilities for dev scripts
# DESC: Internal library (not a command)
# Usage: source "$(dirname "$0")/_lib.sh"
# Dependencies: none

set -euo pipefail

# Source core libs
_LIB_DIR="$(dirname "${BASH_SOURCE[0]}")/lib"
source "$_LIB_DIR/log.sh"
source "$_LIB_DIR/cli.sh"
source "$_LIB_DIR/paths.sh"

# Project root (one level up from scripts/)
DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
