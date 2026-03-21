#!/bin/bash
set -euo pipefail
# Run tests (excluding embeddings) — only show failures
.venv/bin/python -m pytest tests/ -x --ignore=tests/test_embeddings.py --ignore=tests/test_embeddings_availability.py --ignore=tests/test_embeddings_storage.py -q --tb=short 2>&1 | tail -30
