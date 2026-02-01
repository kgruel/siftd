#!/usr/bin/env bash
# lib/templates.sh - Template loading and placeholder injection
# Usage: source this file, then call template_read/template_inject
# Dependencies: none

# Load template content from file
# Usage: template_read /path/to/file
template_read() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        return 1
    fi
    cat "$file"
}

# Replace {{KEY}} placeholders in template
# Usage: template_inject "$template" KEY value KEY2 value2
# Note: For multi-line values, use template_inject_env instead
template_inject() {
    local template="$1"
    shift

    local result="$template"
    while [[ $# -gt 1 ]]; do
        local key="$1"
        local val="$2"
        result="${result//\{\{$key\}\}/$val}"
        shift 2
    done

    printf '%s\n' "$result"
}

# Replace {{KEY}} placeholders using environment variables
# Usage: TPL_KEY="value" template_inject_env /path/to/template
# Supports multi-line values safely via Python
template_inject_env() {
    local template_file="$1"
    python3 -c "
import os, sys, re
template = open(sys.argv[1]).read()
def replace(m):
    key = 'TPL_' + m.group(1)
    return os.environ.get(key, m.group(0))
result = re.sub(r'\{\{(\w+)\}\}', replace, template)
print(result, end='')
" "$template_file"
}
