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

Two properties hold that list to its population, and they fail differently:

- **the sites** — a fourth place that deletes a conversation and stores a new
  one is a fourth place that can drop what the list does not carry;
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

- The site count is over the *primitive*, not over ingest's wrapper. It was
  scoped to `ingestion/orchestration.py` until #77, which is what that scoping
  cost: `api/merge.py` was the second replacement door all along, carrying
  ownership by hand and hard-deleting tags — the mirror of #54 — and a ratchet
  keyed to `_take_conversation_for_replacement` could not see it, because merge
  has its own delete and never calls that wrapper. Keying on
  `snapshot_conversation`/`restore_conversation` names the population by what a
  replacement *does*, so a third door is visible whatever it wraps itself in.
- Counting sites guards *arity*, not *pairing*. A site that snapshots and never
  restores changes the count, but a path that drops the carryover on an early
  return does not.
- The delete-diff half only sees tables the fixture populates. It is the
  corroborating half; ``foreign_key_list`` is the complete one.
"""

import ast

from architecture.support import REPO_ROOT

# Call sites of the carry primitives, per (module, enclosing function).
# Named rather than totalled: a bare count survives deleting one site and
# adding another, and cannot say which one is new — the question the guard
# exists to prompt. Mirrors `test_readonly_opens.py`'s per-(file, function)
# keying.
REPLACEMENT_SITES = {
    ("ingestion/orchestration.py", "_take_conversation_for_replacement"): {
        "snapshot_conversation": 1,
    },
    ("ingestion/orchestration.py", "_restore_carryover_after_replacement"): {
        "restore_conversation": 1,
    },
    ("api/merge.py", "_replace_stale_conversations"): {"snapshot_conversation": 1},
    ("api/merge.py", "_merge_attached"): {"restore_conversation": 1},
}

# Where ingest's own wrapper is called from. The wrapper is what makes "ask
# before replacing" structural for ingest — merge answers that question with
# its owner gate instead — so its arity is still worth pinning, one level down
# from the primitive count above.
INGEST_REPLACEMENT_DOORS = {
    "ingest_all": 2,
    "_reingest_file": 1,
}
CARRY_PRIMITIVES = ("snapshot_conversation", "restore_conversation")

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


def _calls_by_function(names: tuple[str, ...]) -> dict[tuple[str, str], dict[str, int]]:
    """Calls to `names` across `src/siftd`, keyed by (module, enclosing function).

    Swept over the whole package rather than a named file — a ratchet that
    reads one module can only ever confirm that module, which is what let
    `api/merge.py` be a replacement door nothing here could see.
    """
    root = REPO_ROOT / "src" / "siftd"
    sites: dict[tuple[str, str], dict[str, int]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            counts: dict[str, int] = {}
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id in names
                ):
                    counts[child.func.id] = counts.get(child.func.id, 0) + 1
            if counts:
                sites[(path.relative_to(root).as_posix(), node.name)] = counts
    return sites


def _ingest_door_call_sites() -> dict[str, int]:
    """`_take_conversation_for_replacement` calls, keyed by enclosing function."""
    return {
        function: counts["_take_conversation_for_replacement"]
        for (module, function), counts in _calls_by_function(
            ("_take_conversation_for_replacement",)
        ).items()
        if module == "ingestion/orchestration.py"
    }


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

    A site that snapshots without restoring (or the reverse) shows up as a
    lopsided entry rather than a silent one: that shape is #54 and #77 both.
    """
    found = _calls_by_function(CARRY_PRIMITIVES)
    assert found == REPLACEMENT_SITES, (
        f"replacement sites moved: {found} != {REPLACEMENT_SITES}. Every one of "
        "them deletes a conversation and stores a new one, so every one can lose "
        "what `storage/replacement.py` does not carry. A site that appears with "
        "only a snapshot, or only a restore, is a half-carry. Check "
        "_REPLACEMENT_TRIGGERS still enumerates the conditions that reach them."
    )
    snapshots = sum(c.get("snapshot_conversation", 0) for c in found.values())
    restores = sum(c.get("restore_conversation", 0) for c in found.values())
    assert snapshots == restores, (
        f"{snapshots} snapshot site(s) against {restores} restore site(s) — a "
        "carryover that is taken and never put back is the loss itself."
    )


def test_every_ingest_replacement_door_is_accounted_for():
    """Ingest's own wrapper still guards every delete-then-insert it owns.

    Separate from the primitive count above because it answers a different
    question: the wrapper is where ingest asks "may I replace this at all"
    (`_conversation_claimed_elsewhere`), which merge answers with its owner gate
    instead. A new call here is a new place that can refuse — or fail to.
    """
    assert _ingest_door_call_sites() == INGEST_REPLACEMENT_DOORS, (
        f"ingest replacement doors moved: {_ingest_door_call_sites()} != "
        f"{INGEST_REPLACEMENT_DOORS}."
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
