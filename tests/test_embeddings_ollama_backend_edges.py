import io
import json
import urllib.error

import pytest

from siftd.embeddings.ollama_backend import OllamaBackend


class _Resp:
    def __init__(self, payload): self.payload = payload
    def read(self): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *_a): return False


def _url(req):
    return req.full_url if hasattr(req, "full_url") else str(req)


def test_model_discovery_priority_and_embed_fallback(monkeypatch):
    calls = {"tags": 0}

    def fake(req, timeout=5):
        if _url(req).endswith("/api/tags"):
            calls["tags"] += 1
            if calls["tags"] == 1:
                return _Resp({"models": [{"name": "abc"}, {"name": "nomic-embed-text:latest"}]})
            if calls["tags"] == 2:
                return _Resp({"models": [{"name": "some-embed-model"}]})
            return _Resp({"models": [{"name": "plain-model"}]})
        if _url(req).endswith("/api/embeddings"):
            return _Resp({"embedding": [0.1, 0.2, 0.3]})
        raise AssertionError(_url(req))

    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert OllamaBackend(base_url="http://x").model == "nomic-embed-text:latest"
    assert OllamaBackend(base_url="http://x").model == "some-embed-model"
    with pytest.raises(RuntimeError, match="No embedding model found"):
        OllamaBackend(base_url="http://x")


def test_embed_batch_one_and_error_paths(monkeypatch):
    def fake(req, timeout=5):
        if _url(req).endswith("/api/tags"):
            return _Resp({"models": [{"name": "nomic-embed-text"}]})
        if _url(req).endswith("/api/embeddings"):
            prompt = json.loads(req.data.decode())["prompt"]
            if prompt == "badjson":
                return _Resp({"x": 1})
            if prompt == "urlerr":
                raise urllib.error.URLError("down")
            return _Resp({"embedding": [float(len(prompt))]})
        raise AssertionError(_url(req))

    monkeypatch.setattr("urllib.request.urlopen", fake)
    b = OllamaBackend(base_url="http://x")
    assert b.dimension == 1 and b.embed(["a", "bc"]) == [[1.0], [2.0]] and b.embed_one("xyz") == [3.0]
    with pytest.raises(RuntimeError, match="Ollama embed failed"):
        b.embed_one("badjson")
    with pytest.raises(RuntimeError, match="Ollama embed failed"):
        b.embed_one("urlerr")


def test_list_models_connection_and_json_errors(monkeypatch):
    b = object.__new__(OllamaBackend)
    b.base_url = "http://x"
    for fn in (
        lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("nope")),
        lambda *_a, **_k: io.BytesIO(b"not-json"),
    ):
        monkeypatch.setattr("urllib.request.urlopen", fn)
        with pytest.raises(ConnectionError, match="Cannot connect to Ollama"):
            b._list_models()
