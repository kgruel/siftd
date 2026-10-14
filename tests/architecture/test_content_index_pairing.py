"""Every writer of ``event_content`` is declared, and each names the function that writes its index.

``content_fts`` is derived from ``event_content``, and nothing in the schema
ties the two together: an FTS row is a virtual-table insert that no foreign key
describes. Before #75 the question "is this block indexable" was answered in
two places, and a new ``event_content`` writer could ship with no index write
at all — nothing failed until a search came back empty.

This is a **location** claim, not a verdict about the index. It does not decide
whether any writer's index is correct; ``get_fts_sync_status`` and
``test_fts_rebuild_on_push.py`` do that. It says: here is every static SQL
literal in ``src/siftd`` that writes ``event_content``, keyed by the function it
sits in, and each entry names the function that owns the paired index write.
A new site fails until it is declared with its owner; a vanished site fails
until its entry is removed, so a sweep that stops finding anything cannot pass.

Three properties, which fail differently:

- **the sites** — the literal population, by equality of counts;
- **the owners** — each owner's body calls an index writer;
- **the leaves** — the helpers that write a block are reached only from the
  declared chain, so a new caller of a helper is caught even when the helper
  itself is unchanged.

The SQL matcher is pinned by a table of spellings it must and must not match,
so a regex that quietly stops matching ``INSERT OR REPLACE`` or a schema-qualified
name fails here rather than shrinking the population.
"""

import re
from dataclasses import dataclass

from architecture.support import call_to, literal_matching, sites_by_function


@dataclass(frozen=True)
class Writer:
    """One declared site: how many ``event_content`` statements it holds, and who indexes it.

    ``owner`` is ``(module, function)`` of the function whose body calls an index
    writer for these rows, or ``None`` for a carve-out — which must say why, here.
    """

    sites: int
    owner: tuple[str, str] | None


# (module, enclosing function) -> the statements declared there. Paths are
# relative to src/siftd, the same root every ratchet in this directory uses.
EVENT_CONTENT_WRITERS: dict[tuple[str, str], Writer] = {
    # The leaf writer. Reached from insert_prompt_content / insert_response_content
    # only, which store_conversation calls; store_conversation owns the index write.
    ("storage/events.py", "insert_event_content"): Writer(
        sites=1, owner=("storage/sqlite.py", "store_conversation"),
    ),
    # Merge copies the source's blocks in; merge_database owns the scoped index
    # rebuild that covers them (the scope is derived from the content, see #49).
    ("api/merge.py", "_merge_attached"): Writer(
        sites=1, owner=("api/merge.py", "merge_database"),
    ),
    # Slice copies the selected blocks into the slice database; slice_database
    # rebuilds the index over the slice's conversations after populating it.
    ("api/slice.py", "_populate_slice"): Writer(
        sites=1, owner=("api/slice.py", "slice_database"),
    ),
    # CARVE-OUT: frozen migration v4 backfills event_content from the pre-v4
    # tables. It writes no index, and does not need to: migration v5 drops
    # content_fts and rebuilds it from event_content, and v5 runs after v4 on
    # every upgrade path. Frozen migrations are not edited to satisfy a ratchet.
    ("storage/sqlite.py", "_migrate_v4_polymorphic_schema"): Writer(
        sites=2, owner=None,
    ),
}

# The functions that write content_fts. A declared owner must call one of them.
INDEX_WRITERS = ("index_conversations", "rebuild_fts_index")

# The chain a block takes from its caller down to the one INSERT. Each leaf's
# callers, as (module, enclosing function), must be exactly these — and no
# other place in src/siftd may call the leaf.
LEAF_CALLERS: dict[str, set[tuple[str, str]]] = {
    "insert_event_content": {
        ("storage/sqlite.py", "insert_prompt_content"),
        ("storage/sqlite.py", "insert_response_content"),
    },
    "insert_prompt_content": {("storage/sqlite.py", "store_conversation")},
    "insert_response_content": {("storage/sqlite.py", "store_conversation")},
    # The bulk writers are helpers too: their owner is their caller, so a
    # second caller would copy blocks without the owner's index write.
    "_merge_attached": {("api/merge.py", "merge_database")},
    "_populate_slice": {("api/slice.py", "slice_database")},
}

# An INSERT into event_content, in every spelling this codebase can produce:
# plain or ``OR IGNORE`` / ``OR REPLACE`` (and the other conflict clauses),
# multi-line, schema-qualified (``slice.`` / ``main.``), and quoted. The
# negative lookahead ``(?!\w)`` is what keeps ``event_content_fts`` out; ``\b``
# would not do, since it fails between a closing quote and a space.
EVENT_CONTENT_WRITE = re.compile(
    r"""INSERT \s+
        (?: OR \s+ (?:IGNORE | REPLACE | ABORT | FAIL | ROLLBACK) \s+ )?
        INTO \s+
        (?: ["`\[]? \w+ ["`\]]? \s* \. \s* )?      # optional schema, quoted or not
        ["`\[]? event_content ["`\]]? (?!\w)""",
    re.IGNORECASE | re.VERBOSE,
)


def _found_writers() -> dict[tuple[str, str], dict[str, int]]:
    return sites_by_function(literal_matching(EVENT_CONTENT_WRITE, "event_content"))


def _declared_counts() -> dict[tuple[str, str], dict[str, int]]:
    return {site: {"event_content": w.sites} for site, w in EVENT_CONTENT_WRITERS.items()}


def test_every_event_content_writer_is_declared():
    found = _found_writers()
    declared = _declared_counts()
    if found == declared:
        return

    unexpected = sorted(set(found) - set(declared))
    vanished = sorted(set(declared) - set(found))
    miscounted = sorted(
        site for site in set(found) & set(declared) if found[site] != declared[site]
    )
    assert not (unexpected or vanished or miscounted), (
        f"event_content writers out of step with EVENT_CONTENT_WRITERS.\n"
        f"  new (declare each with its index owner, or add the index write): {unexpected}\n"
        f"  vanished (remove the entry; the sweep no longer finds the statement): {vanished}\n"
        f"  count changed (update `sites`, and check the owner still indexes it): {miscounted}"
    )


def test_each_owner_writes_the_index():
    index_sites = sites_by_function(call_to(INDEX_WRITERS))
    unindexed = {
        site: w.owner
        for site, w in EVENT_CONTENT_WRITERS.items()
        if w.owner is not None and w.owner not in index_sites
    }
    assert not unindexed, (
        f"these event_content writers name an owner that never calls an index writer: {unindexed}. "
        f"Add a call to index_conversations (insert-only, for a conversation just created) "
        f"or rebuild_fts_index(conversation_ids=...) in the owner, or declare a carve-out "
        f"with its reason in EVENT_CONTENT_WRITERS."
    )


def test_leaf_writer_is_reached_only_through_its_owner():
    mismatched = {}
    for leaf, expected in LEAF_CALLERS.items():
        callers = set(sites_by_function(call_to((leaf,))))
        if callers != expected:
            mismatched[leaf] = {"found": sorted(callers), "declared": sorted(expected)}
    assert not mismatched, (
        f"event_content leaf helpers called from outside the declared chain: {mismatched}. "
        f"A new caller that writes a block must be added to LEAF_CALLERS and must reach "
        f"store_conversation, which indexes it; otherwise route it through store_conversation."
    )


def test_the_event_content_matcher_reads_every_spelling_of_the_insert():
    must_match = [
        "INSERT INTO event_content (id) VALUES (?)",
        "INSERT OR IGNORE INTO event_content (id) VALUES (?)",
        "INSERT OR REPLACE INTO event_content (id) VALUES (?)",
        "insert into event_content (id) values (?)",
        "INSERT INTO slice.event_content (id) SELECT 1",
        "INSERT OR IGNORE INTO main.event_content (id) SELECT 1",
        'INSERT INTO "event_content" (id) VALUES (?)',
        'INSERT INTO "slice"."event_content" (id) SELECT 1',
        "INSERT INTO [event_content] (id) VALUES (?)",
        "INSERT INTO `event_content` (id) VALUES (?)",
        "INSERT\n        OR IGNORE\n        INTO event_content (id)",
    ]
    must_not_match = [
        "SELECT id FROM event_content",
        "DELETE FROM event_content WHERE id = ?",
        "INSERT INTO event_content_fts (id) VALUES (?)",
        "INSERT INTO content_fts (text_content) SELECT ec.id FROM event_content ec",
    ]
    missed = [s for s in must_match if not EVENT_CONTENT_WRITE.search(s)]
    overmatched = [s for s in must_not_match if EVENT_CONTENT_WRITE.search(s)]
    assert not missed and not overmatched, (
        f"EVENT_CONTENT_WRITE misses {missed} or over-matches {overmatched}. "
        f"Fix the pattern: a missed spelling shrinks the population this ratchet can see."
    )
