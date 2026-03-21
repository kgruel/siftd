#!/bin/bash
set -euo pipefail
# Run tests using project markers to exclude embeddings/serve
.venv/bin/python -m pytest tests/ -x -q --tb=short -m "not embeddings and not serve" 2>&1 | tail -30
