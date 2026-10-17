#!/usr/bin/env bash
# DESC: Refresh src/siftd/data/pricing.toml from models.dev
# pricing-sync.sh
# Usage: ./dev pricing-sync [--check] [--from FILE] [--as-of YYYY-MM-DD] [--target PATH]
# Dependencies: uv
# Idempotent: Yes (a second run against the same upstream writes nothing)
source "$(dirname "$0")/lib/dev.sh"

main() {
    cd "$DEV_ROOT"
    ensure_venv
    uv run python scripts/pricing_sync.py "$@"
}

main "$@"
