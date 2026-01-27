#!/usr/bin/env bash
# Generate CLI help documentation as markdown
set -euo pipefail

OUTPUT="${1:-docs/cli.md}"
mkdir -p "$(dirname "$OUTPUT")"

{
    echo "# strata CLI Reference"
    echo ""
    echo "_Auto-generated from \`--help\` output._"
    echo ""
    echo "## Main"
    echo ""
    echo '```'
    strata --help
    echo '```'

    # Get subcommands from help output
    subcommands=$(strata --help | grep -A20 'positional arguments:' | grep '^\s\s\s\s[a-z]' | awk '{print $1}')

    for cmd in $subcommands; do
        echo ""
        echo "## $cmd"
        echo ""
        echo '```'
        strata "$cmd" --help
        echo '```'
    done
} > "$OUTPUT"

echo "Generated: $OUTPUT"
