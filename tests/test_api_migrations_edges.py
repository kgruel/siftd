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

    def cb(*_a):
        return None

    assert mig.backfill_git_remotes(object(), on_progress=cb, dry_run=True)["ok"] == 1
    assert mig.merge_duplicate_workspaces(object(), on_progress=cb, dry_run=True)["ok"] == 2
    assert mig.verify_workspace_identity(object())["ok"] == 3
    assert calls["backfill"]["dry_run"] is True and calls["merge"]["dry_run"] is True


def test_migration_wrappers_lift_storage_lines_to_progress_events():
    """The wrapper adapts storage's per-row str callback into ProgressEvents."""
    from siftd.domain.progress import ProgressEvent

    seen = []
    shim = mig._line_shim("merge workspaces", seen.append)
    assert shim is not None
    shim("  Merging: /a/b")
    assert len(seen) == 1
    ev = seen[0]
    assert isinstance(ev, ProgressEvent)
    assert ev.group == "merge workspaces" and ev.message == "  Merging: /a/b"
    # No sink → no shim, so storage skips the callback entirely.
    assert mig._line_shim("g", None) is None
