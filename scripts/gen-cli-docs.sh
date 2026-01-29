#!/usr/bin/env bash
# Generate CLI help documentation as markdown
set -euo pipefail

OUTPUT="${1:-docs/cli.md}"
mkdir -p "$(dirname "$OUTPUT")"

# Generate from the in-repo source tree so docs stay in sync with code changes.
CLI=(env PYTHONPATH=src python3 -m siftd.cli)

sanitize_paths() {
    # Replace absolute $HOME paths with ~ for portable docs.
    sed -e "s|$HOME|~|g"
}

{
    echo "# siftd CLI Reference"
    echo ""
    echo "_Auto-generated from \`--help\` output._"
    echo ""
    echo "## Main"
    echo ""
    echo '```'
    "${CLI[@]}" --help | sanitize_paths
    echo '```'

    # Get subcommands from help output
    subcommands=$("${CLI[@]}" --help | grep -A20 'positional arguments:' | grep '^\s\s\s\s[a-z]' | awk '{print $1}')

    for cmd in $subcommands; do
        echo ""
        echo "## $cmd"
        echo ""
        echo '```'
        "${CLI[@]}" "$cmd" --help | sanitize_paths
        echo '```'
    done
} > "$OUTPUT"

echo "Generated: $OUTPUT"
