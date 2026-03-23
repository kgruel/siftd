import types

import pytest

from siftd.embeddings import base


def test_get_backend_uses_cache_and_invalidate(monkeypatch):
    base.invalidate_backend_cache()
    calls = []

    def fake_try(name, verbose):
        calls.append((name, verbose))
        return types.SimpleNamespace(name=name, model="m", dimension=3)

    monkeypatch.setattr(base, "_try_backend", fake_try)
    b1 = base.get_backend(verbose=False)
    b2 = base.get_backend(verbose=True)
    assert b1 is b2
    assert calls == [("ollama", False)]

    base.invalidate_backend_cache()
    b3 = base.get_backend(verbose=True)
    assert b3.name == "ollama"
    assert calls[-1] == ("ollama", True)


def test_get_backend_preferred_and_missing(monkeypatch):
    base.invalidate_backend_cache()
    monkeypatch.setattr(base, "_try_backend", lambda n, v: types.SimpleNamespace(name=n, model="m", dimension=1) if n == "fastembed" else None)
    ok = base.get_backend(preferred="fastembed")
    assert ok.name == "fastembed"

    base.invalidate_backend_cache()
    monkeypatch.setattr(base, "_try_backend", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="Requested embedding backend 'fastembed' is not available"):
        base.get_backend(preferred="fastembed")


def test_get_backend_fallback_and_none_available(monkeypatch):
    base.invalidate_backend_cache()

    def only_fastembed(name, _verbose):
        return types.SimpleNamespace(name=name, model="m", dimension=1) if name == "fastembed" else None

    monkeypatch.setattr(base, "_try_backend", only_fastembed)
    b = base.get_backend()
    assert b.name == "fastembed"

    base.invalidate_backend_cache()
    monkeypatch.setattr(base, "_try_backend", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="No embedding backend available"):
        base.get_backend()


def test_try_backend_paths_and_verbose(monkeypatch, capsys):
    class _Ollama:
        def __init__(self):
            self.model = "om"

    class _Fast:
        def __init__(self):
            self.model = "fm"

    monkeypatch.setitem(__import__("sys").modules, "siftd.embeddings.ollama_backend", types.SimpleNamespace(OllamaBackend=_Ollama))
    monkeypatch.setitem(__import__("sys").modules, "siftd.embeddings.fastembed_backend", types.SimpleNamespace(FastEmbedBackend=_Fast))

    ob = base._try_backend("ollama", verbose=True)
    fb = base._try_backend("fastembed", verbose=True)
    assert ob.model == "om" and fb.model == "fm"
    err = capsys.readouterr().err
    assert "Using embedding backend: ollama" in err and "Using embedding backend: fastembed" in err


def test_try_backend_unknown_and_exception_swallow(monkeypatch):
    with pytest.raises(ValueError, match="Unknown backend"):
        base._try_backend("nope", verbose=False)

    class _Bad:
        def __init__(self):
            raise RuntimeError("boom")

    monkeypatch.setitem(__import__("sys").modules, "siftd.embeddings.ollama_backend", types.SimpleNamespace(OllamaBackend=_Bad))
    assert base._try_backend("ollama", verbose=False) is None
