#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."

# Auto-setup worktree if venv missing (e.g., subtask workers)
if [ ! -d ".venv" ]; then
    ./scripts/setup-worktree.sh
fi

errors=0

# Type check - show only errors/warnings, not progress
ty_out=$(uv run ty check src/ 2>&1)
ty_status=$?
if [ $ty_status -ne 0 ]; then
    echo "$ty_out" | grep -E "^(error|warning)\[" | head -20
    errors=1
fi

# Lint with autofix - show only if issues remain after fix
ruff_out=$(uv run ruff check src/ --fix 2>&1)
ruff_status=$?
if [ $ruff_status -ne 0 ]; then
    echo "$ruff_out" | grep -v "^\[" | head -20
    errors=1
fi

exit $errors
