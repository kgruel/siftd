"""One function spells the id-prefix predicate; every resolver asks it.

siftd addresses conversations, events, and content blocks by ULID prefix, and
that is only usable because a prefix naming two rows is an *error* the caller
can act on (`AmbiguousPrefix`, exit 2 / HTTP 400, with the candidates listed)
rather than one row chosen arbitrarily. Seven arms across three modules used to
write `(x.id = ? OR x.id LIKE ?)` and its two parameters themselves, and both
things that can go wrong at that keystroke went wrong:

- **A wrong answer.** `api.events.resolve_event_row` closed its arm with
  `ORDER BY id LIMIT 1` and returned an arbitrary match — no error, no signal
  (#33). Its docstring argued the risk away on the grounds that a ULID prefix
  is random. It is not: a ULID is 10 chars of millisecond timestamp followed by
  16 of randomness, so the 12-char prefix `short_id` prints carries **10 random
  bits**, and two ids can only collide when minted in the same millisecond —
  which is what a bulk ingest loop produces. On the author's database that is
  25,039 of 1,357,211 events (~1.8%) sharing a 12-char prefix with another.
- **A wrong plan.** An id column is BINARY-collated and SQLite's default LIKE
  is case-insensitive, so `id LIKE 'p%'` can never use the index. Every arm
  scanned the whole of it: ~42 ms per lookup on 1.36M events, against ~0.3 ms
  for the half-open range that replaced it.

Neither is visible in a diff. `LIMIT 1` on a lookup that returns one row reads
as correct, and a `LIKE` prefix reads as the obvious way to match one. Both are
wrong only in relation to decisions recorded in another module, and neither
surfaces as a failure — one is a wrong row, the other is latency. That is the
CLAUDE.md ratchet test: an invariant that lives only in review vigilance is one
nobody is holding.

So the property is single-valuedness, not correctness-in-place: only
`api.conversations.prefix_predicate` may spell an id-prefix predicate. What
the predicate *is* — LIKE, range, something later — then becomes one decision
in one place, and every arm inherits it, which is what the seventh arm did not.

Scope and limits, so a future reader can judge what this does not catch:

- It matches the predicate *text*. A prefix search assembled from fragments,
  or expressed with `substr(id, 1, ?) = ?` or `GLOB`, is invisible here.
- The range form must be two-sided (`id >= ? AND id < ?`). One-sided `id <= ?`
  is a keyset-pagination tiebreaker — five of those exist — and is not a prefix
  match.
- It says nothing about what a caller does with the rows `prefix_candidates`
  returns: reading `rows[0]` instead of asking `resolve_unique_row` would
  reintroduce #33's wrong answer and pass this. That one *is* visible in a diff
  in the way the SQL is not.
- Non-id prefix matching (workspace paths, tag names) is out of scope: those
  are searches, where several matches is the answer, not an ambiguity.

ALLOWLIST is shrink-only. Each entry states why that site may spell its own.
"""

import ast
import re
from pathlib import Path

from architecture.support import SRC, docstring_ids, literal_text, source_files

# The function that owns the predicate. Every resolver asks it.
OWNER = "prefix_predicate"

# `<alias>.id LIKE ?`, or a two-sided `<alias>.id >= ? AND <alias>.id < ?`.
# Anchored on the *column*: `w.path LIKE ?` and `t.name LIKE ?` are searches,
# and a one-sided `id <= ?` is a pagination tiebreaker.
_ID = r"(?:\w+\.)?id"
_ID_PREFIX_SQL = re.compile(
    rf"\b{_ID}\s+LIKE\s+\?|\b{_ID}\s*>=\s*\?\s+AND\s+{_ID}\s*<\s*\?",
    re.IGNORECASE,
)

# Functions that spell their own id-prefix predicate.
# Shrink-only: an entry is a stated reason, not a suppression.
ALLOWLIST: dict[str, str] = {
    "storage/queries.py::fetch_conversation_by_id_or_prefix": (
        "A storage primitive below the resolver, reached only with an id its "
        "caller already resolved (api.get_conversation_metadata's docstring "
        "carries that contract). Its LIKE arm is vestigial for that caller — "
        "the equality arm matches first — and `storage/` cannot import `api/` "
        "to reach the owner anyway. Dissolves by dropping the LIKE arm; that "
        "is a behavior change for any caller still passing a raw prefix, so "
        "it is not free."
    ),
}


def _offending_functions(path: Path) -> list[str]:
    """Names of functions in one file that spell an id-prefix predicate.

    Scoped to the enclosing function rather than the file so a module can hold
    both a compliant resolver and an allowlisted primitive, and so an entry
    names the thing a reader has to go read. Docstrings are prose — the owner
    and the allowlisted primitive both quote the predicate in theirs.
    """
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []

    prose = docstring_ids(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name == OWNER:
            continue
        if any(
            id(child) not in prose
            and (text := literal_text(child))
            and _ID_PREFIX_SQL.search(text)
            for child in ast.walk(node)
        ):
            offenders.append(node.name)
    return offenders


def test_id_prefix_predicate_is_single_valued():
    offenders = {}
    for path in source_files():
        rel = str(path.relative_to(SRC))
        for name in _offending_functions(path):
            if f"{rel}::{name}" not in ALLOWLIST:
                offenders.setdefault(rel, []).append(name)

    assert not offenders, (
        f"id-prefix predicate spelled outside `{OWNER}`: {offenders}. Both "
        f"things that can go wrong at that keystroke have: a `LIKE ? ... "
        f"LIMIT 1` arm returned an arbitrary row when the prefix named "
        f"several (#33), and `id LIKE 'p%'` cannot use the index, so every "
        f"lookup scanned all of it. Ask "
        f"`api.conversations.prefix_candidates` for the rows (it calls "
        f"`{OWNER}`) and `resolve_unique_row` for the decision."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries come out, never in."""
    assert set(ALLOWLIST) == {
        "storage/queries.py::fetch_conversation_by_id_or_prefix",
    }, (
        "ALLOWLIST is shrink-only. A site that genuinely must spell its own "
        "prefix predicate needs its reason written here, where a reviewer "
        "sees it in the diff."
    )
