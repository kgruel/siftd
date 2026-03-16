#!/bin/bash
set -euo pipefail

# Findability autoresearch: recall@10 for tool-usage queries
# Builds an embeddings DB (limited convs for speed) and measures findability.

DB_PATH="${HOME}/.local/share/siftd/siftd.db"
EMBED_DB="/tmp/autoresearch_findability.db"
MAX_CONVS=500  # limit for speed; ~30s build time
STRATEGY="bench/strategies/exchange-window.json"

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
