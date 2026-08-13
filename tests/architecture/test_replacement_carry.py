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

from architecture.support import SRC, docstring_ids, literal_text, source_files

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
}
CARRY_PRIMITIVES = ("snapshot_conversation", "restore_and_report")
DEFINITION_SITE = "storage/replacement.py"

# The other half of the guard, keyed on the delete instead of the carry.
#
# A replacement door can decline to snapshot — that is what both #54 and #77
# were, and it is why the count above could not see them. It cannot decline to
# *delete*. So this population is the tractable one, and the two properties are
# complements rather than substitutes: delete-keying cannot see a half-carry (a
# door that snapshots and never restores still deletes, so it looks correct
# from here), and carry-keying cannot see a door that never snapshots at all.
DESTROY_PRIMITIVES = ("delete_conversation", "delete_conversations")
DESTROY_LITERAL = "DELETE FROM conversations"
DESTROY_DEFINITION_SITE = "storage/sqlite.py"

# Enclosing functions that destroy a conversation without carrying anything
# across, and why that is correct. Shrink-only, in the shape of
# `test_imports.py`'s allowlist: an entry may leave, and a new one has to be
# argued for here rather than discovered in a bug.
#
# Empty, and verified so rather than left unexamined. #79 anticipated a
# `siftd db delete` needing a carve-out; there is no such command — no CLI,
# api, or serve path destroys a conversation. The only two doors in the tree
# are the two replacement doors, and both are paired.
UNPAIRED_DESTROY_SITES: dict[tuple[str, str], str] = {}

# Where ingest's own wrapper is called from, in the same shape. The wrapper is
# what makes "ask before replacing" structural for ingest — merge answers that
# question with its owner gate instead — so its arity is worth pinning, one
# level down from the primitive count above. Deliberately not filtered to
# `ingestion/`: if the wrapper is ever called from outside ingest, the mismatch
# should be the alarm rather than something the query silently discards.
INGEST_REPLACEMENT_DOORS = {
    ("ingestion/orchestration.py", "ingest_all"): {
        "_take_conversation_for_replacement": 2,
    },
    ("ingestion/orchestration.py", "_reingest_file"): {
        "_take_conversation_for_replacement": 1,
    },
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
    "content_fts": "derived from event_content; the replacement's insert writes its own",
    "content_fts_content": "content_fts's shadow storage",
    "content_fts_docsize": "content_fts's shadow storage",
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


def _calls_by_function(
    names: tuple[str, ...], *, exclude: str | None = None
) -> dict[tuple[str, str], dict[str, int]]:
    """Calls to `names` across `src/siftd`, keyed by (module, enclosing function).

    Swept over the whole package rather than a named file — a ratchet that
    reads one module can only ever confirm that module, which is what let
    `api/merge.py` be a replacement door nothing here could see.

    Scope is rebound at each nested `FunctionDef` rather than walking the whole
    subtree per function, so a call inside a nested function is attributed to
    exactly one scope. `test_readonly_opens.py::_sites` does the same, for the
    same reason; the first cut of this used `ast.walk` per function and would
    have counted such a call twice.

    Both call spellings count: bare `f()` and attribute `mod.f()`. Only the
    first was matched until #79, which is a hole precisely where it costs the
    most — the delete guard below is a *name* sweep, and `sq.delete_conversation(...)`
    is a live spelling in this repo's tests. A door reaching the primitive
    through its module would have carried no bare name and no SQL literal, so
    both halves of that guard would have missed it. Matching on `.attr` alone
    is deliberately loose: it counts an unrelated method that happens to share
    the name, which fails *loudly* into the allowlist rather than silently out
    of the population. #79's own trigger was the opposite trade — a name
    collision resolved by renaming, not by teaching this to resolve imports.
    """
    sites: dict[tuple[str, str], dict[str, int]] = {}
    for path in source_files():
        rel = path.relative_to(SRC).as_posix()
        if rel == exclude:
            continue
        counts: dict[str, dict[str, int]] = {}

        def visit(node: ast.AST, scope: str, counts: dict = counts) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit(child, child.name, counts)
                    continue
                if isinstance(child, ast.Call):
                    called = (
                        child.func.id
                        if isinstance(child.func, ast.Name)
                        else child.func.attr
                        if isinstance(child.func, ast.Attribute)
                        else None
                    )
                    if called in names:
                        per = counts.setdefault(scope, {})
                        per[called] = per.get(called, 0) + 1
                visit(child, scope, counts)

        visit(ast.parse(path.read_text()), "<module>")
        for scope, per in counts.items():
            sites[(rel, scope)] = per
    return sites


def _sql_literals_by_function(
    needle: str, *, exclude: str | None = None
) -> dict[tuple[str, str], int]:
    """Static string literals containing `needle`, keyed by (module, function).

    The half that catches a door bypassing the primitive entirely — a raw
    `conn.execute("DELETE FROM conversations ...")` carries no call this file
    would recognise. `literal_text` reads through f-strings, so an interpolated
    predicate still contributes its keywords; `docstring_ids` keeps prose about
    the delete from counting as a use, which matters here because the module
    docstrings in `storage/` quote this exact statement.
    """
    sites: dict[tuple[str, str], int] = {}
    for path in source_files():
        rel = path.relative_to(SRC).as_posix()
        if rel == exclude:
            continue
        tree = ast.parse(path.read_text())
        prose = docstring_ids(tree)
        counts: dict[str, int] = {}

        def visit(node: ast.AST, scope: str, counts: dict = counts, prose: set = prose) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit(child, child.name, counts, prose)
                    continue
                if id(child) not in prose:
                    text = literal_text(child)
                    if text and needle.lower() in text.lower():
                        counts[scope] = counts.get(scope, 0) + 1
                visit(child, scope, counts, prose)

        visit(tree, "<module>")
        for scope, n in counts.items():
            sites[(rel, scope)] = n
    return sites


def test_every_site_that_destroys_a_conversation_is_paired_or_declared():
    """Every place a conversation is destroyed either carries, or says why not.

    The complement of `test_every_replacement_site_is_accounted_for`, and the
    reason #79 exists: that one keys on what a *correct* door does, so it can
    only see doors already using the primitives — while both defects it was
    built to prevent were doors that did not. `api/merge.py` hand-rolled its
    own snapshot for the whole life of the previous ratchet.

    Pairing is **same module**, not same enclosing function. Same-function is
    the stronger claim and neither real door satisfies it: ingest snapshots in
    `_take_conversation_for_replacement` and restores in
    `_restore_carryover_after_replacement`; merge snapshots in
    `_replace_stale_conversations` and restores in `_merge_attached`. A
    property no correct code satisfies is a property that gets weakened at the
    first failure, which is how an allowlist becomes a habit.

    Both spellings of the destroy count — the primitive by name, and the raw
    SQL literal for a door that never reaches the primitive at all.
    """
    by_call = _calls_by_function(DESTROY_PRIMITIVES, exclude=DESTROY_DEFINITION_SITE)
    by_sql = _sql_literals_by_function(DESTROY_LITERAL, exclude=DESTROY_DEFINITION_SITE)
    found = set(by_call) | set(by_sql)

    carrying_modules = {module for module, _ in REPLACEMENT_SITES}
    unpaired = {
        site
        for site in found
        if site[0] not in carrying_modules and site not in UNPAIRED_DESTROY_SITES
    }
    assert unpaired == set(), (
        f"{sorted(unpaired)} destroys a conversation in a module that never "
        "snapshots a carryover. Either pair it — `snapshot_conversation` before "
        "the delete, `restore_and_report` after — or add it to "
        "UNPAIRED_DESTROY_SITES with the reason nothing attached to that "
        "conversation needs to survive."
    )
    stale = set(UNPAIRED_DESTROY_SITES) - found
    assert stale == set(), (
        f"{sorted(stale)} is allowlisted but no longer destroys a conversation — "
        "drop the entry. The allowlist is shrink-only, which it cannot be if "
        "dead entries are left to accumulate."
    )


def test_the_delete_closure_has_exactly_one_home():
    """`DELETE FROM conversations` is written once, where the closure lives.

    The positive half of the sweep above, which by construction only looks
    *outside* `storage/sqlite.py`. Without this, moving the statement into a
    second module and deleting the original passes both tests: the literal
    would simply have relocated to a place the exclusion hides. #51 is what one
    unnoticed second copy of this closure costs.
    """
    home = SRC / DESTROY_DEFINITION_SITE
    tree = ast.parse(home.read_text())
    prose = docstring_ids(tree)
    uses = [
        node
        for node in ast.walk(tree)
        if id(node) not in prose
        and (text := literal_text(node))
        and DESTROY_LITERAL.lower() in text.lower()
    ]
    assert len(uses) == 1, (
        f"{DESTROY_DEFINITION_SITE} spells {DESTROY_LITERAL!r} {len(uses)} times, "
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
    found = _calls_by_function(CARRY_PRIMITIVES, exclude=DEFINITION_SITE)
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
    snapshots = sum(c.get("snapshot_conversation", 0) for c in REPLACEMENT_SITES.values())
    restores = sum(c.get("restore_and_report", 0) for c in REPLACEMENT_SITES.values())
    assert snapshots == restores, (
        f"{snapshots} snapshot site(s) listed against {restores} restore site(s). "
        "If a door really does snapshot without restoring, say why here — "
        "silently is how the carryover gets dropped."
    )


def test_every_ingest_replacement_door_is_accounted_for():
    """Ingest's own wrapper still guards every delete-then-insert it owns.

    Separate from the primitive count above because it answers a different
    question: the wrapper is where ingest asks "may I replace this at all"
    (`_conversation_claimed_elsewhere`), which merge answers with its owner gate
    instead. A new call here is a new place that can refuse — or fail to.
    """
    found = _calls_by_function(("_take_conversation_for_replacement",))
    assert found == INGEST_REPLACEMENT_DOORS, (
        f"ingest replacement doors moved: {found} != {INGEST_REPLACEMENT_DOORS}."
    )


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
