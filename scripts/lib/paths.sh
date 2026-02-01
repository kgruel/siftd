#!/usr/bin/env bash
# lib/paths.sh - Script and XDG path helpers
# Usage: source this file, then call paths_init
# Dependencies: none

# Initialize script path variables
# Usage: paths_init "$0" or paths_init "${BASH_SOURCE[0]}"
# Sets: SCRIPT_PATH, SCRIPT_DIR, SCRIPT_NAME
paths_init() {
    local script_path="${1:-$0}"
    SCRIPT_PATH="$(cd "$(dirname "$script_path")" && pwd)/$(basename "$script_path")"
    SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
    SCRIPT_NAME="$(basename "$SCRIPT_PATH")"
}

xdg_config_home() {
    echo "${XDG_CONFIG_HOME:-$HOME/.config}"
}

xdg_cache_home() {
    echo "${XDG_CACHE_HOME:-$HOME/.cache}"
}

xdg_data_home() {
    echo "${XDG_DATA_HOME:-$HOME/.local/share}"
}

xdg_state_home() {
    echo "${XDG_STATE_HOME:-$HOME/.local/state}"
}
