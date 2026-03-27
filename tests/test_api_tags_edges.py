
from siftd.api import tags as api_tags


def test_modify_conversation_tag_apply_remove_and_not_found(monkeypatch):
    calls = {}

    class _Conn:
        def commit(self):
            calls["committed"] = True

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr("siftd.api.tags._open_database", lambda path: _Conn())
    monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda conn, typ, cid, owner=None: None)
    assert api_tags.modify_conversation_tag("c", "t") == []

    monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda conn, typ, cid, owner=None: "conv")
    monkeypatch.setattr("siftd.storage.queries.fetch_conversation_tags", lambda conn, cid: ["t"])
    monkeypatch.setattr("siftd.api.tags._get_or_create_tag", lambda conn, name: "tid")
    monkeypatch.setattr("siftd.api.tags._apply_tag", lambda conn, et, eid, tid: None)
    assert api_tags.modify_conversation_tag("c", "t", action="apply") == ["t"]

    monkeypatch.setattr("siftd.api.tags._get_tag_id", lambda conn, name: "tid")
    monkeypatch.setattr("siftd.api.tags._remove_tag", lambda conn, et, eid, tid: None)
    assert api_tags.modify_conversation_tag("c", "t", action="remove") == ["t"]
    assert calls["committed"] and calls["closed"]


def test_rename_tag_auto_open_commit_and_close(monkeypatch):
    calls = {}

    class _Conn:
        def close(self):
            calls["closed"] = True

    monkeypatch.setattr("siftd.api.tags._open_database", lambda path: _Conn())

    def _rename(conn, old_name, new_name, *, commit=False):
        calls["commit"] = commit
        return True

    monkeypatch.setattr("siftd.api.tags._rename_tag", _rename)
    assert api_tags.rename_tag(conn=None, old_name="a", new_name="b") is True
    assert calls["commit"] is True and calls["closed"]
