#!/usr/bin/env bash
set -euo pipefail
# Correctness checks: lint + tests (no embeddings). Only errors shown.
./dev check 2>&1 | grep -iE "error|fail|FAILED" | grep -v "^$" || true
