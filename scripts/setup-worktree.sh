#!/usr/bin/env bash
# Setup script for git worktrees (used by subtask workers)
# Ensures venv exists and dependencies are synced
set -euo pipefail

cd "$(dirname "$0")/.."

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating venv..."
    uv venv .venv
fi

# Sync dependencies
echo "Syncing dependencies..."
uv sync --quiet

echo "Worktree ready."
