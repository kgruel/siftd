#!/bin/bash
set -euo pipefail

# Storage coverage efficiency benchmark
# Metric: test_LOC × test_time_s / covered_lines (lower = better)

# Quick pre-check: syntax errors in test files
.venv/bin/python -c "
import py_compile, sys
for f in ['tests/test_blobs.py', 'tests/test_storage.py']:
    try:
        py_compile.compile(f, doraise=True)
    except (py_compile.PyCompileError, FileNotFoundError) as e:
        if 'FileNotFoundError' not in type(e).__name__:
            print(f'Syntax error: {e}', file=sys.stderr)
            sys.exit(1)
"

# Coverage source files (excluding embeddings, migrations)
INCLUDE="src/siftd/storage/blobs.py,src/siftd/storage/conversation_stats.py,src/siftd/storage/filters.py,src/siftd/storage/fts.py,src/siftd/storage/queries.py,src/siftd/storage/sessions.py,src/siftd/storage/sql_helpers.py,src/siftd/storage/sqlite.py,src/siftd/storage/tags.py,src/siftd/storage/tool_search.py"

# Test files to measure LOC for
TEST_FILES="tests/test_blobs.py"
if [ -f tests/test_storage.py ]; then
    TEST_FILES="$TEST_FILES tests/test_storage.py"
fi

# Count test LOC (non-empty, non-comment lines)
TEST_LOC=$(cat $TEST_FILES 2>/dev/null | grep -v '^\s*$' | grep -v '^\s*#' | wc -l | tr -d ' ')

# Run tests with coverage, capture timing
START=$(.venv/bin/python -c "import time; print(time.monotonic())")
.venv/bin/python -m coverage run \
    --include="$INCLUDE" \
    -m pytest tests/test_blobs.py tests/test_storage.py \
    -x -q --tb=short -p no:xdist --override-ini="addopts=" \
    -m "not embeddings and not serve" 2>&1 | tail -5
END=$(.venv/bin/python -c "import time; print(time.monotonic())")

TEST_TIME=$(.venv/bin/python -c "print(round($END - $START, 3))")

# Extract coverage stats
# Write coverage JSON to temp file to avoid stderr mixing
COVERAGE_TMPFILE=$(mktemp)
.venv/bin/python -m coverage json -o "$COVERAGE_TMPFILE" --include="$INCLUDE" 2>/dev/null
COVERAGE_JSON=$(cat "$COVERAGE_TMPFILE")
rm -f "$COVERAGE_TMPFILE"
COVERED=$( echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['covered_lines'])")
TOTAL=$(   echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['num_statements'])")
PCT=$(     echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(round(d['totals']['percent_covered'], 1))")

# Compute primary metric: test_LOC * test_time / covered_lines
# Guard against division by zero
if [ "$COVERED" -eq 0 ]; then
    EFFICIENCY=99999
else
    EFFICIENCY=$(.venv/bin/python -c "print(round($TEST_LOC * $TEST_TIME / $COVERED, 2))")
fi

echo ""
echo "=== Storage Coverage Efficiency ==="
echo "Test LOC:       $TEST_LOC"
echo "Test time:      ${TEST_TIME}s"
echo "Covered lines:  $COVERED / $TOTAL"
echo "Coverage:       ${PCT}%"
echo "Efficiency:     $EFFICIENCY"
echo ""
echo "METRIC efficiency=$EFFICIENCY"
echo "METRIC coverage_pct=$PCT"
echo "METRIC test_time_s=$TEST_TIME"
echo "METRIC test_loc=$TEST_LOC"
echo "METRIC covered_lines=$COVERED"
