from types import SimpleNamespace

from siftd.serve import delegation


def test_try_delegate_post_success_and_try_serve_post(monkeypatch, tmp_path):
    db = tmp_path / "siftd.db"
    db.touch()

    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: True)
    monkeypatch.setattr("siftd.serve.delegation.resolve_serve_url", lambda: ("http://127.0.0.1:8484", False))
    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": str(db.resolve())})
    monkeypatch.setattr("siftd.serve.client._post_json", lambda *a, **k: {"ok": True})

    assert delegation.try_delegate_post("/v1/tags", {"x": 1}, db=db) == {"ok": True}

    op = SimpleNamespace(method="POST", path="/v1/tags", params={"db_path": db, "x": 1}, db=db)
    assert delegation.try_serve(op) == {"ok": True}


def test_try_serve_get_remaps_lambda_and_handles_errors(monkeypatch, tmp_path):
    db = tmp_path / "siftd.db"
    db.touch()

    monkeypatch.setattr("siftd.serve.delegation.try_delegate", lambda p, params, **k: {"params": params})
    op = SimpleNamespace(method="GET", path="/v1/search", params={"db_path": db, "lambda_": 0.7}, db=db)
    assert delegation.try_serve(op) == {"params": {"lambda": 0.7}}

    monkeypatch.setattr("siftd.serve.delegation.try_delegate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert delegation.try_serve(op) is None
