#!/usr/bin/env bash
# Generate CLI help documentation as markdown
set -euo pipefail

OUTPUT="${1:-docs/cli.md}"
mkdir -p "$(dirname "$OUTPUT")"

{
    echo "# tbd CLI Reference"
    echo ""
    echo "_Auto-generated from \`--help\` output._"
    echo ""
    echo "## Main"
    echo ""
    echo '```'
    tbd --help
    echo '```'

    # Get subcommands from help output
    subcommands=$(tbd --help | grep -A20 'positional arguments:' | grep '^\s\s\s\s[a-z]' | awk '{print $1}')

    for cmd in $subcommands; do
        echo ""
        echo "## $cmd"
        echo ""
        echo '```'
        tbd "$cmd" --help
        echo '```'
    done
} > "$OUTPUT"

echo "Generated: $OUTPUT"
