#!/usr/bin/env bash
# lib/cli.sh - CLI argument parsing and usage helpers
# Usage: source this file, then call helpers in arg parsing
# Dependencies: none

# Print error to stderr
cli_error() {
    echo "Error: $*" >&2
}

# Standard unknown flag error
cli_unknown_flag() {
    cli_error "Unknown flag: $1"
    return 1
}

# Enforce required flag value
# Usage: cli_require_value "--flag" "$value" || exit 1
cli_require_value() {
    local flag="$1"
    local value="$2"
    local message="${3:-Error: $flag requires a value}"
    if [[ -z "$value" ]]; then
        echo "$message" >&2
        return 1
    fi
    return 0
}

# Print usage text from stdin to stdout
# Usage: cli_usage <<EOF ... EOF
cli_usage() {
    cat
}

# Print an error message (optional) and usage text to stderr
# Usage: cli_usage_error "Error message" <<EOF ... EOF
cli_usage_error() {
    local message="${1:-}"
    if [[ -n "$message" ]]; then
        echo "$message" >&2
    fi
    cat >&2
}
