import builtins
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


def test_bool_and_loopback_parsers_edge_cases(monkeypatch):
    assert delegation._parse_bool_like("maybe") is None
    monkeypatch.setattr("siftd.serve.delegation.urlparse", lambda _u: (_ for _ in ()).throw(ValueError("bad")))
    assert delegation.is_loopback_url("http://example") is False


def test_delegation_enabled_and_resolve_url_import_and_config_fallbacks(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "siftd.config":
            raise ImportError("no config")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("SIFTD_SERVE_DELEGATE", "maybe")
    assert delegation.delegation_enabled() is True
    assert delegation.resolve_serve_url() == ("http://127.0.0.1:8484", False)

    monkeypatch.setattr(builtins, "__import__", real_import)
    monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)

    def fake_get(key):
        return {"serve.url": "http://cfg:9999", "serve.port": "not-int"}.get(key)

    monkeypatch.setattr("siftd.config.get_config", fake_get)
    assert delegation.resolve_serve_url() == ("http://cfg:9999", True)


def test_can_delegate_non_loopback_autodiscovered_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr("siftd.serve.delegation.delegation_enabled", lambda: True)
    monkeypatch.setattr("siftd.serve.delegation.resolve_serve_url", lambda: ("http://example.com:8484", False))
    assert delegation.can_delegate(db=tmp_path / "x.db") is False


def test_try_delegate_success_and_failure_guards(monkeypatch, tmp_path):
    db = tmp_path / "siftd.db"
    db.touch()

    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: True)
    monkeypatch.setattr("siftd.serve.delegation.resolve_serve_url", lambda: ("http://127.0.0.1:8484", True))
    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": str(db.resolve())})
    monkeypatch.setattr("siftd.serve.client._get_json", lambda *a, **k: {"ok": True})
    assert delegation.try_delegate("/v1/search", db=db) == {"ok": True}

    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": 123})
    assert delegation.try_delegate("/v1/search", db=db) is None

    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": str(db.resolve())})
    monkeypatch.setattr("siftd.serve.client._get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert delegation.try_delegate("/v1/search", db=db) is None


def test_try_delegate_post_guard_and_exception_paths(monkeypatch, tmp_path):
    db = tmp_path / "siftd.db"
    db.touch()

    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: False)
    assert delegation.try_delegate_post("/v1/tags", {}, db=db) is None

    monkeypatch.setattr("siftd.serve.delegation.can_delegate", lambda **k: True)
    monkeypatch.setattr("siftd.serve.delegation.resolve_serve_url", lambda: ("http://127.0.0.1:8484", False))
    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": 7})
    assert delegation.try_delegate_post("/v1/tags", {}, db=db) is None

    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": "/wrong.db"})
    assert delegation.try_delegate_post("/v1/tags", {}, db=db) is None

    monkeypatch.setattr("siftd.serve.client.probe_health", lambda **k: {"db_path": str(db.resolve())})
    monkeypatch.setattr("siftd.serve.client._post_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert delegation.try_delegate_post("/v1/tags", {}, db=db) is None


def test_resolve_serve_url_invalid_port_falls_back(monkeypatch, tmp_path):
    monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)

    def fake_get(key):
        return "bad-port" if key == "serve.port" else None

    monkeypatch.setattr("siftd.config.get_config", fake_get)
    monkeypatch.setattr("siftd.paths.state_dir", lambda: tmp_path)
    assert delegation.resolve_serve_url() == ("http://127.0.0.1:8484", False)
