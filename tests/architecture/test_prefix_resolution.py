"""An id-prefix query is answered by `prefix_candidates`, never by first-matching.

siftd addresses conversations, events, and content blocks by ULID prefix, and
that is only usable because a prefix naming two rows is an *error* the caller
can act on (`AmbiguousPrefix`, exit 2 / HTTP 400, with the candidates listed)
rather than one row chosen arbitrarily. Whether a given site honors that is
decided at the moment the SQL is written, and both spellings are one line:

    SELECT ... WHERE id LIKE ? ORDER BY id LIMIT 1     # first-match
    prefix_candidates(...) -> resolve_unique_row(...)  # resolve-or-raise

The first is what `api.events.resolve_event_row` did until #33. It returned a
different event than the caller asked for — no error, no signal — and its own
docstring argued the risk away on the grounds that a ULID prefix is random.
It is not: a ULID is 10 chars of millisecond timestamp followed by 16 of
randomness, so the 12-char prefix `short_id` prints carries **10 random bits**,
not 60, and two ids can only collide when they were minted in the same
millisecond — which is precisely what a bulk ingest loop produces. On the
author's database that is 25,039 of 1,357,211 events (~1.8%) sharing a 12-char
prefix with another event.

**Why a ratchet and not review attention.** A first-matching prefix query is
invisible in a diff: `LIMIT 1` on a lookup that returns one row reads as
correct. It is only wrong in relation to a decision recorded in another module
— that ambiguity is surfaced, not resolved — and the wrongness never surfaces
as a failure, only as a wrong answer. That is the CLAUDE.md ratchet test.

The property is *routing*, not textual: a function that spells an id-prefix
predicate must hand it to `prefix_candidates`, which caps the fetch at six and
returns the lazy exact-count the error needs. The resolvers that compose it
(`resolve_entity_id`, `TargetRef._resolve_cross_kind`, `resolve_event_row`)
still write the predicate themselves — they pass it in — so forbidding the
literal outright would flag every correct site.

Scope and limits, so a future reader can judge what this does not catch:

- It matches the *predicate text*, so a prefix search assembled from fragments,
  or expressed with `substr(id, 1, ?) = ?` or `GLOB`, is invisible here.
- It says nothing about what a caller does with the resolved row, and nothing
  about non-id prefix matching (workspace paths, tag names) — those are
  searches, where several matches is the answer, not an ambiguity.
- Routing through `prefix_candidates` is necessary, not sufficient: a caller
  could still read `rows[0]` instead of asking `resolve_unique_row`. That is
  visible in a diff in a way the SQL is not.

ALLOWLIST is shrink-only. Each entry states why that site may first-match.
"""

import ast
import re
from pathlib import Path

from architecture.support import SRC, literal_text, source_files

# The helper that owns the fetch. Every prefix resolver asks it.
OWNER = "prefix_candidates"

# `<alias>.id LIKE ?` / `id LIKE ?` — an id column matched by prefix. Deliberately
# anchored on the *column*: `w.path LIKE ?` and `t.name LIKE ?` are searches.
_ID_PREFIX_SQL = re.compile(r"\b(?:\w+\.)?id\s+LIKE\s+\?", re.IGNORECASE)

# Functions that spell an id-prefix predicate and answer it themselves.
# Shrink-only: an entry is a stated reason, not a suppression.
ALLOWLIST: dict[str, str] = {
    "storage/queries.py::fetch_conversation_by_id_or_prefix": (
        "A storage primitive below the resolver, reached only with an id its "
        "caller already resolved (api.get_conversation_metadata's docstring "
        "carries that contract). Its LIKE arm is vestigial for that caller — "
        "the equality arm matches first — and `storage/` cannot import `api/` "
        "to reach the resolver anyway. Dissolves by dropping the LIKE arm; "
        "that is a behavior change for any caller still passing a raw prefix, "
        "so it is not free."
    ),
}


def _offending_functions(path: Path) -> list[str]:
    """Names of functions in one file that spell an id-prefix predicate
    without handing it to `prefix_candidates`.

    Scoped to the enclosing function rather than the file so a module can hold
    both a compliant resolver and an allowlisted primitive, and so an entry
    names the thing a reader has to go read.

    Bare string statements are skipped: a docstring quoting the predicate it
    documents is prose, and both `prefix_candidates` and the allowlisted
    primitive quote it in theirs.
    """
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name == OWNER:
            continue
        prose = {
            id(stmt.value) for stmt in ast.walk(node) if isinstance(stmt, ast.Expr)
        }
        spells_prefix = any(
            id(child) not in prose
            and (text := literal_text(child))
            and _ID_PREFIX_SQL.search(text)
            for child in ast.walk(node)
        )
        if not spells_prefix:
            continue
        routes = any(
            isinstance(child, ast.Call)
            and (
                (isinstance(child.func, ast.Name) and child.func.id == OWNER)
                or (isinstance(child.func, ast.Attribute) and child.func.attr == OWNER)
            )
            for child in ast.walk(node)
        )
        if not routes:
            offenders.append(node.name)
    return offenders


def test_id_prefix_queries_route_through_prefix_candidates():
    offenders = {}
    for path in source_files():
        rel = str(path.relative_to(SRC))
        for name in _offending_functions(path):
            key = f"{rel}::{name}"
            if key not in ALLOWLIST:
                offenders.setdefault(rel, []).append(name)

    assert not offenders, (
        f"id-prefix query answered without `{OWNER}`: {offenders}. A "
        f"`WHERE id LIKE ? ... LIMIT 1` returns an arbitrary row when the "
        f"prefix names several, and a ULID's first 10 chars are its "
        f"millisecond timestamp, so ids minted in one ingest loop collide at "
        f"~1/1024 on the 12 chars `short_id` prints. Fetch with "
        f"`api.conversations.prefix_candidates` and decide with "
        f"`resolve_unique_row`, which raises `AmbiguousPrefix` with the "
        f"candidates instead of picking one (#33)."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries come out, never in."""
    assert set(ALLOWLIST) == {
        "storage/queries.py::fetch_conversation_by_id_or_prefix",
    }, (
        "ALLOWLIST is shrink-only. A site that genuinely must answer its own "
        "prefix query needs its reason written here, where a reviewer sees it "
        "in the diff."
    )
