"""`dateparse` is the only module that turns a timestamp string into a datetime.

The sibling ratchet, `test_timestamps.py`, holds the *write* side: every
timestamp an adapter emits carries a UTC designator. This one holds the read
side, and it exists because that invariant is not self-enforcing at the point
of use — a correctly-written `...T09:30:12Z` is still misread by a converter
that strips the suffix, and a legacy naive value is misread by one that lets
`astimezone()` resolve it against the host zone.

Seven modules had written their own converter before #32: `ingestion`,
`output` twice, `search`, `peek`, `api`, and `cli`. Each spelled the `Z`
handling differently — `.replace("Z", "+00:00")`, `.rstrip("Z")`, a
`"+" not in s` heuristic — and they had drifted into disagreeing about
whether a lowercase `z` is a timestamp at all. Three of them read a naive
value as host-local, which is what `datetime.astimezone()` and
`datetime.timestamp()` do by default, so the bug was what you got by writing
the obvious thing.

**Why an AST check and not review attention.** Nothing about writing
`datetime.fromisoformat(ts.replace("Z", "+00:00"))` looks wrong in a diff. It
looks like parsing a timestamp. It is only wrong in relation to a decision
recorded somewhere else — that a naive value from one of siftd's own columns
means UTC — and a rule whose violation is invisible locally is exactly the
kind that drifts. That is the ratchet test from CLAUDE.md: an invariant that
lives only in review vigilance is one nobody is actually holding.

Scope and limits, stated so a future reader can judge what this does not
catch:

- It matches on the *call*, `datetime.fromisoformat(...)`, not on the
  resulting semantics. Something that parses with `strptime`, or slices the
  string by index, passes this and is just as wrong.
- `date.fromisoformat` is deliberately not matched. A calendar date has no
  instant to get wrong; the zone question does not arise. `parse_date` and
  the stats/HTML day-bucket code use it and are correct to.
- It is source-level, so a converter reached through `getattr` or an alias
  (`from datetime import datetime as dt`) is invisible. The alias case is
  covered — the check reads the attribute name, not the binding — but a fully
  dynamic call is not.

ALLOWLIST is shrink-only. Each entry states why that site cannot go through
`to_utc`, and the guard below fails if the set grows past what is written
here.
"""

import ast
from pathlib import Path

from architecture.support import SRC, source_files

# The module that owns the operation. Every other module asks it.
OWNER = "dateparse.py"

# Sites that parse a timestamp outside `dateparse` and are exempt. Shrink-only:
# an entry is a stated reason, not a suppression. Empty is the goal state and
# the current state.
ALLOWLIST: dict[str, str] = {}


def _fromisoformat_calls(path: Path) -> list[int]:
    """Line numbers of `datetime.fromisoformat(...)` calls in one file.

    Matches the attribute name rather than the binding, so `datetime` imported
    under an alias is still caught. `date.fromisoformat` is excluded by
    reading the receiver: a calendar date carries no instant to misplace.
    """
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []

    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr != "fromisoformat":
            continue
        # `date.fromisoformat` is fine; only the datetime form is in scope.
        receiver = fn.value
        if isinstance(receiver, ast.Name) and receiver.id == "date":
            continue
        lines.append(node.lineno)
    return lines


def test_only_dateparse_parses_timestamps():
    offenders = {}
    for path in source_files():
        rel = str(path.relative_to(SRC))
        if rel == OWNER or rel in ALLOWLIST:
            continue
        if lines := _fromisoformat_calls(path):
            offenders[rel] = lines

    assert not offenders, (
        f"`datetime.fromisoformat` called outside `{OWNER}`: {offenders}. "
        f"A hand-rolled converter has to re-decide what a naive value means, "
        f"and the seven that existed before #32 did not all decide the same "
        f"thing. Use `siftd.dateparse.to_utc` for a value read out of one of "
        f"siftd's own columns (naive means UTC), or `local_to_utc` for an "
        f"adapter log with no offset (naive means the host's zone)."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries come out, never in."""
    assert ALLOWLIST == {}, (
        "ALLOWLIST is shrink-only and currently empty. A site that genuinely "
        "cannot go through `dateparse` needs its reason written here, where a "
        "reviewer sees it in the diff."
    )
