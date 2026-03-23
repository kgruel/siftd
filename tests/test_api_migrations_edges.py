import sys
from types import SimpleNamespace

import siftd.api.migrations as mig


def test_migration_wrappers_delegate(monkeypatch):
    calls = {}

    def _mk(tag, ok):
        def fn(*args, **kwargs):
            calls[tag] = kwargs
            return {"ok": ok}

        return fn

    monkeypatch.setitem(sys.modules, "siftd.storage.migrate_workspaces", SimpleNamespace(
        backfill_git_remotes=_mk("backfill", 1),
        merge_duplicate_workspaces=_mk("merge", 2),
        verify_workspace_identity=lambda conn: {"ok": 3},
    ))
    monkeypatch.setitem(sys.modules, "siftd.storage.migrate_blobs", SimpleNamespace(migrate_existing_results=_mk("blobs", 4)))

    def cb(*_a):
        return None

    assert mig.backfill_git_remotes(object(), on_progress=cb, dry_run=True)["ok"] == 1
    assert mig.merge_duplicate_workspaces(object(), on_progress=cb, dry_run=True)["ok"] == 2
    assert mig.migrate_blobs(object(), batch_size=5, on_progress=cb)["ok"] == 4
    assert mig.verify_workspace_identity(object())["ok"] == 3
    assert calls["backfill"]["dry_run"] is True and calls["merge"]["dry_run"] is True and calls["blobs"]["batch_size"] == 5
