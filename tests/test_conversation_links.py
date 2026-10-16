"""conversation_links: lineage as a first-class edge, resolved at read time.

``Conversation.lineage`` names the one parent a conversation continues, by the
parent's external_id. The edge never stores a conversation id: ingest replaces a
changed transcript with delete-then-insert under a fresh ULID, so a stored id
would dangle on every re-ingest of the parent.
"""

from __future__ import annotations

import sqlite3

import pytest
from painted import Fidelity

from siftd.api.conversations import list_conversations
from siftd.api.merge import merge_database
from siftd.api.slice import slice_database
from siftd.domain import ContentBlock, Conversation, Harness, Lineage, Prompt, Response
from siftd.storage.links import get_conversation_children, get_conversation_links, get_conversation_parent
from siftd.storage.sqlite import (
    create_database,
    delete_conversation,
    open_database,
    store_conversation,
)

T0 = "2024-04-01T10:00:00Z"


def _conv(external_id: str, *, lineage: Lineage | None = None, harness: str = "claude_code", started: str = T0) -> Conversation:
    return Conversation(
        external_id=external_id,
        harness=Harness(name=harness, source="test", log_format="jsonl"),
        started_at=started,
        prompts=[Prompt(
            timestamp=started, external_id="p", content=[ContentBlock(block_type="text", content={"text": f"hi {external_id}"})],
            responses=[Response(timestamp=started, external_id="r", model="m",
                                content=[ContentBlock(block_type="text", content={"text": "ok"})])],
        )],
        lineage=(lineage,) if lineage is not None else (),
    )


def _cid(conn, external_id: str) -> str:
    return conn.execute("SELECT id FROM conversations WHERE external_id = ?", (external_id,)).fetchone()[0]


@pytest.fixture
def conn(tmp_path):
    c = create_database(tmp_path / "t.db")
    yield c
    c.close()


class TestWrite:
    def test_lineage_is_stored_as_one_row(self, conn):
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("fork", "c::parent", anchor="rec-9")), commit=True)
        row = conn.execute("SELECT * FROM conversation_links WHERE conversation_id = ?", (child,)).fetchone()
        assert (row["kind"], row["parent_external_id"], row["anchor"]) == ("fork", "c::parent", "rec-9")

    def test_no_lineage_no_row(self, conn):
        store_conversation(conn, _conv("c::alone"), commit=True)
        assert conn.execute("SELECT COUNT(*) FROM conversation_links").fetchone()[0] == 0

    def test_a_conversation_has_at_most_one_link_of_each_kind(self, conn):
        # The assembler keeps one link per relation; the table backs it with one per kind.
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("fork", "c::parent")), commit=True)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO conversation_links (conversation_id, kind, parent_external_id) VALUES (?, 'fork', 'other')",
                (child,),
            )

    def test_a_forked_subagent_stores_both_links_and_reads_where_it_ran_first(self, conn):
        store_conversation(conn, _conv("c::session"), commit=True)
        store_conversation(conn, _conv("c::origin"), commit=True)
        conv = _conv("c::child")
        conv.lineage = (Lineage("fork", "c::origin", anchor="r9"), Lineage("subagent", "c::session"))
        child = store_conversation(conn, conv, commit=True)
        assert [link.kind for link in get_conversation_links(conn, [child])[child]] == ["subagent", "fork"]
        assert get_conversation_parent(conn, child).parent_id == _cid(conn, "c::session")
        assert get_conversation_parent(conn, child, kind="fork").parent_id == _cid(conn, "c::origin")
        assert get_conversation_parent(conn, child, kind="resume") is None


class TestResolution:
    def test_resolves_to_the_parent_when_it_is_stored(self, conn):
        parent = store_conversation(conn, _conv("c::parent"), commit=True)
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("subagent", "c::parent")), commit=True)
        link = get_conversation_parent(conn, child)
        assert link is not None
        assert (link.kind, link.parent_external_id, link.parent_id) == ("subagent", "c::parent", parent)
        assert [c.conversation_id for c in get_conversation_children(conn, [parent])] == [child]

    def test_a_parent_that_was_never_ingested_dangles_without_error(self, conn):
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("subagent", "c::missing")), commit=True)
        link = get_conversation_parent(conn, child)
        assert link is not None and link.parent_id is None and link.parent_external_id == "c::missing"
        assert get_conversation_children(conn, [child]) == []

    def test_a_conversation_without_an_edge_has_no_parent(self, conn):
        alone = store_conversation(conn, _conv("c::alone"), commit=True)
        assert get_conversation_parent(conn, alone) is None
        assert get_conversation_links(conn, [alone]) == {}

    def test_reingesting_the_parent_cannot_leave_a_stale_pointer(self, conn):
        """The parent is replaced under a new ULID; the edge follows the external_id to it."""
        old_parent = store_conversation(conn, _conv("c::parent"), commit=True)
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("subagent", "c::parent")), commit=True)

        delete_conversation(conn, old_parent)
        new_parent = store_conversation(conn, _conv("c::parent"), commit=True)
        assert new_parent != old_parent

        link = get_conversation_parent(conn, child)
        assert link is not None and link.parent_id == new_parent
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_deleting_the_parent_leaves_the_edge_dangling(self, conn):
        parent = store_conversation(conn, _conv("c::parent"), commit=True)
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("fork", "c::parent")), commit=True)
        delete_conversation(conn, parent)
        conn.commit()
        link = get_conversation_parent(conn, child)
        assert link is not None and link.parent_id is None

    def test_resolution_stays_within_the_childs_harness(self, conn):
        store_conversation(conn, _conv("shared-id", harness="other_tool"), commit=True)
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("subagent", "shared-id")), commit=True)
        link = get_conversation_parent(conn, child)
        assert link is not None and link.parent_id is None

    def test_children_filter_by_kind_and_come_oldest_first(self, conn):
        parent = store_conversation(conn, _conv("c::parent"), commit=True)
        late = store_conversation(conn, _conv("c::late", lineage=Lineage("subagent", "c::parent"), started="2024-04-01T12:00:00Z"), commit=True)
        early = store_conversation(conn, _conv("c::early", lineage=Lineage("subagent", "c::parent"), started="2024-04-01T11:00:00Z"), commit=True)
        forked = store_conversation(conn, _conv("c::forked", lineage=Lineage("fork", "c::parent")), commit=True)
        assert [c.conversation_id for c in get_conversation_children(conn, [parent], kind="subagent")] == [early, late]
        assert {c.conversation_id for c in get_conversation_children(conn, [parent])} == {early, late, forked}

    def test_children_are_owner_scoped(self, conn):
        from siftd.storage.sqlite import ensure_conversation_owners_table

        ensure_conversation_owners_table(conn)
        parent = store_conversation(conn, _conv("c::parent"), commit=True)
        mine = store_conversation(conn, _conv("c::mine", lineage=Lineage("subagent", "c::parent")), commit=True)
        theirs = store_conversation(conn, _conv("c::theirs", lineage=Lineage("subagent", "c::parent")), commit=True)
        for cid, user in ((mine, "alice"), (theirs, "bob")):
            conn.execute(
                "INSERT INTO conversation_owners (conversation_id, user_id, assigned_at) VALUES (?, ?, ?)", (cid, user, T0)
            )
        conn.commit()
        assert [c.conversation_id for c in get_conversation_children(conn, [parent], owner="alice")] == [mine]
        assert get_conversation_children(conn, [parent], owner="nobody") == []


class TestDelete:
    def test_deleting_the_child_removes_its_link(self, conn):
        child = store_conversation(conn, _conv("c::child", lineage=Lineage("fork", "c::parent")), commit=True)
        assert conn.execute("SELECT COUNT(*) FROM conversation_links").fetchone()[0] == 1
        delete_conversation(conn, child)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM conversation_links").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _populated(path, *conversations: Conversation):
    c = create_database(path)
    for conv in conversations:
        store_conversation(c, conv)
    c.commit()
    c.close()
    return path


def _links(path) -> list[tuple]:
    c = sqlite3.connect(path)
    try:
        return c.execute(
            "SELECT c.external_id, l.kind, l.parent_external_id, l.anchor"
            " FROM conversation_links l JOIN conversations c ON c.id = l.conversation_id ORDER BY 1"
        ).fetchall()
    finally:
        c.close()


class TestCopySurfaces:
    def test_merge_carries_links_and_sequences(self, tmp_path):
        target = _populated(tmp_path / "target.db", _conv("c::other"))
        source = _populated(
            tmp_path / "source.db",
            _conv("c::parent"),
            _conv("c::child", lineage=Lineage("subagent", "c::parent", anchor="a1")),
        )
        merge_database(target, source)

        assert _links(target) == [("c::child", "subagent", "c::parent", "a1")]
        c = open_database(target, read_only=True)
        try:
            child = _cid(c, "c::child")
            assert get_conversation_parent(c, child).parent_id == _cid(c, "c::parent")
            assert c.execute(
                "SELECT COUNT(*), COUNT(sequence) FROM events WHERE conversation_id = ?", (child,)
            ).fetchone()[:] == (2, 2)
        finally:
            c.close()

    def test_a_replacing_merge_brings_the_newer_conversations_link(self, tmp_path):
        """Merge replaces a stale conversation; the edge is re-derived from the source's, not lost or kept stale."""
        target = _populated(tmp_path / "target.db", _conv("c::child", lineage=Lineage("fork", "c::old-parent")))
        source = _populated(tmp_path / "source.db", _conv("c::child", lineage=Lineage("subagent", "c::new-parent")))
        result = merge_database(target, source, replace=True)

        assert result["replaced_conversations"] == 1
        assert _links(target) == [("c::child", "subagent", "c::new-parent", None)]

    def test_a_replacing_merge_drops_the_edge_the_newer_version_no_longer_has(self, tmp_path):
        target = _populated(tmp_path / "target.db", _conv("c::child", lineage=Lineage("fork", "c::old-parent")))
        source = _populated(tmp_path / "source.db", _conv("c::child"))
        merge_database(target, source, replace=True)
        assert _links(target) == []

    def test_merge_refuses_a_source_without_the_links_table(self, tmp_path):
        target = _populated(tmp_path / "target.db", _conv("c::a"))
        source = _populated(tmp_path / "source.db", _conv("c::b"))
        raw = sqlite3.connect(source)
        raw.execute("DROP TABLE conversation_links")
        raw.commit()
        raw.close()
        with pytest.raises(RuntimeError, match="conversation_links"):
            merge_database(target, source)

    def test_slice_carries_links_and_sequences(self, tmp_path):
        source = _populated(
            tmp_path / "source.db",
            _conv("c::parent"),
            _conv("c::child", lineage=Lineage("subagent", "c::parent")),
            _conv("c::stranger"),
        )
        sliced = tmp_path / "slice.db"
        result = slice_database(source, sliced)

        assert result["conversations"] == 3
        assert _links(sliced) == [("c::child", "subagent", "c::parent", None)]
        c = sqlite3.connect(sliced)
        try:
            assert c.execute("SELECT COUNT(*), COUNT(sequence) FROM events").fetchone() == (6, 6)
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            c.close()

    def test_a_slice_of_the_child_alone_keeps_a_dangling_edge(self, tmp_path):
        source = _populated(
            tmp_path / "source.db",
            _conv("c::parent", started="2024-01-01T00:00:00Z"),
            _conv("c::child", lineage=Lineage("subagent", "c::parent"), started="2024-06-01T00:00:00Z"),
        )
        sliced = tmp_path / "slice.db"
        result = slice_database(source, sliced, since="2024-05-01")

        assert result["conversations"] == 1
        assert _links(sliced) == [("c::child", "subagent", "c::parent", None)]
        c = open_database(sliced, read_only=True)
        try:
            assert get_conversation_parent(c, _cid(c, "c::child")).parent_id is None
        finally:
            c.close()


class TestSessionsView:
    """list_conversations(group_subagents=True) nests sub-agents by the edge, and still by the legacy spelling."""

    def _db(self, tmp_path, *, linked: bool, marker: bool):
        """Two roots and three sub-agents, with the parent relation spelled by edge, by marker, or both."""
        def child(root: str, name: str, started: str) -> Conversation:
            ext = f"claude_code::{root}::agent::{name}" if marker else f"claude_code::{name}"
            return _conv(ext, started=started, lineage=Lineage("subagent", f"claude_code::{root}") if linked else None)

        return _populated(
            tmp_path / "g.db",
            _conv("claude_code::r1", started="2024-02-02T00:00:00Z"),
            child("r1", "a", "2024-02-02T00:01:00Z"),
            child("r1", "b", "2024-02-02T00:02:00Z"),
            _conv("claude_code::r2", started="2024-01-01T00:00:00Z"),
            child("r2", "c", "2024-01-01T00:01:00Z"),
        )

    @staticmethod
    def _shape(rows) -> dict[str, str | None]:
        return {r.external_id: r.parent_external_id for r in rows}

    def test_the_edge_and_the_legacy_spelling_give_identical_nesting(self, tmp_path):
        expected_n1 = {
            "claude_code::r1": None,
            "claude_code::r1::agent::a": "claude_code::r1",
            "claude_code::r1::agent::b": "claude_code::r1",
        }
        for name, linked in (("legacy", False), ("both", True)):
            (tmp_path / name).mkdir()
            db = self._db(tmp_path / name, linked=linked, marker=True)
            rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
            assert self._shape(rows) == expected_n1, name

    def test_an_edge_alone_nests_a_child_whose_external_id_carries_no_marker(self, tmp_path):
        db = self._db(tmp_path, linked=True, marker=False)
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        assert self._shape(rows) == {
            "claude_code::r1": None,
            "claude_code::a": "claude_code::r1",
            "claude_code::b": "claude_code::r1",
        }
        # ...and n counts roots only: the second root and its child are past the page.
        assert len(list_conversations(fidelity=Fidelity(), db_path=db, n=2, group_subagents=True)) == 5

    def test_a_mixed_database_nests_both_kinds_without_duplicates(self, tmp_path):
        """One child re-parsed since v13 (edge + marker), one still legacy (marker only)."""
        db = _populated(
            tmp_path / "m.db",
            _conv("claude_code::r1", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::r1::agent::a", started="2024-02-02T00:01:00Z", lineage=Lineage("subagent", "claude_code::r1")),
            _conv("claude_code::r1::agent::b", started="2024-02-02T00:02:00Z"),
        )
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        assert [r.external_id for r in rows].count("claude_code::r1::agent::a") == 1
        assert self._shape(rows) == {
            "claude_code::r1": None,
            "claude_code::r1::agent::a": "claude_code::r1",
            "claude_code::r1::agent::b": "claude_code::r1",
        }

    def test_a_non_subagent_edge_is_not_nested(self, tmp_path):
        db = _populated(
            tmp_path / "f.db",
            _conv("claude_code::r1", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::fork", started="2024-02-02T00:01:00Z", lineage=Lineage("fork", "claude_code::r1")),
        )
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=True)
        assert self._shape(rows) == {"claude_code::r1": None, "claude_code::fork": None}

    def test_a_childs_edge_does_not_cross_owners(self, tmp_path):
        db = self._db(tmp_path, linked=True, marker=False)
        raw = sqlite3.connect(db)
        from siftd.storage.sqlite import ensure_conversation_owners_table

        ensure_conversation_owners_table(raw)
        owners = {"claude_code::r1": "alice", "claude_code::a": "alice", "claude_code::b": "bob"}
        for ext, user in owners.items():
            raw.execute(
                "INSERT INTO conversation_owners (conversation_id, user_id, assigned_at)"
                " SELECT id, ?, ? FROM conversations WHERE external_id = ?", (user, T0, ext),
            )
        raw.commit()
        raw.close()
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True, owner="alice")
        assert {r.external_id for r in rows} == {"claude_code::r1", "claude_code::a"}


class TestSessionsViewEdgeCases:
    """Review r1: orphans stay visible, descendants are complete, the edge outranks the marker."""

    @staticmethod
    def _shape(rows) -> dict[str, str | None]:
        return {r.external_id: r.parent_external_id for r in rows}

    def test_a_subagent_whose_parent_is_not_stored_is_a_top_level_entry(self, tmp_path):
        """A child-only slice (or a deleted/never-ingested parent) must not vanish from Sessions."""
        for name, lineage, ext in (
            ("edge", Lineage("subagent", "claude_code::gone"), "claude_code::x"),
            ("legacy", None, "claude_code::gone::agent::x"),
        ):
            (tmp_path / name).mkdir()
            db = _populated(tmp_path / name / "o.db", _conv(ext, lineage=lineage))
            rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=True)
            # Listed, and still labelled a sub-agent so the renderer flags it.
            assert self._shape(rows) == {ext: "claude_code::gone"}, name

    def test_the_whole_subtree_of_a_root_is_pulled_in(self, tmp_path):
        db = _populated(
            tmp_path / "t.db",
            _conv("claude_code::root", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::child", started="2024-02-02T00:01:00Z", lineage=Lineage("subagent", "claude_code::root")),
            _conv("claude_code::grand", started="2024-02-02T00:02:00Z", lineage=Lineage("subagent", "claude_code::child")),
            # legacy spelling, one generation further down
            _conv("claude_code::grand::agent::great", started="2024-02-02T00:03:00Z"),
            _conv("claude_code::other", started="2024-01-01T00:00:00Z"),
        )
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        assert self._shape(rows) == {
            "claude_code::root": None,
            "claude_code::child": "claude_code::root",
            "claude_code::grand": "claude_code::child",
            "claude_code::grand::agent::great": "claude_code::grand",
        }
        assert len(rows) == len({r.id for r in rows})  # nobody twice

    def test_a_malformed_edge_cycle_terminates(self, tmp_path):
        db = _populated(
            tmp_path / "c.db",
            _conv("claude_code::root", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::a", started="2024-02-02T00:01:00Z", lineage=Lineage("subagent", "claude_code::root")),
            _conv("claude_code::b", started="2024-02-02T00:02:00Z", lineage=Lineage("subagent", "claude_code::a")),
        )
        raw = sqlite3.connect(db)
        raw.execute(
            "UPDATE conversation_links SET parent_external_id = 'claude_code::b'"
            " WHERE conversation_id = (SELECT id FROM conversations WHERE external_id = 'claude_code::a')"
        )
        raw.commit()
        raw.close()
        # a<->b is unreachable from any root; the point is that the walk ends.
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=True)
        assert {r.external_id for r in rows} == {"claude_code::root"}

    def test_an_edge_outranks_a_conflicting_marker(self, tmp_path):
        """root1::agent::child is linked to root2: it belongs to root2 only, once."""
        db = _populated(
            tmp_path / "x.db",
            _conv("claude_code::root1", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::root2", started="2024-02-01T00:00:00Z"),
            _conv("claude_code::root1::agent::child", started="2024-02-02T00:01:00Z",
                  lineage=Lineage("subagent", "claude_code::root2")),
        )
        r1 = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        assert self._shape(r1) == {"claude_code::root1": None}
        r2 = list_conversations(fidelity=Fidelity(), db_path=db, n=2, group_subagents=True)
        assert self._shape(r2) == {
            "claude_code::root1": None,
            "claude_code::root2": None,
            "claude_code::root1::agent::child": "claude_code::root2",
        }
        assert [r.external_id for r in r2].count("claude_code::root1::agent::child") == 1

    def test_a_non_subagent_edge_with_a_marker_is_not_labelled_a_subagent(self, tmp_path):
        db = _populated(
            tmp_path / "f.db",
            _conv("claude_code::r1", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::r1::agent::fork", started="2024-02-02T00:01:00Z", lineage=Lineage("fork", "claude_code::r1")),
        )
        for group in (True, False):
            rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=group)
            assert self._shape(rows) == {"claude_code::r1": None, "claude_code::r1::agent::fork": None}, group


class TestSessionsViewAcrossHarnesses:
    """Review r2: external_ids are unique per harness only, so nesting follows the resolved parent id."""

    _CTX = dict(live_enabled=False, detail_base="/folio", shell_base="/", follow_base="/follow")

    def _db(self, tmp_path):
        return _populated(
            tmp_path / "h.db",
            _conv("shared-parent", harness="claude_code", started="2024-02-02T00:00:00Z"),
            _conv("shared-parent", harness="other_tool", started="2024-02-02T00:05:00Z"),
            _conv("claude_code::child", harness="claude_code", started="2024-02-02T00:01:00Z",
                  lineage=Lineage("subagent", "shared-parent")),
        )

    def test_a_child_nests_under_its_own_harnesses_parent_only(self, tmp_path):
        from siftd.output.html_fmt import render_sessions

        db = self._db(tmp_path)
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=True)
        assert len(rows) == 3
        own_parent = next(r for r in rows if r.external_id == "shared-parent" and r.started_at == "2024-02-02T00:00:00Z")
        child = next(r for r in rows if r.external_id == "claude_code::child")
        assert child.parent_id == own_parent.id

        html = render_sessions([], rows, **self._CTX)
        assert html.count(f'data-parent="{own_parent.id}" hidden') == 1
        assert html.count("data-parent=") == 1  # the other harness's parent gets no child
        assert "1 agent</span>" in html and "2 agents</span>" not in html
        assert html.count("<li class=") == 3  # two roots and one nested child, no duplicate row

    def test_a_marker_child_does_not_match_a_same_named_parent_of_another_harness(self, tmp_path):
        db = _populated(
            tmp_path / "m.db",
            _conv("shared-parent", harness="other_tool", started="2024-02-02T00:00:00Z"),
            _conv("shared-parent::agent::x", harness="claude_code", started="2024-02-02T00:01:00Z"),
        )
        for group in (True, False):
            rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=group)
            child = next(r for r in rows if r.external_id == "shared-parent::agent::x")
            assert child.parent_external_id == "shared-parent" and child.parent_id is None, group
        # the orphan is a top-level entry in its own right, not a child pulled in
        # under the other harness's parent when that parent heads the page
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        assert {r.external_id for r in rows} == {"shared-parent::agent::x"}

    def test_a_page_headed_by_another_harnesss_parent_does_not_pull_in_a_marker_child(self, tmp_path):
        db = _populated(
            tmp_path / "n.db",
            _conv("shared-parent::agent::x", harness="claude_code", started="2024-02-02T00:01:00Z"),
            _conv("shared-parent", harness="other_tool", started="2024-02-02T00:05:00Z"),
        )
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        assert [r.external_id for r in rows] == ["shared-parent"]

    def test_the_resolved_parent_id_is_the_stored_parent_for_edge_and_marker(self, tmp_path):
        db = _populated(
            tmp_path / "p.db",
            _conv("claude_code::r1", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::e", started="2024-02-02T00:01:00Z", lineage=Lineage("subagent", "claude_code::r1")),
            _conv("claude_code::r1::agent::m", started="2024-02-02T00:02:00Z"),
        )
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1, group_subagents=True)
        root = next(r for r in rows if r.external_id == "claude_code::r1")
        assert root.parent_id is None
        assert {r.external_id: r.parent_id for r in rows if r is not root} == {
            "claude_code::e": root.id,
            "claude_code::r1::agent::m": root.id,
        }


class TestSessionsViewOwnerVisibility:
    """A child is nested only under a parent its owner can see (review A4).

    Root selection asks whether the parent is on the page, so it is decided within
    the owner-visible graph: an invisible parent leaves its visible child a root,
    and an invisible child is never listed beneath a visible parent.
    """

    @staticmethod
    def _owned(db, owners: dict[str, str]) -> None:
        from siftd.storage.sqlite import ensure_conversation_owners_table

        raw = sqlite3.connect(db)
        ensure_conversation_owners_table(raw)
        for ext, user in owners.items():
            raw.execute(
                "INSERT INTO conversation_owners (conversation_id, user_id, assigned_at)"
                " SELECT id, ?, ? FROM conversations WHERE external_id = ?", (user, T0, ext),
            )
        raw.commit()
        raw.close()

    def test_a_visible_child_of_an_inaccessible_parent_is_a_root(self, tmp_path):
        db = _populated(
            tmp_path / "p.db",
            _conv("claude_code::bob-parent", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::alice-child", started="2024-02-02T00:01:00Z",
                  lineage=Lineage("subagent", "claude_code::bob-parent")),
        )
        self._owned(db, {"claude_code::bob-parent": "bob", "claude_code::alice-child": "alice"})
        for grouped in (False, True):
            rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=grouped, owner="alice")
            assert [r.external_id for r in rows] == ["claude_code::alice-child"], grouped
            # The parent's name is the child's own data; Bob's conversation id is not.
            assert (rows[0].parent_external_id, rows[0].parent_id) == ("claude_code::bob-parent", None), grouped

    def test_an_inaccessible_child_under_a_visible_parent_is_not_listed(self, tmp_path):
        db = _populated(
            tmp_path / "c.db",
            _conv("claude_code::alice-parent", started="2024-02-02T00:00:00Z"),
            _conv("claude_code::bob-child", started="2024-02-02T00:01:00Z",
                  lineage=Lineage("subagent", "claude_code::alice-parent")),
        )
        self._owned(db, {"claude_code::alice-parent": "alice", "claude_code::bob-child": "bob"})
        for grouped in (False, True):
            rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=grouped, owner="alice")
            assert [r.external_id for r in rows] == ["claude_code::alice-parent"], grouped
        # Bob's own page: his child has no visible parent, so it is a root there.
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=5, group_subagents=True, owner="bob")
        assert [r.external_id for r in rows] == ["claude_code::bob-child"]
