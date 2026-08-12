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

- The site count is scoped to `ingestion/orchestration.py`. `api/merge.py`
  replaces conversations too, and carries its own ownership by hand — that
  second door is #77, and this ratchet deliberately does not claim it.
- Counting sites guards *arity*, not *pairing*. A site that snapshots and never
  restores changes the count, but a path that drops the carryover on an early
  return does not.
- The delete-diff half only sees tables the fixture populates. It is the
  corroborating half; ``foreign_key_list`` is the complete one.
"""

import ast

from architecture.support import REPO_ROOT

# Call sites of `_take_conversation_for_replacement`, per enclosing function.
# Named rather than totalled: a bare count survives deleting one site and
# adding another, and cannot say which one is new — the question the guard
# exists to prompt. Mirrors `test_readonly_opens.py`'s per-(file, function)
# keying.
REPLACEMENT_SITES = {
    "ingest_all": 2,
    "_reingest_file": 1,
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
}


def _replacement_call_sites() -> dict[str, int]:
    """`_take_conversation_for_replacement` calls, keyed by enclosing function."""
    path = REPO_ROOT / "src" / "siftd" / "ingestion" / "orchestration.py"
    tree = ast.parse(path.read_text())
    sites: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = sum(
            1 for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_take_conversation_for_replacement"
        )
        if calls:
            sites[node.name] = calls
    return sites


def test_every_replacement_site_is_accounted_for():
    """The trigger enumeration is hand-written; this is what guards it.

    A replacement *trigger* is a condition, so it cannot be read off the source
    the way `DEDUP_STRATEGY` can. What can be read is the doors: every
    delete-then-insert in ingest goes through
    `_take_conversation_for_replacement`, so a new one shows up here. That is
    the signal to ask which trigger it introduces and whether
    `tests/test_live_tagging.py::_REPLACEMENT_TRIGGERS` still covers the
    population — the question nobody asked when #36 made a second trigger
    common.
    """
    assert _replacement_call_sites() == REPLACEMENT_SITES, (
        f"replacement sites moved: {_replacement_call_sites()} != {REPLACEMENT_SITES}. "
        "Every one of them deletes a conversation and stores a new one, so every "
        "one can lose what `storage/replacement.py` does not carry. Check "
        "_REPLACEMENT_TRIGGERS still enumerates the conditions that reach them."
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
        event_id = conn.execute("SELECT id FROM events LIMIT 1").fetchone()[0]
        apply_tag(conn, "prompt", event_id, get_or_create_tag(conn, "evt"))
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
