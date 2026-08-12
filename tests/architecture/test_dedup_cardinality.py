"""A `"file"`-strategy adapter yields at most one conversation per source.

`ingested_files.path` is UNIQUE, so a file-strategy row names exactly one
conversation. Ingest enforces that at runtime by failing the source — which
means a violation reaches the user as an errored bookkeeping row they have to
go looking for, not as a test failure the author sees.

The aider adapter sat in that gap for its whole life (#36). Its golden fixture
asserted `parse()` yields *two* conversations and passed, because the adapter
was correct; `src/siftd/adapters/README.md` stated the invariant in prose; and
nothing connected the two. Every aider history file with a second session
failed permanently. The fix moved aider to `"session"`, where a source is a
container and many conversations are the point.

This is the enumerable-property form of the invariant: the cardinality an
adapter's fixture demonstrates has to agree with the strategy it declares.

It is a tripwire, not a proof, and the limits are worth naming:

- The base is golden fixtures, so it covers the inputs those cases exercise. A
  file-strategy adapter that yields two conversations only on input no case
  has is still invisible here — as it is to every other fixture-based check.
- Nothing requires a registered adapter to *have* a golden fixture. It is 9/9
  today and `./dev new-adapter` scaffolds one, but a tenth shipped without a
  fixture is invisible.
- The converse is deliberately not asserted. A `"session"` adapter whose
  fixture holds one conversation is fine: cardinality is a property of the
  *source*, and a database with one session in it is a container all the same.

ALLOWLIST is shrink-only: it is empty, and an entry added to it is a diff a
reviewer sees and questions.
"""

import importlib

import pytest
from conftest import _golden_cases, load_golden_expected

# (adapter, case) pairs exempt from the check. Shrink-only — a file-strategy
# adapter that yields many conversations belongs on `"session"`, so there is
# no legitimate entry; adding one is the conversation.
ALLOWLIST: set[tuple[str, str]] = set()


@pytest.mark.parametrize("adapter_name,case", _golden_cases())
def test_file_strategy_yields_at_most_one_conversation(adapter_name, case):
    if (adapter_name, case) in ALLOWLIST:
        pytest.skip(f"allowlisted: {adapter_name}/{case}")

    adapter = importlib.import_module(f"siftd.adapters.{adapter_name}")
    if getattr(adapter, "DEDUP_STRATEGY", "file") != "file":
        pytest.skip(f"{adapter_name} is a session-strategy adapter")

    expected = load_golden_expected(adapter_name, case)
    assert len(expected) <= 1, (
        f"{adapter_name} declares DEDUP_STRATEGY = 'file' but its {case} fixture "
        f"yields {len(expected)} conversations. Ingest fails such a source outright, "
        f"so every file of this shape would never ingest (#36). A source that holds "
        f"or grows several conversations belongs on DEDUP_STRATEGY = 'session'."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries are removed, never added."""
    assert ALLOWLIST == set(), (
        "ALLOWLIST is shrink-only and currently empty. A file-strategy adapter "
        "yielding many conversations needs the 'session' strategy, not an entry."
    )
