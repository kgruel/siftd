"""A conversation replacement is answerable to one list of what it must carry.

Ingest replaces a changed transcript with delete-then-insert, and everything
hung off the old conversation goes with it. Most of that loss is correct — the
replacement's own parse supplies it again. What is not correct is the part a
person or a server attached, which no re-parse reproduces:
``storage/replacement.py`` is that list.

It shipped with exactly one entry for a long time, and #54 is what one entry
costs. Ownership was dropped by every replacement, on every dedup strategy, at
every trigger. Nothing failed, because there was no list to be incomplete — the
snapshot was *named* for tags, so carrying only tags read as complete.

Three properties hold that list to its population, and they fail differently:

- **the sites, by carry** — a fourth place that deletes a conversation and
  stores a new one is a fourth place that can drop what the list does not
  carry;
- **the sites, by delete** — the same population approached from the side no
  door can decline, which is what catches a door that never snapshots at all
  (#79);
- **the children** — a table added later with a cascade from ``conversations``
  is a fact the list has to have an opinion about, one way or the other.

This is the first ratchet here that derives its population by *running* the
database rather than reading the source, and that is deliberate rather than
convenient. `storage/sqlite.py`'s `_CASCADE_CONTRACT` declares the closure and
is already wrong — five of its entries name pre-v4 tables, and it omits both
`events` and `usage_by_conv_model`. Trusting it would reproduce the exact
sampling error #54 is about. So the schema is asked directly, two ways, because
neither sees the whole population: ``PRAGMA foreign_key_list`` sees the declared
cascades and nothing else, and a real delete additionally exposes the
trigger-driven cleanup (``tr_polymorphic_*``) and the virtual table
(``content_fts``) that no FK describes.

Scope and limits, stated so a future reader can judge what this does not catch:

- The site count is over the *primitives*, not over ingest's wrapper. It was
  scoped to `ingestion/orchestration.py` until #77, which is what that scoping
  cost: `api/merge.py` was the second replacement door all along, carrying
  ownership by hand and hard-deleting tags — the mirror of #54 — and a ratchet
  keyed to `_take_conversation_for_replacement` could not see it, because merge
  has its own delete and never calls that wrapper.
- The carry count sees only doors that use the primitives, which is narrower
  than "every site that can lose a carryover" — a third door hand-rolling its
  own snapshot is invisible to it, and that is not hypothetical, it is
  precisely what `api/merge.py` was. That is the gap the delete-keyed property
  closes (#79), and the two remain complements rather than substitutes:
  delete-keying in turn cannot see a half-carry, since a site that snapshots
  and never restores still deletes and so looks correct from there.
- The delete-keyed half pairs by **module**, not by enclosing function, because
  neither real door snapshots and restores in one function. It therefore does
  not catch a module that deletes in one place and carries in an unrelated
  other; the carry count is what holds that module's arity.
- Counting sites guards *arity*, not *pairing*. A site that snapshots and never
  restores changes the count, but a path that drops the carryover on an early
  return does not.
- The delete-diff half only sees tables the fixture populates. It is the
  corroborating half; ``foreign_key_list`` is the complete one.
"""

import ast
import re

from architecture.support import call_to, literal_matching, parsed_sources, sites_by_function

# Call sites of the carry primitives, per (module, enclosing function).
# Named rather than totalled: a bare count survives deleting one site and
# adding another, and cannot say which one is new — the question the guard
# exists to prompt. Mirrors `test_readonly_opens.py`'s per-(file, function)
# keying.
#
# `restore_and_report` rather than `restore_conversation`: the door-facing pair
# is snapshot-then-restore-and-report, and `storage/replacement.py` is excluded
# from the sweep below because it *defines* them rather than using them.
REPLACEMENT_SITES = {
    ("ingestion/orchestration.py", "_take_conversation_for_replacement"): {
        "snapshot_conversation": 1,
    },
    ("ingestion/orchestration.py", "_restore_carryover_after_replacement"): {
        "restore_and_report": 1,
    },
    ("api/merge.py", "_replace_stale_conversations"): {"snapshot_conversation": 1},
    ("api/merge.py", "_merge_attached"): {"restore_and_report": 1},
    # A move, not a replacement: the row another path claims stays, and the
    # assignments its transcript owns are released from it and re-attached to
    # the conversation stored beside it. That conversation is restored through
    # `_restore_carryover_after_replacement`, which every ingest door calls, so
    # this snapshot has no restore of its own (see SPLIT_SNAPSHOT_SITES).
    ("ingestion/orchestration.py", "_split_carryover"): {"snapshot_conversation": 1},
}
# Snapshot sites whose carryover is moved to a conversation the caller stores
# and restores, rather than restored by the function that snapshots.
SPLIT_SNAPSHOT_SITES = {("ingestion/orchestration.py", "_split_carryover")}
CARRY_PRIMITIVES = ("snapshot_conversation", "restore_and_report")
DEFINITION_SITE = "storage/replacement.py"

# The other half of the guard, keyed on the delete instead of the carry.
#
# A replacement door can decline to snapshot — that is what both #54 and #77
# were, and it is why the count above could not see them. It cannot decline to
# *delete*.
DESTROY_PRIMITIVES = ("delete_conversation", "delete_conversations")
DESTROY_DEFINITION_SITE = "storage/sqlite.py"

# A raw delete against the table, in the spellings this codebase can actually
# produce. It was the bare substring `"DELETE FROM conversations"` until
# external review pointed out how narrow that is, and the two extensions are
# not hypothetical here:
#
# - **whitespace** — `storage/sqlite.py` alone has 63 lines of continued SQL, so
#   `DELETE\n    FROM conversations` is the ordinary way a long statement gets
#   written;
# - **a schema qualifier** — `api/merge.py` attaches the source database and
#   writes `FROM main.conversations` in six places today. That is precisely the
#   module that hand-rolled its own delete through #77, so the one spelling a
#   bypass would most plausibly use was the one not being matched.
#
# Quoted identifiers are matched too, though siftd writes none. Stated limit:
# this reads *static* text, so a table name assembled at runtime is invisible —
# `sites_by_function`'s call half is what covers a door that goes through the
# primitive, and nothing covers a door that does both.
DESTROY_LITERAL = re.compile(
    r"""DELETE \s+ FROM \s+
        (?: ["`\[]? \w+ ["`\]]? \s* \. \s* )?   # optional schema, quoted or not
        ["`\[]? conversations \b""",
    re.IGNORECASE | re.VERBOSE,
)

# Every place in the package that destroys a conversation, and what answers for
# it. Declared rather than allowlisted-by-exception, mirroring
# `REPLACEMENT_SITES` — and counted rather than merely named, because a set of
# keys is blind to a *second* delete added inside a function already on the
# list, the cheapest way for a door to grow one.
#
# Asserting the population by *equality* is what keeps the property from passing
# vacuously: a sweep that silently stops finding anything — a refactor here, a
# changed spelling there — satisfies "nothing unpaired" perfectly, which is the
# failure mode this whole file exists to remove.
#
# A new door has to be argued for here rather than discovered in a bug. #79
# anticipated a `siftd db delete` needing an entry; there is no such command —
# no CLI, api, or serve path destroys a conversation.
#
# The definition site is declared here rather than excluded from the sweep.
# Excluding the module was the obvious shape and the wrong one: it hid every
# *other* function in `storage/sqlite.py` too, so a new helper there could
# reach the primitive and appear nowhere — and, adding no second literal, keep
# `test_the_delete_closure_has_exactly_one_home` green as well. A carve-out
# that big is not a carve-out, it is a hole. Declaring the two real rows costs
# two entries and leaves the population total.
DESTROY_SITES = {
    # paired: snapshots here, restores in _restore_carryover_after_replacement
    ("ingestion/orchestration.py", "_take_conversation_for_replacement"): {
        "delete_conversation": 1,
    },
    # paired: snapshots here, restores in _merge_attached
    ("api/merge.py", "_replace_stale_conversations"): {"delete_conversations": 1},
    # the closure itself: this IS the delete every door above routes through
    (DESTROY_DEFINITION_SITE, "delete_conversations"): {"conversations": 1},
    # the one-conversation spelling, which only delegates
    (DESTROY_DEFINITION_SITE, "delete_conversation"): {"delete_conversations": 1},
}

# Where ingest's own wrapper is called from, in the same shape. The wrapper is
# what makes "ask before replacing" structural for ingest — merge answers that
# question with its owner gate instead — so its arity is worth pinning, one
# level down from the primitive count above. Deliberately not filtered to
# `ingestion/`: if the wrapper is ever called from outside ingest, the mismatch
# should be the alarm rather than something the query silently discards.
INGEST_REPLACEMENT_DOORS = {
    ("ingestion/orchestration.py", "ingest_all"): {
        # One more than before: the session branch adopts a row stored under a
        # superseded external_id (aider's path-identity re-key), the same door the
        # file branch has for gemini_cli. Same wrapper, same refusal.
        "_take_conversation_for_replacement": 3,
    },
    # One door, reached by two resolutions: the orphan found by external_id, or a
    # legacy row under a superseded external_id (gemini_cli chat identity, #93).
    # The target is resolved once (_replacement_target, shared with the --reparse
    # tag gate), then taken by the same call; only the superseded case settles a
    # refusal as a duplicate, because a refused orphan collides into the existing
    # duplicate handler instead. Was two call sites before the tag gate needed the
    # same resolution.
    ("ingestion/orchestration.py", "_reingest_file"): {
        "_take_conversation_for_replacement": 1,
    },
    # A new file adopting a row stored under a superseded external_id (gemini_cli
    # chat identity, #93). Same wrapper, same refusal, so the same ratchet shape.
    ("ingestion/orchestration.py", "_ingest_file"): {
        "_take_conversation_for_replacement": 1,
    },
}

# The `--reparse` tag gate: one question, asked by every door that removes
# assignments, about the assignments *that door* would remove.
#
# Two primitives remove them: `_take_conversation_for_replacement` deletes the
# row, `_split_carryover` takes assignments off a row it keeps. A door is any
# function that calls either. Each must consult `_declined_for_tags` before it
# acts, or be named in GATED_BY_CALLER with the reason its caller already did.
# A gate added per door, by hand, is how a door comes to ask about the wrong
# row, or to skip the question: the new-file route did exactly that, and a
# split removed assignments from a row the gate had exempted as "claimed".
TAG_GATE = "_declined_for_tags"
GATE_DOOR_PRIMITIVES = ("_take_conversation_for_replacement", "_split_carryover")
# What removes assignments from a row that is kept. Asserted by equality: a
# second function releasing assignments would be a door the list above cannot see.
RELEASE_SITES = {
    ("ingestion/orchestration.py", "_split_carryover"): {"release_carryover": 1},
}
# Gate consultations per door function, counted. Each is at least the number of
# `_take_conversation_for_replacement` calls in the same function; a split rides
# on the gate of the take it follows (the gate asks about both, see
# `_door_tag_loss`).
TAG_GATE_SITES = {
    # file branch (one gate for the pointer/orphan/legacy-marker row, resolved by
    # `_replacement_target`), session adoption, session replacement
    ("ingestion/orchestration.py", "ingest_all"): {TAG_GATE: 3},
    # a new file adopting a superseded row
    ("ingestion/orchestration.py", "_ingest_file"): {TAG_GATE: 1},
}
# Door functions that do not consult the gate themselves, and why that holds.
# Shrink-only: an entry is a claim about a caller, and the test below checks it.
GATED_BY_CALLER = {
    ("ingestion/orchestration.py", "_reingest_file"): (
        "reached only from ingest_all's file branch, which resolves the same target "
        "(_replacement_target) and gates it before calling"
    ),
}

# What a conversation delete removes, and what a replacement does about it.
# Shrink-only in the sense that an entry moving between them means
# `storage/replacement.py` moved too.
CARRIED = {
    "tag_assignments": "re-pointed by rejoining on external_id",
    "conversation_owners": "copied across; keys on conversation_id (#54)",
}
NOT_CARRIED = {
    "conversations": "the row being replaced",
    "events": "the replacement's parse IS the new events",
    "event_content": "ditto — content comes from the parse",
    "event_response": "ditto",
    "event_tool_call": "ditto",
    "content_blobs": "content-addressed; the replacement's store re-inserts what it needs",
    "content_fts": (
        "derived from event_content; the replacement's store_conversation indexes "
        "its own via index_conversations"
    ),
    "content_fts_content": "content_fts's shadow storage",
    "content_fts_docsize": "content_fts's shadow storage",
    "conversation_links": (
        "the replacement's parse states its own parent (Conversation.lineage), "
        "and the edge holds the parent's external_id, so nothing in it goes stale "
        "when either side is re-ingested"
    ),
    "conversation_stats": "derived tier, rebuilt from the replacement's rows",
    "usage_by_conv_model": "derived tier, rebuilt from the replacement's rows",
    "ingested_files": "the replacement writes its own row (record_ingested_file)",
    "attributes": (
        "written by the parse — store_conversation re-derives them at every "
        "scope ('analyzer', 'provider', tool-call). Known residual cost: gaps "
        "that `siftd backfill` filled rather than the parse are dropped and "
        "need backfilling again; they are re-derivable, unlike a tag"
    ),
}


def test_a_conversation_is_destroyed_only_where_the_module_carries():
    """Every place a conversation is destroyed is one of the declared doors.

    The complement of `test_every_replacement_site_is_accounted_for`, and the
    reason #79 exists: that one keys on what a *correct* door does, so it can
    only see doors already using the primitives — while both defects it was
    built to prevent were doors that did not. `api/merge.py` hand-rolled its
    own snapshot for the whole life of the previous ratchet.

    **What this does not claim**, since the name used to say "paired" and the
    mechanism does not support it: the unit is the *site*, but the pairing it
    can verify is only that the site's module also carries. A module that
    deletes in one function and restores in an unrelated other satisfies this,
    and both #54 and #77 lived inside these two modules. Same-function pairing
    is unavailable rather than merely unenforced — neither door has that shape
    (ingest snapshots in `_take_conversation_for_replacement`, restores in
    `_restore_carryover_after_replacement`; merge snapshots in
    `_replace_stale_conversations`, restores in `_merge_attached`), and a
    property no correct code satisfies is one that gets weakened at the first
    failure. What holds a listed module's arity is the carry count above; what
    this holds is the *population*.

    Asserted by equality over *counts*, not by "nothing unpaired" over keys.
    Both weaker forms have a silent pass in them: the negative form succeeds
    when the sweep finds nothing at all (a refactored walker, a changed
    spelling, a narrowed `source_files()`), and key-equality succeeds when a
    second delete is added inside a function already on the list — which is the
    cheapest way for a door to grow one, and was live here until external
    review caught it.
    """
    found: dict[tuple[str, str], dict[str, int]] = {}
    for finder in (call_to(DESTROY_PRIMITIVES), literal_matching(DESTROY_LITERAL, "conversations")):
        for site, per in sites_by_function(finder).items():
            for label, n in per.items():
                found.setdefault(site, {})[label] = found.setdefault(site, {}).get(label, 0) + n

    assert found == DESTROY_SITES, (
        f"conversation-destroying sites moved: {found} != {DESTROY_SITES}. A new "
        "one must snapshot before the delete and restore after — see "
        "`storage/replacement.py` — then be counted in DESTROY_SITES with what "
        "answers for it. A site that vanished means either the door closed or "
        "the sweep stopped seeing it; the second is the dangerous one."
    )


def test_the_destroy_pattern_reads_every_spelling_of_the_delete():
    """The regex half is a detector, so its own reach is worth pinning.

    Every widening below came from an external review pass finding the previous
    form too narrow, one spelling at a time — first whitespace, then a schema
    qualifier, then a *quoted* schema qualifier. Written as a table because the
    alternative is discovering the next narrowing the way the last three were
    found. The negatives matter as much: `conversation_owners` and a
    `conversations_backup` table must not read as a conversation delete, or the
    population grows entries that are not doors.
    """
    matches = [
        "DELETE FROM conversations",
        "DELETE  FROM   conversations",
        "DELETE\n          FROM conversations",
        "DELETE FROM main.conversations",
        'DELETE FROM "main"."conversations"',
        "DELETE FROM [main].[conversations]",
        "delete from Main . Conversations",
    ]
    misses = [
        "DELETE FROM conversation_owners",
        "DELETE FROM conversations_backup",
        "SELECT * FROM conversations",
    ]
    assert [s for s in matches if not DESTROY_LITERAL.search(s)] == [], (
        "a spelling of the conversation delete is no longer matched — the sweep "
        "would read a door written that way as absent."
    )
    assert [s for s in misses if DESTROY_LITERAL.search(s)] == [], (
        "the pattern matches something that is not a conversation delete; it "
        "would put a non-door into DESTROY_SITES."
    )


def test_the_destroy_primitives_are_never_imported_under_an_alias():
    """`from ... import delete_conversation as x` would walk straight past the sweep.

    The sweep matches call *names*, so an aliased import renames the call out
    of the population — found by external review, and cheaper to forbid than to
    resolve. Forbidding is also the honest shape: teaching a name sweep to
    follow imports makes it claim a reach it would only sometimes have, where
    this fails loudly at the one line that introduces the alias.
    """
    aliased = [
        (rel, node.module, alias.name, alias.asname)
        for rel, tree, _ in parsed_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in DESTROY_PRIMITIVES and alias.asname
    ]
    assert aliased == [], (
        f"{aliased} imports a conversation-destroying primitive under another "
        "name, which `sites_by_function` cannot see. Import it plainly."
    )


def test_the_delete_closure_has_exactly_one_home():
    """`DELETE FROM conversations` is written once, where the closure lives.

    The positive half of the sweep above, which by construction only looks
    *outside* `storage/sqlite.py`. Without this, moving the statement into a
    second module and deleting the original passes: the literal would simply
    have relocated to a place the exclusion hides — and if it relocated into a
    module that already carries, the population check would accept it too. #51
    is what one unnoticed second copy of this closure costs.
    """
    home = {
        scope: per
        for (rel, scope), per in sites_by_function(literal_matching(DESTROY_LITERAL, "conversations")).items()
        if rel == DESTROY_DEFINITION_SITE
    }
    uses = sum(n for per in home.values() for n in per.values())
    assert uses == 1, (
        f"{DESTROY_DEFINITION_SITE} deletes from `conversations` {uses} times, "
        "expected exactly 1. The closure is a fact about the schema and gets one "
        "home; if it genuinely moved, move DESTROY_DEFINITION_SITE with it."
    )


def test_every_replacement_site_is_accounted_for():
    """Every site that snapshots or restores a carryover is named here.

    A replacement *trigger* is a condition, so it cannot be read off the source
    the way `DEDUP_STRATEGY` can. What can be read is the sites that take and
    put back the carryover — and, since #77, they are read from the whole
    package rather than from ingest alone. A new entry is the signal to ask
    which trigger it introduces and whether
    `tests/test_live_tagging.py::_REPLACEMENT_TRIGGERS` still covers the
    population — the question nobody asked when #36 made a second trigger
    common.
    """
    found = sites_by_function(call_to(CARRY_PRIMITIVES), exclude=DEFINITION_SITE)
    assert found == REPLACEMENT_SITES, (
        f"replacement sites moved: {found} != {REPLACEMENT_SITES}. Every one of "
        "them deletes a conversation and stores a new one, so every one can lose "
        "what `storage/replacement.py` does not carry. Check "
        "_REPLACEMENT_TRIGGERS still enumerates the conditions that reach them."
    )


def test_the_listed_sites_snapshot_and_restore_in_equal_number():
    """A door that takes a carryover and never puts it back is the loss itself.

    Asserted against the allowlist, not against the tree: by the time the test
    above has passed, the two are the same object and summing the tree would be
    summing the constant. The reader this guards is the one *editing*
    `REPLACEMENT_SITES` to match a change — the moment a half-carry gets
    written down as expected, which is what both #54 and #77 were.
    """
    snapshots = sum(
        c.get("snapshot_conversation", 0)
        for site, c in REPLACEMENT_SITES.items()
        if site not in SPLIT_SNAPSHOT_SITES
    )
    restores = sum(c.get("restore_and_report", 0) for c in REPLACEMENT_SITES.values())
    assert snapshots == restores, (
        f"{snapshots} snapshot site(s) listed against {restores} restore site(s). "
        "If a door really does snapshot without restoring, say why here — "
        "silently is how the carryover gets dropped."
    )
    unpaired = SPLIT_SNAPSHOT_SITES - set(REPLACEMENT_SITES)
    assert not unpaired, f"split site(s) {unpaired} are not listed in REPLACEMENT_SITES"


def test_every_ingest_replacement_door_is_accounted_for():
    """Ingest's own wrapper still guards every delete-then-insert it owns.

    Separate from the primitive count above because it answers a different
    question: the wrapper is where ingest asks "may I replace this at all"
    (`_conversation_claimed_elsewhere`), which merge answers with its owner gate
    instead. A new call here is a new place that can refuse — or fail to.
    """
    found = sites_by_function(call_to(("_take_conversation_for_replacement",)))
    assert found == INGEST_REPLACEMENT_DOORS, (
        f"ingest replacement doors moved: {found} != {INGEST_REPLACEMENT_DOORS}."
    )


def test_every_door_that_removes_assignments_consults_the_tag_gate():
    """A replacement, an adoption, or a split asks `_declined_for_tags` before it acts.

    The gate is the single decision ``siftd ingest --reparse`` makes about user
    tags, so the property is that no door bypasses it, and it is read off two
    populations rather than trusted: the functions that call a removing primitive
    (a delete or a release), and the functions that call the gate.

    Scope, stated so a reader can judge what it does not catch: this counts
    *consultations*, not their order or the row they name. A door that calls the
    gate and then acts on a different row passes; what keeps the two on one row
    is that the target is resolved once (`_replacement_target`) and the door
    predicates (`_split_moves_assignments`, `_refused_claim_is_a_copy`) are shared
    between the gate and the door. The gate itself is a no-op without `--reparse`,
    so a plain ingest is not covered by this property.
    """
    # The removing primitives are reached only through the two door wrappers.
    release = sites_by_function(call_to(("release_carryover",)), exclude=DEFINITION_SITE)
    assert release == RELEASE_SITES, (
        f"release sites moved: {release} != {RELEASE_SITES}. A function that releases "
        "assignments from a kept row is a replacement door: it must consult the tag gate."
    )

    doors = sites_by_function(call_to(GATE_DOOR_PRIMITIVES))
    gated = sites_by_function(call_to((TAG_GATE,)))
    assert gated == TAG_GATE_SITES, (
        f"tag-gate consultations moved: {gated} != {TAG_GATE_SITES}."
    )

    for site, per in doors.items():
        if site in GATED_BY_CALLER:
            continue
        takes = per.get("_take_conversation_for_replacement", 0)
        consults = gated.get(site, {}).get(TAG_GATE, 0)
        assert consults >= max(takes, 1), (
            f"{site} removes assignments ({per}) but consults the tag gate {consults} time(s). "
            "Ask `_declined_for_tags` before the door acts, or name the caller that does in "
            "GATED_BY_CALLER."
        )

    # An exemption is a claim about a caller: check it, so it cannot go stale.
    callers = sites_by_function(call_to(("_reingest_file",)))
    assert callers == {("ingestion/orchestration.py", "ingest_all"): {"_reingest_file": 1}}, (
        f"_reingest_file is called from {callers}; its exemption assumed ingest_all's gated file branch only."
    )
    unlisted = set(GATED_BY_CALLER) - set(doors)
    assert not unlisted, f"GATED_BY_CALLER names {unlisted}, which are not doors any more: remove them."


def test_carried_set_matches_the_carryover(tmp_path):
    """The declared carried set is what `ConversationCarryover` actually holds.

    Without this, `CARRIED` is a claim about another module that nothing checks
    — and the first thing that went wrong in #54's own fix was a field added to
    the dataclass and not to everything that enumerates it.
    """
    from siftd.storage.replacement import ConversationCarryover

    holds = {name for name in ConversationCarryover().__dataclass_fields__}
    # The carryover's field names are per-fact; map them to the tables they
    # restore into. A new field with no mapping is the failure this catches.
    field_tables = {
        "conversation": "tag_assignments",
        "events": "tag_assignments",
        "owners": "conversation_owners",
        "dropped_events": None,   # counted, not carried
        "dropped_blocks": None,   # counted, not carried
    }
    assert holds == set(field_tables), (
        f"ConversationCarryover's fields changed: {sorted(holds ^ set(field_tables))}. "
        "Map the new one to the table it restores into (or to None if it is a "
        "loss counter), and add that table to CARRIED."
    )
    assert {t for t in field_tables.values() if t} == set(CARRIED)


def test_every_cascade_child_is_carried_or_declared(tmp_path):
    """Every table a conversation delete reaches has an opinion recorded."""
    from conftest import make_db
    from siftd.storage.fts import rebuild_fts_index
    from siftd.storage.sqlite import (
        delete_conversation,
        ensure_conversation_owners_table,
        open_database,
    )
    from siftd.storage.attributes import set_attribute
    from siftd.storage.tags import apply_tag, get_or_create_tag
    from siftd.storage.usage_rollup import rebuild_rollups

    db = make_db(
        tmp_path / "cascade.db",
        conversations=[{"external_id": "c1", "prompt_text": "hello",
                        "tool_name": "sh", "tags": ["keep"]}],
    )
    conn = open_database(db)
    try:
        ensure_conversation_owners_table(conn)
        # Declared half: every cascade from `conversations`, whether or not
        # anything in this fixture happens to have written a row to it.
        declared_children = {
            table
            for (table,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            if fk[2] == "conversations" and fk[6] == "CASCADE"
        }

        conv_id = conn.execute("SELECT id FROM conversations").fetchone()[0]
        conn.execute(
            "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at)"
            " VALUES (?, 'alice', NULL, '2024-01-01T00:00:00Z')", (conv_id,),
        )
        rebuild_fts_index(conn)
        rebuild_rollups(conn)

        # Seed every polymorphic target kind the `tr_polymorphic_*_cleanup`
        # triggers reach. That cleanup is invisible to `foreign_key_list`, so
        # the delete-diff is the only half that can see it — and a diff only
        # sees what the fixture wrote. `attributes` was missed exactly this
        # way, which is the same fixture-shaped blind spot that hid
        # `ingested_files` from the previous cut.
        event_id = conn.execute("SELECT id FROM events LIMIT 1").fetchone()[0]
        block_id = conn.execute("SELECT id FROM event_content LIMIT 1").fetchone()[0]
        for kind, target in (("conversation", conv_id), ("prompt", event_id), ("block", block_id)):
            apply_tag(conn, kind, target, get_or_create_tag(conn, f"t-{kind}"))
            set_attribute(conn, kind, target, "k", "v", scope="analyzer")
        conn.commit()

        tables = [
            name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        def counts():
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

        before = counts()
        delete_conversation(conn, conv_id)
        conn.commit()
        after = counts()
    finally:
        conn.close()

    # Emptied by the delete, plus declared cascades the fixture never populated,
    # plus `conversations` itself (which has no FK to itself).
    population = {t for t in tables if after[t] < before[t]} | declared_children | {"conversations"}
    declared = set(CARRIED) | set(NOT_CARRIED)

    assert population - declared == set(), (
        f"a conversation delete reaches {sorted(population - declared)}, which nothing "
        "declares. Add it to CARRIED (and to storage/replacement.py's "
        "snapshot/restore) or to NOT_CARRIED with the reason a replacement's own "
        "parse supplies it again."
    )
    assert declared - population == set(), (
        f"{sorted(declared - population)} is declared but a conversation delete no "
        "longer reaches it — drop the entry rather than leaving a claim nothing tests."
    )
