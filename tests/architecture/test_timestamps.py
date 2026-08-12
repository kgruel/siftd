"""Every timestamp an adapter emits carries a UTC designator.

`conversations.started_at` is compared as a SQL *string* against the bound
`parse_date` renders — `WhereBuilder.since` is literally `c.started_at >= ?`.
So the column is only orderable if every adapter spells the same clock. The
aider adapter did not (#31): it wrote its header's local wall time, which
sorts below a UTC cursor by the size of the host's offset, so delta pulls
skipped those rows silently and permanently.

That invariant lived nowhere but review attention, and it drifted for the
adapter's entire life. This is the enumerable-property form of it.

**Stated as a suffix, deliberately, not as "carries an offset."** A value like
`2025-07-15T14:32:01-05:00` carries an offset and still sorts wrong, because
the comparison is lexical and the bound is UTC. An offset-presence check would
let #31 straight back in wearing a suffix.

It is a tripwire, not a proof, and the limits are worth naming:

- The base is golden fixtures, so it covers the inputs those cases exercise.
- `claude_code`, `codex_cli`, and `gemini_cli` pass their log's own timestamp
  through, so what they emit is a property of third-party log content — no
  static analysis of the adapter could prove its shape. Fixture-level is the
  only enforceable version.
- Nothing requires a registered adapter to *have* a golden fixture. It is 9/9
  today and `./dev new-adapter` scaffolds one, but a tenth shipped without a
  fixture is invisible here.

ALLOWLIST is shrink-only: it is empty, and an entry added to it is a diff a
reviewer sees and questions.
"""

import json

import pytest
from conftest import FIXTURES_DIR, _golden_cases

# Domain fields that land in a timestamp column and are therefore compared as
# strings. Read from the collapsed fixtures, which omit defaulted fields — an
# absent key means the case does not exercise it, not that it is exempt.
TIMESTAMP_KEYS = ("started_at", "ended_at", "timestamp")

# A UTC designator, in the two spellings `datetime.isoformat()` and the tools
# themselves produce. Anything else is either a local wall time or a non-UTC
# offset, and both sort wrong against a UTC bound.
UTC_SUFFIXES = ("Z", "z", "+00:00")

# (adapter, case) pairs exempt from the check. Shrink-only — there is no
# legitimate reason for a new one, so adding an entry is the conversation.
ALLOWLIST: set[tuple[str, str]] = set()


def _timestamps(node, path=""):
    """Yield (json_path, value) for every timestamp-shaped key in the tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in TIMESTAMP_KEYS and isinstance(value, str):
                yield here, value
            else:
                yield from _timestamps(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _timestamps(item, f"{path}[{i}]")


@pytest.mark.parametrize("adapter_name,case", _golden_cases())
def test_adapter_timestamps_are_utc_anchored(adapter_name, case):
    if (adapter_name, case) in ALLOWLIST:
        pytest.skip(f"allowlisted: {adapter_name}/{case}")

    expected = json.loads(
        (FIXTURES_DIR / "adapters" / adapter_name / case / "expected.json").read_text()
    )
    offenders = [
        (where, value)
        for where, value in _timestamps(expected)
        if not value.endswith(UTC_SUFFIXES)
    ]
    assert not offenders, (
        f"{adapter_name}/{case} emits timestamps with no UTC designator: {offenders}. "
        f"They sort against `--since` by the host's offset, not by their instant "
        f"(#31). Convert at parse time with `siftd.dateparse.local_to_utc`."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries are removed, never added."""
    assert ALLOWLIST == set(), (
        "ALLOWLIST is shrink-only and currently empty. An adapter that cannot "
        "emit a UTC-anchored timestamp needs a stated reason here, not an entry."
    )
