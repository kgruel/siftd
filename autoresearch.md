# Autoresearch: Test Coverage Methodology

## Metric
**Primary**: `efficiency` (lower is better) = `test_LOC × test_time_s / covered_lines`

Rewards concise, fast tests that cover more source lines. Encourages shared fixtures,
integration tests, and clean test structure over bloated one-offs.

**Secondary** (for monitoring):
- `covered_lines` — absolute count of covered source lines (drives keep/discard rule)
- `coverage_pct` — percentage of target lines covered
- `miss` — number of uncovered source lines
- `test_loc` — lines of test code
- `test_time_s` — seconds to run tests

## Keep/Discard: Stairstep Rule

The decision checks **Δcovered first**, then efficiency:

```
if covered_lines > previous_covered:
    keep    # coverage gained — always accept
elif efficiency improved:
    keep    # tests got tighter without losing coverage
else:
    discard # nothing improved
```

No zones, no thresholds, no composite formulas.

### Why this works

Coverage gains and efficiency gains are different activities with a natural rhythm:

1. **Step up** — Add tests covering new lines. Efficiency gets worse (more LOC for
   hard-to-reach edges). Keep anyway: coverage ratcheted up.
2. **Step down** — Compress, extract fixtures, share helpers, merge tests. Coverage stays
   the same. Efficiency improves.
3. **Repeat** — Coverage never goes back. Efficiency oscillates but trends down.

The efficiency metric still serves its purpose: it pushes toward shared fixtures,
integration-style tests, and clean structure. But it can never veto real coverage gains.

### Edge cases

- **Padding** (useless LOC, no new coverage): covered unchanged, efficiency worse → discard ✓
- **Expensive edge coverage** (20 LOC for 2 lines): covered increased → keep ✓
- **Pure compression** (same coverage, fewer LOC): covered unchanged, efficiency better → keep ✓
- **Delete tests**: covered decreased → never keep (implicit: always track previous best covered)

## Measurement

Coverage is measured from the **full test suite** (not just target-specific tests) to avoid
writing redundant tests for lines already covered by integration/CLI tests. Only the
target-specific test files count toward LOC and timing.

```
coverage: full suite with --cov=<target_package> (parallel via xdist)
timing:   single run of target test files only
LOC:      non-empty, non-comment lines in target test files
```

## Applying to a new target

1. Pick a package (e.g., `src/siftd/output/`)
2. Set `INCLUDE` and `TEST_FILES` in `autoresearch.sh`
3. Run baseline → establishes covered_lines and efficiency
4. Push coverage (step up), then optimize (step down), repeat
5. Stop when remaining miss lines are diminishing returns (platform guards, dead code, etc.)
