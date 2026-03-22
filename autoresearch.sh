#!/bin/bash
set -euo pipefail

# Output layer coverage benchmark
# Primary metric: true_miss + (test_LOC × test_time_s / true_covered) — lower = better
#
# Coverage is measured from the FULL test suite so we don't re-test lines
# already covered by CLI/integration tests. Timing uses only the output-specific
# test files (what we're writing/optimizing).

INCLUDE="src/siftd/output/*"
TEST_FILES="tests/test_output_common.py tests/test_output_formats.py"

# Quick pre-check: syntax errors
for f in $TEST_FILES; do
    [ -f "$f" ] && .venv/bin/python -c "import py_compile; py_compile.compile('$f', doraise=True)"
done

# Count test LOC (non-empty, non-comment lines) across output test files
TEST_LOC=0
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        LOC=$(grep -v '^\s*$' "$f" | grep -v '^\s*#' | wc -l | tr -d ' ')
        TEST_LOC=$((TEST_LOC + LOC))
    fi
done

# --- Step 1: Full suite coverage run (once) to get TRUE miss/covered ---
echo "Running full test suite with output coverage..."
.venv/bin/python -m coverage run --include="$INCLUDE" \
    -m pytest tests/ -x -q --tb=short -p no:xdist \
    --override-ini="addopts=" -m "not embeddings and not serve" 2>&1 | tail -5

COVERAGE_TMPFILE=$(mktemp)
.venv/bin/python -m coverage json -o "$COVERAGE_TMPFILE" --include="$INCLUDE" 2>/dev/null
COVERAGE_JSON=$(cat "$COVERAGE_TMPFILE")
rm -f "$COVERAGE_TMPFILE"

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

# --- Step 2: Median-of-5 timing for output tests only ---
time_one_run() {
    local start end
    start=$(.venv/bin/python -c "import time; print(time.monotonic())")
    .venv/bin/python -m pytest $TEST_FILES -x -q --tb=short -p no:xdist \
        --override-ini="addopts=" -m "not embeddings and not serve" > /dev/null 2>&1
    end=$(.venv/bin/python -c "import time; print(time.monotonic())")
    .venv/bin/python -c "print(round($end - $start, 4))"
}

T1=$(time_one_run)
T2=$(time_one_run)
T3=$(time_one_run)
T4=$(time_one_run)
T5=$(time_one_run)

TEST_TIME=$(.venv/bin/python -c "
times = sorted([$T1, $T2, $T3, $T4, $T5])
print(round(times[2], 3))
")

# --- Step 3: Compute metrics ---
if [ "$COVERED" -eq 0 ]; then
    EFFICIENCY=99999
    SCORE=$MISS
else
    EFFICIENCY=$(.venv/bin/python -c "print(round($TEST_LOC * $TEST_TIME / $COVERED, 2))")
    SCORE=$(.venv/bin/python -c "print(round($MISS + $TEST_LOC * $TEST_TIME / $COVERED, 2))")
fi

echo ""
echo "=== Output Coverage Score ==="
echo "Test LOC:       $TEST_LOC (output tests only)"
echo "Test time:      ${TEST_TIME}s (median of 5: $T1, $T2, $T3, $T4, $T5)"
echo "Covered lines:  $COVERED / $TOTAL (from full suite)"
echo "Missing lines:  $MISS"
echo "Coverage:       ${PCT}%"
echo "Efficiency:     $EFFICIENCY"
echo "Score:          $SCORE (miss=$MISS + efficiency=$EFFICIENCY)"
echo ""
echo "Missing by file:"
echo "$MISSING"
echo ""
echo "METRIC score=$SCORE"
echo "METRIC miss=$MISS"
echo "METRIC coverage_pct=$PCT"
echo "METRIC test_time_s=$TEST_TIME"
echo "METRIC test_loc=$TEST_LOC"
echo "METRIC covered_lines=$COVERED"
echo "METRIC efficiency=$EFFICIENCY"
