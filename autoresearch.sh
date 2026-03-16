#!/bin/bash
set -euo pipefail

# Use project venv
export PATH="$(pwd)/.venv/bin:$PATH"

# Findability autoresearch: recall@10 for tool-usage queries
# Builds an embeddings DB (limited convs for speed) and measures findability.

DB_PATH="${HOME}/.local/share/siftd/siftd.db"
EMBED_DB="/tmp/autoresearch_findability.db"
MAX_CONVS=50   # limit for speed; ~2-3min build time
STRATEGY="bench/strategies/exchange-window-tool-summaries.json"

# Clean up previous run
rm -f "$EMBED_DB"

echo "Building embeddings DB (max_convs=${MAX_CONVS})..." >&2
python3 bench/build.py \
  --strategy "$STRATEGY" \
  --output "$EMBED_DB" \
  --db "$DB_PATH" \
  --max-convs "$MAX_CONVS"

echo "Running findability benchmark..." >&2
python3 bench/findability.py \
  --embed-db "$EMBED_DB" \
  --main-db "$DB_PATH" \
  --metric \
  --verbose
