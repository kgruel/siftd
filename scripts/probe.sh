#!/usr/bin/env bash
# probe.sh
# DESC: Conservation probe: raw-file counts vs adapter parse() output, per adapter (read-only)
# Usage: ./dev probe [--adapter NAME] [--limit N] [--json]
# Dependencies: uv
# Idempotent: Yes (read-only; calls parse() directly and never opens a siftd database)
source "$(dirname "$0")/lib/dev.sh"

main() {
    cd "$DEV_ROOT"
    ensure_venv
    uv run python scripts/probe.py "$@"
}

main "$@"
