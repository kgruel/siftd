# Shared utilities for dev scripts
# Source this at the top of each script: source "$(dirname "$0")/_lib.sh"

set -euo pipefail

# Colors (disabled if not a terminal)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BOLD='' NC=''
fi

# Project root (one level up from scripts/)
DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Ensure venv exists, auto-setup if missing
# Usage: ensure_venv [--embed]
ensure_venv() {
    if [ ! -d "$DEV_ROOT/.venv" ]; then
        echo "Venv missing, running setup..."
        "$DEV_ROOT/dev" setup "$@"
    fi
}

# Run command in project root with venv
# Usage: run_uv <command> [args...]
run_uv() {
    (cd "$DEV_ROOT" && uv "$@")
}
