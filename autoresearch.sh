#!/bin/bash
set -euo pipefail

# VSCode adapter coverage efficiency benchmark
# Metric: test_LOC × test_time_s / covered_lines (lower = better)
# Uses median-of-5 timing (all with coverage) for stability

# Quick pre-check: syntax errors
.venv/bin/python -c "
import py_compile, sys
py_compile.compile('tests/adapters/test_vscode.py', doraise=True)
"

INCLUDE="src/siftd/adapters/vscode.py"
TEST_FILE="tests/adapters/test_vscode.py"

# Count test LOC (non-empty, non-comment lines)
TEST_LOC=$(grep -v '^\s*$' "$TEST_FILE" | grep -v '^\s*#' | wc -l | tr -d ' ')

# Helper: run once with coverage, print elapsed time
time_one_run() {
    local start end
    start=$(.venv/bin/python -c "import time; print(time.monotonic())")
    .venv/bin/python -m coverage run --include="$INCLUDE" \
        -m pytest "$TEST_FILE" -x -q --tb=short -p no:xdist \
        --override-ini="addopts=" -m "not embeddings and not serve" > /dev/null 2>&1
    end=$(.venv/bin/python -c "import time; print(time.monotonic())")
    .venv/bin/python -c "print(round($end - $start, 4))"
}

# Run 1 — show output (for pass/fail detection)
START1=$(.venv/bin/python -c "import time; print(time.monotonic())")
.venv/bin/python -m coverage run --include="$INCLUDE" \
    -m pytest "$TEST_FILE" -x -q --tb=short -p no:xdist \
    --override-ini="addopts=" -m "not embeddings and not serve" 2>&1 | tail -5
END1=$(.venv/bin/python -c "import time; print(time.monotonic())")
T1=$(.venv/bin/python -c "print(round($END1 - $START1, 4))")

# Runs 2-5 — silent
T2=$(time_one_run)
T3=$(time_one_run)
T4=$(time_one_run)
T5=$(time_one_run)

# Median of 5 runs
TEST_TIME=$(.venv/bin/python -c "
times = sorted([$T1, $T2, $T3, $T4, $T5])
print(round(times[2], 3))
")

# Extract coverage stats
COVERAGE_TMPFILE=$(mktemp)
.venv/bin/python -m coverage json -o "$COVERAGE_TMPFILE" --include="$INCLUDE" 2>/dev/null
COVERAGE_JSON=$(cat "$COVERAGE_TMPFILE")
rm -f "$COVERAGE_TMPFILE"
COVERED=$( echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['covered_lines'])")
TOTAL=$(   echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(d['totals']['num_statements'])")
PCT=$(     echo "$COVERAGE_JSON" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(round(d['totals']['percent_covered'], 1))")
MISSING=$( echo "$COVERAGE_JSON" | .venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
for f in d['files'].values():
    lines = f.get('missing_lines', [])
    print(','.join(str(l) for l in lines) if lines else 'none')
")

# Compute primary metric: test_LOC * test_time / covered_lines
if [ "$COVERED" -eq 0 ]; then
    EFFICIENCY=99999
else
    EFFICIENCY=$(.venv/bin/python -c "print(round($TEST_LOC * $TEST_TIME / $COVERED, 2))")
fi

# Pass/fail logic:
#   Normal zone (<90% coverage): efficiency must improve
#   Edge zone (>=90%): pass if coverage improved >=1% even if efficiency regressed up to 25%
ZONE=$(.venv/bin/python -c "print('edge' if $PCT >= 90 else 'normal')")

echo ""
echo "=== VSCode Adapter Coverage Efficiency ==="
echo "Test LOC:       $TEST_LOC"
echo "Test time:      ${TEST_TIME}s (median of 5: $T1, $T2, $T3, $T4, $T5)"
echo "Covered lines:  $COVERED / $TOTAL"
echo "Coverage:       ${PCT}%"
echo "Missing lines:  $MISSING"
echo "Efficiency:     $EFFICIENCY"
echo "Zone:           $ZONE (normal <90%, edge >=90%)"
echo ""
echo "METRIC efficiency=$EFFICIENCY"
echo "METRIC coverage_pct=$PCT"
echo "METRIC test_time_s=$TEST_TIME"
echo "METRIC test_loc=$TEST_LOC"
echo "METRIC covered_lines=$COVERED"
echo "METRIC zone_is_edge=$( [ "$ZONE" = "edge" ] && echo 1 || echo 0 )"
