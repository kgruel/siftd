#!/bin/bash
set -euo pipefail

# adapters.registry coverage efficiency benchmark

INCLUDE_ARGS="--cov=siftd.adapters.registry"
TARGET_FILE="src/siftd/adapters/registry.py"
TEST_FILES="tests/adapters/test_registry_edges.py"

for f in $TEST_FILES; do
    uv run python -c "import py_compile; py_compile.compile('$f', doraise=True)"
done

TEST_LOC=0
for f in $TEST_FILES; do
    LOC=$(grep -v '^\s*$' "$f" | grep -v '^\s*#' | wc -l | tr -d ' ')
    TEST_LOC=$((TEST_LOC + LOC))
done

echo "Running full test suite with coverage..."
uv run python -m pytest tests/ -x -q --tb=short -p no:randomly \
    $INCLUDE_ARGS --cov-report=json:coverage.json \
    --override-ini="addopts=" -m "not embeddings and not serve" \
    -k "not test_import_rules and not test_basics and not test_follow_session and not test_doctor_fix_shows_fix_commands" 2>&1 | tail -5

COVERAGE_JSON=$(cat coverage.json)
rm -f coverage.json
COVERED=$( echo "$COVERAGE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); tf='$TARGET_FILE'; f=next((v for k,v in d['files'].items() if k.endswith(tf)), None); s=(f or d['totals']); print(s['summary']['covered_lines'] if 'summary' in s else s['covered_lines'])")
TOTAL=$(   echo "$COVERAGE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); tf='$TARGET_FILE'; f=next((v for k,v in d['files'].items() if k.endswith(tf)), None); s=(f or d['totals']); print(s['summary']['num_statements'] if 'summary' in s else s['num_statements'])")
MISS=$(    echo "$COVERAGE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); tf='$TARGET_FILE'; f=next((v for k,v in d['files'].items() if k.endswith(tf)), None); s=(f or d['totals']); print(s['summary']['missing_lines'] if 'summary' in s else s['missing_lines'])")
PCT=$(     echo "$COVERAGE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); tf='$TARGET_FILE'; f=next((v for k,v in d['files'].items() if k.endswith(tf)), None); s=(f or d['totals']); p=(s['summary']['percent_covered'] if 'summary' in s else s['percent_covered']); print(round(p, 1))")
MISSING=$( echo "$COVERAGE_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
tf='$TARGET_FILE'
f=next((v for k,v in d['files'].items() if k.endswith(tf)), None)
lines=(f or {}).get('missing_lines', [])
if lines:
    print('  L' + ','.join(str(l) for l in lines))
")

BEST_TIME=99999
for i in 1 2 3 4 5; do
    START=$(python3 -c "import time; print(time.monotonic())")
    uv run python -m pytest $TEST_FILES -x -q --tb=short -p no:xdist \
        --override-ini="addopts=" -m "not embeddings and not serve" 2>&1 | tail -1
    END=$(python3 -c "import time; print(time.monotonic())")
    RUN_TIME=$(python3 -c "print(round($END - $START, 3))")
    BEST_TIME=$(python3 -c "print(min($BEST_TIME, $RUN_TIME))")
done
TEST_TIME=$BEST_TIME

if [ "$COVERED" -eq 0 ]; then
    EFFICIENCY=99999
else
    EFFICIENCY=$(python3 -c "print(round($TEST_LOC * $TEST_TIME / $COVERED, 2))")
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
echo "Missing:"
echo "$MISSING"
echo ""
echo "METRIC efficiency=$EFFICIENCY"
echo "METRIC covered_lines=$COVERED"
echo "METRIC coverage_pct=$PCT"
echo "METRIC miss=$MISS"
echo "METRIC test_time_s=$TEST_TIME"
echo "METRIC test_loc=$TEST_LOC"
