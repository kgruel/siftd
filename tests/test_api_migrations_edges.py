import sys
from types import SimpleNamespace

import siftd.api.migrations as mig


def test_migration_wrappers_delegate(monkeypatch):
    calls = {}

    def backfill(conn, on_progress=None, dry_run=False):
        calls["backfill"] = (on_progress, dry_run)
        return {"ok": 1}

    def merge(conn, on_progress=None, dry_run=False):
        calls["merge"] = (on_progress, dry_run)
        return {"ok": 2}

    def verify(conn):
        return {"ok": 3}

    def blobs(conn, batch_size=1000, on_progress=None):
        calls["blobs"] = (batch_size, on_progress)
        return {"ok": 4}

    monkeypatch.setitem(
        sys.modules,
        "siftd.storage.migrate_workspaces",
        SimpleNamespace(
            backfill_git_remotes=backfill,
            merge_duplicate_workspaces=merge,
            verify_workspace_identity=verify,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "siftd.storage.migrate_blobs",
        SimpleNamespace(migrate_existing_results=blobs),
    )

    def cb(*_a):
        return None

    assert mig.backfill_git_remotes(object(), on_progress=cb, dry_run=True)["ok"] == 1
    assert mig.merge_duplicate_workspaces(object(), on_progress=cb, dry_run=True)["ok"] == 2
    assert mig.migrate_blobs(object(), batch_size=5, on_progress=cb)["ok"] == 4
    assert mig.verify_workspace_identity(object())["ok"] == 3
    assert calls["backfill"][1] is True and calls["merge"][1] is True and calls["blobs"][0] == 5
