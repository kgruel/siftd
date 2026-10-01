"""CI runs every local pytest lane, unconditionally, with the same marker.

`tests/README.md` states the rule this file enforces: a lane no CI job runs is
a test that doesn't exist. Two ways that has failed, one of them for real:

- **A condition that is never true.** `test-slow` was gated on
  `if: github.event_name == 'workflow_call'` so that only publish would run it.
  A called workflow never sees that event name — the `github` context is the
  caller's, so publish's tag push reports `push` — and the job was skipped on
  every run, publish included, for every release through 0.12.2. A reviewer
  cannot tell a dead condition from a live one by reading it, so the property
  checked here is the simpler one: a job that runs pytest carries no `if:` at
  all. A lane that genuinely must be conditional gets an entry in
  `CONDITIONAL_LANE_JOBS` with the reason, and the entry is what review reads.
- **Marker drift.** Each `./dev test*` script and its CI job spell the lane's
  `-m` expression separately. If they diverge, the local lane and CI select
  different tests while both report green. The set of non-empty markers in
  `scripts/test*.sh` must equal the set of `-m` expressions in `ci.yml`.

`ci.yml` is read as text, not YAML: PyYAML is not a base-lane dependency, and
the workflow's layout (jobs at two-space indent) is regular enough that the
parse below fails loudly — rather than passing vacuously — if it changes.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPTS = _REPO_ROOT / "scripts"

# Shrink-only. Format: job name -> why it may be conditional.
CONDITIONAL_LANE_JOBS: dict[str, str] = {}

_JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_PYTEST_MARKER = re.compile(r"""pytest\b.*\s-m\s+(?:"([^"]+)"|'([^']+)'|(\S+))""")
_SCRIPT_MARKER = re.compile(r'^\s*local marker="([^"]*)"\s*$', re.MULTILINE)


def _jobs() -> dict[str, list[str]]:
    """Job name -> the job's body lines, split on two-space-indent headers under `jobs:`."""
    lines = _CI_YML.read_text().splitlines()
    start = lines.index("jobs:") + 1
    jobs: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines[start:]:
        header = _JOB_HEADER.match(line)
        if header:
            current = jobs.setdefault(header.group(1), [])
        elif current is not None:
            current.append(line)
    return jobs


def _lane_jobs() -> dict[str, str]:
    """Job name -> `-m` expression, for every job whose steps run pytest."""
    lanes: dict[str, str] = {}
    for name, body in _jobs().items():
        markers = [m for line in body if (m := _PYTEST_MARKER.search(line))]
        if not markers:
            continue
        assert len(markers) == 1, f"job {name!r} runs pytest {len(markers)} times; this parse expects one"
        lanes[name] = next(g for g in markers[0].groups() if g is not None)
    return lanes


def _script_markers() -> dict[str, str]:
    """Script name -> marker, for every `scripts/test*.sh` lane with a non-empty marker."""
    found: dict[str, str] = {}
    for script in sorted(_SCRIPTS.glob("test*.sh")):
        match = _SCRIPT_MARKER.search(script.read_text())
        if match and match.group(1):
            found[script.name] = match.group(1)
    return found


def test_parse_finds_the_lanes():
    """Guard against a vacuous pass: the text parse must still see the known lanes."""
    assert set(_lane_jobs()) >= {"test", "test-with-embeddings", "test-with-serve", "test-slow"}
    assert set(_script_markers()) >= {"test.sh", "test-embed.sh", "test-serve.sh", "test-slow.sh"}


def test_lane_jobs_are_unconditional():
    jobs = _jobs()
    conditional = {
        name
        for name in _lane_jobs()
        if any(re.match(r"^\s+(?:-\s+)?if:", line) for line in jobs[name])
    }
    unexpected = conditional - set(CONDITIONAL_LANE_JOBS)
    assert not unexpected, (
        f"pytest lane jobs with an `if:` in ci.yml: {sorted(unexpected)}. A lane CI skips is a test "
        "that doesn't exist; if the condition is genuinely needed, add it to CONDITIONAL_LANE_JOBS with the reason."
    )
    stale = set(CONDITIONAL_LANE_JOBS) - conditional
    assert not stale, f"CONDITIONAL_LANE_JOBS lists jobs that are no longer conditional; remove: {sorted(stale)}"


def test_ci_markers_match_local_lanes():
    ci = set(_lane_jobs().values())
    local = set(_script_markers().values())
    assert ci == local, (
        f"CI and ./dev lanes select different tests.\n  only in ci.yml: {sorted(ci - local)}\n"
        f"  only in scripts/test*.sh: {sorted(local - ci)}"
    )
