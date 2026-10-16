"""One definition of event order; every reader that orders events asks it.

Inside a conversation, order is the *recorded* order: ``events.sequence``,
assigned by ``store_conversation`` in tree-walk order, with ``(timestamp, id)``
as the fallback for legacy rows that have none. A timestamp cannot carry order.
ULIDs are random within a millisecond, so ``ORDER BY timestamp, id`` breaks a
tie arbitrarily, and some adapters stamp every event of a session identically —
3,348 groups of prompts share a timestamp on the author's corpus, and an
exchange-index lookup against one aider session returned the wrong prompt at 38
of 40 positions.

Fixing one reader was never going to hold. The readers that define order are
many (the prompt list, the exchange index, the last-event markers, the
colon-path resolver and its inverse, the turn index, the neighbour lookup, the
exchange fetchers), and they have to *agree*: ``conv:prompt:3`` must name the
prompt ``get_prompt_by_index(conn, conv, 3)`` returns. Two readers that disagree
on a tie are a wrong answer that looks like a right one. So the order is spelled
once, in ``storage/sql_helpers.py`` (``event_order`` for ORDER BY,
``event_key`` for position predicates), and this ratchet is the CLAUDE.md
"enumerable property with a shrink-only allowlist": no SQL literal under
``src/siftd`` may order by ``timestamp`` itself.

Scope and limits, so a future reader can judge what this does not catch:

- It matches the *text* ``ORDER BY ... timestamp`` in a SQL literal (f-strings
  contribute their constant segments, so ``ORDER BY {event_order('e')}`` is
  invisible, which is the point). A sort done in Python (``sorted(...,
  key=timestamp)``), a ``MIN(timestamp)``, or a position predicate written as
  ``timestamp < ?`` is invisible here. The last has its own owner,
  ``event_key``, but nothing enforces it beyond review.
- It cannot say whether an allowlisted site *should* be timestamp-primary, only
  that someone wrote down why. The survivors below order events *across*
  conversations by recency, where the clock is the thing being asked for and no
  per-conversation sequence is comparable.
- Hand-written SQL that ships as data (``builtin_queries/*.sql``) is not Python
  and is not swept; ``session-tools.sql`` spells the order out by hand.

ALLOWLIST is shrink-only. Each entry states why that site may order by time.
"""

import re

from architecture.support import literal_matching, sites_by_function

# `ORDER BY [alias.]timestamp`, also as a later key (`ORDER BY a.conversation_id, a.timestamp`).
ORDERS_BY_TIMESTAMP = re.compile(
    r"ORDER\s+BY\s+(?:[\w.]+(?:\s+(?:ASC|DESC))?\s*,\s*)*(?:\w+\.)?timestamp\b",
    re.IGNORECASE,
)

# (module, function) -> why it orders by time. Shrink-only.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("api/search.py", "enumerate_tagged"): (
        "Most-recently-tagged events and blocks across every conversation, newest "
        "first, capped by LIMIT: recency of the event, not position within a conversation."
    ),
    ("api/export.py", "export_elements"): (
        "Tagged elements across conversations, newest first. Same recency question as "
        "the search sibling; the export re-sorts them by timestamp afterwards."
    ),
}


def test_no_sql_orders_events_by_timestamp_outside_the_allowlist():
    sites = sites_by_function(literal_matching(ORDERS_BY_TIMESTAMP, "order-by-timestamp"))
    offenders = {key: counts for key, counts in sites.items() if key not in ALLOWLIST}
    assert not offenders, (
        f"SQL orders by timestamp in {sorted(offenders)}. Order within a conversation is "
        "events.sequence with (timestamp, id) as the legacy fallback; spell it with "
        "`siftd.storage.sql_helpers.event_order` (ORDER BY) or `event_key` (position "
        "predicates) so every reader agrees on a tie. A site that really means recency "
        "across conversations needs its reason written in ALLOWLIST."
    )


def test_every_allowlisted_site_still_orders_by_timestamp():
    """The ratchet's own guard: an entry that no longer matches is a claim nothing tests."""
    sites = sites_by_function(literal_matching(ORDERS_BY_TIMESTAMP, "order-by-timestamp"))
    stale = sorted(set(ALLOWLIST) - set(sites))
    assert not stale, f"drop {stale} from ALLOWLIST: it no longer orders by timestamp"


def test_allowlist_only_shrinks():
    assert set(ALLOWLIST) == {
        ("api/search.py", "enumerate_tagged"),
        ("api/export.py", "export_elements"),
    }, (
        "ALLOWLIST is shrink-only. A site that genuinely must order events by time needs "
        "its reason written here, where a reviewer will read it."
    )
