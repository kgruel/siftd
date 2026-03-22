#!/bin/bash
set -euo pipefail

# Test coverage efficiency benchmark (stairstep methodology)
#
# Primary metric: efficiency = test_LOC × test_time_s / covered_lines (lower = better)
#
# Keep/discard rule (applied by human, not this script):
#   covered_lines increased → keep (coverage gained)
#   covered_lines unchanged AND efficiency improved → keep
#   otherwise → discard
#
# Coverage measured from full suite. Timing from target tests only.

INCLUDE="src/siftd/output/*"
TEST_FILES="tests/test_output_common.py tests/test_output_formats.py"

# Quick pre-check: syntax errors
for f in $TEST_FILES; do
    [ -f "$f" ] && .venv/bin/python -c "import py_compile; py_compile.compile('$f', doraise=True)"
done

# Count test LOC (non-empty, non-comment lines)
TEST_LOC=0
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        LOC=$(grep -v '^\s*$' "$f" | grep -v '^\s*#' | wc -l | tr -d ' ')
        TEST_LOC=$((TEST_LOC + LOC))
    fi
done

# --- Step 1: Full suite coverage (with xdist) ---
echo "Running full test suite with output coverage..."
.venv/bin/python -m pytest tests/ -x -q --tb=short \
    --cov=src/siftd/output --cov-report=json \
    --override-ini="addopts=" -m "not embeddings and not serve" \
    -k "not test_import_rules and not test_doctor" 2>&1 | tail -5

COVERAGE_JSON=$(cat coverage.json)
rm -f coverage.json
COVERED=$( echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['covered_lines'])")
TOTAL=$(   echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['num_statements'])")
MISS=$(    echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['missing_lines'])")
PCT=$(     echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(round(d['totals']['percent_covered'], 1))")
MISSING=$( echo "$COVERAGE_JSON" | .venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
for fname, fdata in sorted(d['files'].items()):
    lines = fdata.get('missing_lines', [])
    if lines:
        short = fname.replace('src/siftd/output/', '')
        print(f'  {short}: {len(lines)} miss — L{\",\".join(str(l) for l in lines[:10])}{\"...\" if len(lines)>10 else \"\"}')
")

# --- Step 2: Single timed run of target tests ---
START=$(.venv/bin/python -c "import time; print(time.monotonic())")
.venv/bin/python -m pytest $TEST_FILES -x -q --tb=short -p no:xdist \
    --override-ini="addopts=" -m "not embeddings and not serve" 2>&1 | tail -3
END=$(.venv/bin/python -c "import time; print(time.monotonic())")
TEST_TIME=$(.venv/bin/python -c "print(round($END - $START, 3))")

# --- Step 3: Compute metrics ---
if [ "$COVERED" -eq 0 ]; then
    EFFICIENCY=99999
else
    EFFICIENCY=$(.venv/bin/python -c "print(round($TEST_LOC * $TEST_TIME / $COVERED, 2))")
fi

echo ""
echo "=== Coverage Efficiency ==="
echo "Test LOC:       $TEST_LOC"
echo "Test time:      ${TEST_TIME}s"
echo "Covered lines:  $COVERED / $TOTAL"
echo "Missing lines:  $MISS"
echo "Coverage:       ${PCT}%"
echo "Efficiency:     $EFFICIENCY"
echo ""
echo "Missing by file:"
echo "$MISSING"
echo ""
echo "METRIC efficiency=$EFFICIENCY"
echo "METRIC covered_lines=$COVERED"
echo "METRIC coverage_pct=$PCT"
echo "METRIC miss=$MISS"
echo "METRIC test_time_s=$TEST_TIME"
echo "METRIC test_loc=$TEST_LOC"
